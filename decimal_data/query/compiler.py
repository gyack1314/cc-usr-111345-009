"""类型推导与编译 IR。

冻结的结果类型规则(由 Rules 版本化):

- int64 与 decimal 混合时,int64 先提升为 decimal(19,0)。
- add/subtract(decimal):s = max(s1,s2),p = max(p1-s1, p2-s2) + s + 1。
- multiply(decimal):s = s1+s2,p = p1+p2+1。
- divide(一律走 decimal):s = max(rules.div_min_scale, s1+p2+1),
  p = (p1-s1)+s2+s;若 p 超过 maximum_precision,则压缩 s 使 p 等于上限,
  s 压缩到 0 仍不够时编译期拒绝。除法结果按 half-even 舍入到结果 scale。
- int64 的 add/subtract/multiply 结果仍是 int64,运行期检查溢出;
  int64 的 divide 先提升再按 decimal 规则。
- 结果精度超过 maximum_precision 的类型组合在编译期拒绝(除法按上条压缩)。
- 比较:数值按公共类型提升;bool 只允许 eq/ne;其余组合拒绝。
- AND/OR/NOT:操作数必须是 bool(或无类型 null),结果 bool,三值逻辑。
- CASE/COALESCE:结果为各分支的公共类型;公共类型 decimal 时
  s = max(si),p = max(pi-si) + s,超过上限拒绝。bool 与数值不能混合。
- CAST:数值之间允许(运行期按 half-even 舍入并检查溢出);
  bool 与数值之间不允许;null 可 cast 到任何类型。

编译器在 IR 中插入隐式 cast 节点:它们只会放大 scale/精度,因此永远精确,
不会触发舍入或溢出;需要舍入的只有查询里显式写出的 cast 与除法。

IR 节点按结构哈希共享(公共子表达式只保留一份),节点编号 nid 按输出顺序
先序分配,是错误定位与执行计数的稳定坐标。
"""
from dataclasses import dataclass

from . import expr as E
from .errors import (CompileError, INVALID_LITERAL, PRECISION_EXCEEDED,
                     TYPE_MISMATCH, UNKNOWN_COLUMN, UNSUPPORTED_TYPES)
from .fpdecimal import fits_int64, fits_precision, parse_decimal, rescale
from .fptypes import (BOOL, INT64, NULL, INT64_AS_DECIMAL, Bool, Decimal,
                      Int64, Null, parse_type)


@dataclass(frozen=True)
class Node:
    op: str               # column|const|add|subtract|multiply|divide|
                          # eq|ne|lt|le|gt|ge|and|or|not|case|coalesce|cast
    type: object
    children: tuple = ()
    value: object = None  # const 载荷:decimal 尾数 int / int / bool / None
    name: str = ''        # column 名
    target: object = None  # cast 目标类型
    implicit: bool = False  # 编译器插入的精确转换


class Plan:
    """一份编译完成的投影计划:类型、节点表、输出根。"""

    def __init__(self, rules, outputs, nodes, nid_of, paths):
        self.rules = rules
        self.outputs = outputs          # tuple[(名字, Node)]
        self.nodes = nodes              # tuple[Node],按下标即 nid
        self.nid_of = nid_of            # Node -> int
        self.paths = paths              # Node -> 源路径(首次出现)
        self._out_types = {n: node.type for n, node in outputs}

    def output_type(self, name):
        return self._out_types[name]

    def explain(self):
        r = self.rules
        lines = [f'rules version={r.version} maximum_precision={r.maximum_precision} '
                 f'rounding={r.rounding} div_min_scale={r.div_min_scale} '
                 f'avg_min_scale={r.avg_min_scale}']
        for name, node in self.outputs:
            lines.append(f'output {name}: {node.type}  (root #{self.nid_of[node]:06d})')
        for nid, node in enumerate(self.nodes):
            payload = ''
            if node.op == 'column':
                payload = f' {node.name}'
            elif node.op == 'const':
                payload = f' ={node.value!r}'
            elif node.op == 'cast':
                payload = (f' {node.children[0].type} -> {node.target}'
                           + (' [implicit]' if node.implicit else ' [explicit]'))
            kids = ' '.join(f'#{self.nid_of[c]:06d}' for c in node.children)
            if kids:
                payload += f'  <- {kids}'
            lines.append(f'#{nid:06d} {node.op} {node.type}{payload}'
                         f'   ; {self.paths.get(node, "?")}')
        return '\n'.join(lines)


