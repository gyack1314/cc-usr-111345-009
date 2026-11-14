"""受限 AST:列、常量、四则、比较、AND/OR/NOT、CASE、COALESCE、显式 CAST。

AST 以 JSON 对象登记在 expressions 表中,parse_ast 负责结构校验并给每个
节点记录源路径(如 $.branches[0].when),编译期错误按此路径定位。

JSON 形态:
  {"column": "price"}
  {"literal": "0.000000", "type": "decimal(12,6)"}   # type 必填;null 字面量
                                                     # 写作 {"literal": null, "type": "null"}
  {"op": "add"|"subtract"|"multiply"|"divide", "left": E, "right": E}
  {"op": "eq"|"ne"|"lt"|"le"|"gt"|"ge", "left": E, "right": E}
  {"op": "and"|"or", "left": E, "right": E}
  {"op": "not", "arg": E}
  {"op": "case", "when": C, "then": T, "else": E}    # 单分支糖
  {"op": "case", "branches": [{"when": C, "then": T}, ...], "else": E}
  {"op": "coalesce", "args": [E, ...]}
  {"op": "cast", "arg": E, "type": "decimal(10,2)"}
"""
from dataclasses import dataclass

from .errors import CompileError, INVALID_AST

ARITH_OPS = ('add', 'subtract', 'multiply', 'divide')
CMP_OPS = ('eq', 'ne', 'lt', 'le', 'gt', 'ge')
LOGIC_OPS = ('and', 'or')


@dataclass(frozen=True)
class Expr:
    path: str


@dataclass(frozen=True)
class Column(Expr):
    name: str


@dataclass(frozen=True)
class Literal(Expr):
    text: object      # str | int | bool | None,按 type_text 解释
    type_text: str


@dataclass(frozen=True)
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Not(Expr):
    arg: Expr


@dataclass(frozen=True)
class Case(Expr):
    branches: tuple   # ((when, then), ...)
    otherwise: object  # Expr 或 None(缺省 ELSE 视为无类型 null)


@dataclass(frozen=True)
class Coalesce(Expr):
    args: tuple


@dataclass(frozen=True)
class Cast(Expr):
    arg: Expr
    type_text: str


def _fail(path, message):
    raise CompileError(INVALID_AST, path, message)


def parse_ast(obj, path='$'):
    if not isinstance(obj, dict):
        _fail(path, 'AST 节点必须是对象')
    if 'column' in obj:
        if not isinstance(obj['column'], str) or not obj['column']:
            _fail(path, 'column 必须是非空字符串')
        return Column(path, obj['column'])
    if 'literal' in obj:
        if 'type' not in obj:
            _fail(path, 'literal 缺少 type')
        if not isinstance(obj['type'], str):
            _fail(path, 'literal 的 type 必须是字符串')
        return Literal(path, obj['literal'], obj['type'])
    op = obj.get('op')
    if not isinstance(op, str):
        _fail(path, '节点缺少 op/column/literal')
    if op in ARITH_OPS + CMP_OPS + LOGIC_OPS:
        return Binary(path, op,
                      parse_ast(obj.get('left'), f'{path}.left'),
                      parse_ast(obj.get('right'), f'{path}.right'))
    if op == 'not':
        return Not(path, parse_ast(obj.get('arg'), f'{path}.arg'))
    if op == 'cast':
        if not isinstance(obj.get('type'), str):
            _fail(path, 'cast 缺少字符串 type')
        return Cast(path, parse_ast(obj.get('arg'), f'{path}.arg'), obj['type'])
    if op == 'coalesce':
        args = obj.get('args')
        if not isinstance(args, list) or not args:
            _fail(path, 'coalesce 需要非空 args')
        return Coalesce(path, tuple(
            parse_ast(a, f'{path}.args[{i}]') for i, a in enumerate(args)))
    if op == 'case':
        otherwise = obj.get('else')
        other_expr = (parse_ast(otherwise, f'{path}.else')
                      if otherwise is not None else None)
        if 'branches' in obj:
            branches = obj['branches']
            if not isinstance(branches, list) or not branches:
                _fail(path, 'case.branches 必须是非空数组')
            parsed = []
            for i, b in enumerate(branches):
                if not isinstance(b, dict) or 'when' not in b or 'then' not in b:
                    _fail(f'{path}.branches[{i}]', '分支需要 when 与 then')
                parsed.append((
                    parse_ast(b['when'], f'{path}.branches[{i}].when'),
                    parse_ast(b['then'], f'{path}.branches[{i}].then')))
            return Case(path, tuple(parsed), other_expr)
        if 'when' in obj and 'then' in obj:
            return Case(path, ((
                parse_ast(obj['when'], f'{path}.when'),
                parse_ast(obj['then'], f'{path}.then')),), other_expr)
        _fail(path, 'case 需要 branches 或 when/then')
    _fail(path, f'未知 op: {op!r}')