def _is_numeric(t):
    return isinstance(t, (Int64, Decimal))


class _Compiler:
    def __init__(self, schema, rules):
        self.schema = schema
        self.rules = rules
        self._intern = {}
        self.paths = {}

    def node(self, path, **kw):
        n = Node(**kw)
        n = self._intern.setdefault(n, n)
        self.paths.setdefault(n, path)
        return n

    # ---- 类型规则 ----

    def common_type(self, t1, t2, path):
        if t1 == t2:
            return t1
        if isinstance(t1, Null):
            return t2
        if isinstance(t2, Null):
            return t1
        if _is_numeric(t1) and _is_numeric(t2):
            d1 = t1 if isinstance(t1, Decimal) else INT64_AS_DECIMAL
            d2 = t2 if isinstance(t2, Decimal) else INT64_AS_DECIMAL
            s = max(d1.scale, d2.scale)
            p = max(d1.precision - d1.scale, d2.precision - d2.scale) + s
            if p > self.rules.maximum_precision:
                raise CompileError(PRECISION_EXCEEDED, path,
                                   f'公共类型 decimal({p},{s}) 超过最大精度 '
                                   f'{self.rules.maximum_precision}')
            return Decimal(p, s)
        raise CompileError(TYPE_MISMATCH, path, f'没有公共类型: {t1} 与 {t2}')

    def _arith_type(self, op, t1, t2, path):
        if isinstance(t1, Null) and isinstance(t2, Null):
            return NULL, None, None
        if isinstance(t1, Null):
            t1 = t2
        if isinstance(t2, Null):
            t2 = t1
        if not (_is_numeric(t1) and _is_numeric(t2)):
            raise CompileError(TYPE_MISMATCH, path,
                               f'算术运算不支持 {t1} 与 {t2}')
        if isinstance(t1, Int64) and isinstance(t2, Int64) and op != 'divide':
            return INT64, None, None  # 运行期检查 int64 溢出
        d1 = t1 if isinstance(t1, Decimal) else INT64_AS_DECIMAL
        d2 = t2 if isinstance(t2, Decimal) else INT64_AS_DECIMAL
        mp = self.rules.maximum_precision
        if op in ('add', 'subtract'):
            s = max(d1.scale, d2.scale)
            p = max(d1.precision - d1.scale, d2.precision - d2.scale) + s + 1
            if p > mp:
                raise CompileError(PRECISION_EXCEEDED, path,
                                   f'{op} 结果 decimal({p},{s}) 超过最大精度 {mp}')
            # 操作数对齐到公共类型(只放大 scale,精确)
            common = Decimal(p - 1, s)
            return Decimal(p, s), common, common
        if op == 'multiply':
            s = d1.scale + d2.scale
            p = d1.precision + d2.precision + 1
            if p > mp:
                raise CompileError(PRECISION_EXCEEDED, path,
                                   f'multiply 结果 decimal({p},{s}) 超过最大精度 {mp}')
            return Decimal(p, s), d1, d2
        # divide:先按完整公式,再压缩 scale 到精度上限内
        s = max(self.rules.div_min_scale, d1.scale + d2.precision + 1)
        p = (d1.precision - d1.scale) + d2.scale + s
        if p > mp:
            s -= p - mp
            p = mp
            if s < 0:
                raise CompileError(
                    PRECISION_EXCEEDED, path,
                    f'divide 结果的整数部分超过最大精度 {mp}')
        return Decimal(p, s), d1, d2

    def _check_cast_legal(self, from_t, to_t, path):
        if isinstance(from_t, Null) or from_t == to_t:
            return
        if isinstance(from_t, Bool) or isinstance(to_t, Bool):
            raise CompileError(UNSUPPORTED_TYPES, path,
                               f'不支持 {from_t} 与 {to_t} 之间的 cast')
        if _is_numeric(from_t) and _is_numeric(to_t):
            return
        raise CompileError(UNSUPPORTED_TYPES, path,
                           f'不支持 {from_t} 到 {to_t} 的 cast')

    def cast_to(self, node, target, path, implicit):
        if node.type == target:
            return node
        self._check_cast_legal(node.type, target, path)
        return self.node(path, op='cast', type=target, children=(node,),
                         target=target, implicit=implicit)

    def _ensure_bool(self, node, path):
        if isinstance(node.type, Null):
            return self.node(path, op='const', type=BOOL, value=None)
        if not isinstance(node.type, Bool):
            raise CompileError(TYPE_MISMATCH, path,
                               f'逻辑运算需要 bool,得到 {node.type}')
        return node

    # ---- 递归编译 ----

    def compile(self, e):
        path = e.path
        if isinstance(e, E.Column):
            if e.name not in self.schema:
                raise CompileError(UNKNOWN_COLUMN, path, f'未知列: {e.name}')
            return self.node(path, op='column', type=self.schema[e.name],
                             name=e.name)
        if isinstance(e, E.Literal):
            return self._literal(e)
        if isinstance(e, E.Binary):
            left = self.compile(e.left)
            right = self.compile(e.right)
            if e.op in E.ARITH_OPS:
                return self._arith(e.op, left, right, path)
            if e.op in E.CMP_OPS:
                return self._compare(e.op, left, right, path)
            # and / or
            left = self._ensure_bool(left, e.left.path)
            right = self._ensure_bool(right, e.right.path)
            return self.node(path, op=e.op, type=BOOL, children=(left, right))
        if isinstance(e, E.Not):
            arg = self._ensure_bool(self.compile(e.arg), e.arg.path)
            return self.node(path, op='not', type=BOOL, children=(arg,))
        if isinstance(e, E.Case):
            return self._case(e)
        if isinstance(e, E.Coalesce):
            return self._coalesce(e)
        if isinstance(e, E.Cast):
            arg = self.compile(e.arg)
            try:
                target = parse_type(e.type_text)
            except ValueError as exc:
                raise CompileError(INVALID_LITERAL, path, str(exc)) from exc
            if isinstance(target, Decimal) and \
                    target.precision > self.rules.maximum_precision:
                raise CompileError(PRECISION_EXCEEDED, path,
                                   f'cast 目标 {target} 超过最大精度 '
                                   f'{self.rules.maximum_precision}')
            self._check_cast_legal(arg.type, target, path)
            return self.node(path, op='cast', type=target, children=(arg,),
                             target=target, implicit=False)
        raise CompileError(TYPE_MISMATCH, path, f'未知表达式: {e!r}')

    def _literal(self, e):
        path = e.path
        try:
            t = parse_type(e.type_text)
        except ValueError as exc:
            raise CompileError(INVALID_LITERAL, path, str(exc)) from exc
        if isinstance(t, Null):
            if e.text is not None:
                raise CompileError(INVALID_LITERAL, path,
                                   'null 字面量的值必须是 null')
            return self.node(path, op='const', type=NULL, value=None)
        if e.text is None:
            return self.node(path, op='const', type=t, value=None)
        if isinstance(t, Bool):
            if e.text in ('true', True):
                return self.node(path, op='const', type=BOOL, value=True)
            if e.text in ('false', False):
                return self.node(path, op='const', type=BOOL, value=False)
            raise CompileError(INVALID_LITERAL, path, f'非法 bool 字面量: {e.text!r}')
        if isinstance(t, Int64):
            try:
                v, scale = parse_decimal(e.text)
            except ValueError as exc:
                raise CompileError(INVALID_LITERAL, path, str(exc)) from exc
            if scale != 0:
                raise CompileError(INVALID_LITERAL, path,
                                   f'int64 字面量不能带小数: {e.text!r}')
            if not fits_int64(v):
                raise CompileError(INVALID_LITERAL, path,
                                   f'int64 字面量越界: {e.text!r}')
            return self.node(path, op='const', type=INT64, value=v)
        # decimal:精确读取后按声明类型做 half-even 缩放,越界即编译期拒绝
        try:
            m, scale = parse_decimal(e.text)
        except ValueError as exc:
            raise CompileError(INVALID_LITERAL, path, str(exc)) from exc
        m = rescale(m, scale, t.scale)
        if not fits_precision(m, t.precision):
            raise CompileError(INVALID_LITERAL, path,
                               f'字面量 {e.text!r} 超出 {t}')
        return self.node(path, op='const', type=t, value=m)

    def _arith(self, op, left, right, path):
        result_t, lt, rt = self._arith_type(op, left.type, right.type, path)
        children = (left, right)
        if lt is not None:
            children = (self.cast_to(left, lt, path, implicit=True),
                        self.cast_to(right, rt, path, implicit=True))
        return self.node(path, op=op, type=result_t, children=children)

    def _compare(self, op, left, right, path):
        t1, t2 = left.type, right.type
        if isinstance(t1, Null) or isinstance(t2, Null):
            other = t2 if isinstance(t1, Null) else t1
            if isinstance(other, Null):
                return self.node(path, op=op, type=BOOL, children=(left, right))
            t1 = t2 = other
        if isinstance(t1, Bool) and isinstance(t2, Bool):
            if op not in ('eq', 'ne'):
                raise CompileError(TYPE_MISMATCH, path, 'bool 只支持 = 与 <>')
            return self.node(path, op=op, type=BOOL, children=(left, right))
        if _is_numeric(t1) and _is_numeric(t2):
            common = self.common_type(t1, t2, path)
            return self.node(path, op=op, type=BOOL, children=(
                self.cast_to(left, common, path, implicit=True),
                self.cast_to(right, common, path, implicit=True)))
        raise CompileError(TYPE_MISMATCH, path, f'不能比较 {t1} 与 {t2}')

    def _case(self, e):
        pairs = []
        result_types = []
        for when, then in e.branches:
            w = self._ensure_bool(self.compile(when), when.path)
            t = self.compile(then)
            pairs.append((w, t))
            result_types.append(t.type)
        if e.otherwise is not None:
            otherwise = self.compile(e.otherwise)
        else:
            otherwise = self.node(e.path, op='const', type=NULL, value=None)
        result_types.append(otherwise.type)
        result_t = result_types[0]
        for t in result_types[1:]:
            result_t = self.common_type(result_t, t, e.path)
        children = []
        for w, t in pairs:
            children.append(w)
            children.append(self.cast_to(t, result_t, e.path, implicit=True))
        children.append(self.cast_to(otherwise, result_t, e.path, implicit=True))
        return self.node(e.path, op='case', type=result_t, children=tuple(children))

    def _coalesce(self, e):
        args = [self.compile(a) for a in e.args]
        result_t = args[0].type
        for a in args[1:]:
            result_t = self.common_type(result_t, a.type, e.path)
        return self.node(e.path, op='coalesce', type=result_t, children=tuple(
            self.cast_to(a, result_t, e.path, implicit=True) for a in args))


def compile_projection(schema, rules, named_exprs):
    """把 {输出名: Expr} 编译成 Plan;非法组合在编译期拒绝。"""
    compiler = _Compiler(schema, rules)
    outputs = tuple((name, compiler.compile(e)) for name, e in named_exprs.items())
    nodes = []
    nid_of = {}

    def visit(n):
        if n in nid_of:
            return
        nid_of[n] = len(nodes)
        nodes.append(n)
        for c in n.children:
            visit(c)

    for _, root in outputs:
        visit(root)
    return Plan(rules, outputs, tuple(nodes), nid_of, compiler.paths)
