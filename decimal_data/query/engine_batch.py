"""按掩码求值的批执行引擎。

- 行集合用 Python 大整数位掩码表示,节点按"需求掩码"驱动求值。
- 每个节点记录自己已为哪些行求值(memo 掩码 + 值数组):公共子表达式
  每行只求值一次,但求值只发生在被需求的行上,绝不越过 CASE /
  COALESCE / AND / OR 的惰性边界提前计算。
- 某行在某节点出错:记录 EvalError(row_id, 节点位置, 类别),该行进入
  dead 掩码,后续需求自动跳过它;其它行继续。每行最多记录一个错误,
  与行解释器"首个错误即中止该行"的行为一致。
- counts[nid] 统计该节点实际执行了多少次行求值,可与行解释器逐点对照。
"""
from .errors import EvalError, KernelError
from .kernels import (k_addsub, k_cast, k_compare, k_divide, k_multiply,
                      tri_and, tri_or)
from .result import Result

_STRICT_BINARIES = ('add', 'subtract', 'multiply', 'divide',
                    'eq', 'ne', 'lt', 'le', 'gt', 'ge')


def _bits(mask):
    while mask:
        b = mask & -mask
        yield b.bit_length() - 1
        mask ^= b


class _Ctx:
    __slots__ = ('plan', 'batch', 'n', 'masks', 'vals', 'counts',
                 'errors', 'dead')

    def __init__(self, plan, batch):
        self.plan = plan
        self.batch = batch
        self.n = len(batch)
        k = len(plan.nodes)
        self.masks = [0] * k
        self.vals = [None] * k
        self.counts = [0] * k
        self.errors = []
        self.dead = 0

    def fail(self, nid, r, err):
        node = self.plan.nodes[nid]
        self.errors.append(EvalError(
            err.category, self.batch.row_ids[r], f'#{nid:06d}',
            self.plan.paths.get(node, '?'), err.detail))
        self.dead |= 1 << r


def execute(plan, batch):
    """对整批执行计划,返回 Result(错误不抛出,由调用方决定)。"""
    ctx = _Ctx(plan, batch)
    full = (1 << ctx.n) - 1
    for _, node in plan.outputs:
        _ensure(ctx, plan.nid_of[node], full)
    out_nids = [(name, plan.nid_of[node]) for name, node in plan.outputs]
    values = {}
    for r in range(ctx.n):
        if ctx.dead >> r & 1:
            continue
        values[batch.row_ids[r]] = {
            name: ctx.vals[nid][r] for name, nid in out_nids}
    return Result(plan, values, ctx.counts, ctx.errors)


def execute_chunked(plan, batch, chunk_size):
    """按 chunk_size 分批执行并合并;结果必须与整批执行一致。"""
    if chunk_size < 1:
        raise ValueError('chunk_size 必须 >= 1')
    from .batch import take_rows
    values = {}
    counts = [0] * len(plan.nodes)
    errors = []
    for start in range(0, len(batch), chunk_size):
        sub = take_rows(batch, range(start, min(start + chunk_size, len(batch))))
        res = execute(plan, sub)
        values.update(res.values)
        for i, c in enumerate(res.counts):
            counts[i] += c
        errors.extend(res.errors)
    return Result(plan, values, counts, errors)


def _ensure(ctx, nid, mask):
    """保证节点 nid 对 mask 中的活行都有值;只计算缺失且未死的行。"""
    need = mask & ~ctx.masks[nid] & ~ctx.dead
    if not need:
        return
    node = ctx.plan.nodes[nid]
    vals = ctx.vals[nid]
    if vals is None:
        vals = ctx.vals[nid] = [None] * ctx.n
    op = node.op
    if op == 'column':
        col = ctx.batch.columns[node.name]
        for r in _bits(need):
            vals[r] = col[r]
        done = need
    elif op == 'const':
        for r in _bits(need):
            vals[r] = node.value
        done = need
    elif op in _STRICT_BINARIES or op in ('cast', 'not'):
        done = _strict(ctx, node, nid, need, vals)
    elif op in ('and', 'or'):
        done = _logic(ctx, node, nid, need, vals)
    elif op == 'case':
        done = _case(ctx, node, nid, need, vals)
    else:  # coalesce
        done = _coalesce(ctx, node, nid, need, vals)
    ctx.masks[nid] |= done
    ctx.counts[nid] += need.bit_count()


def _child_values(ctx, node, need):
    """对严格节点:先求全部子节点,返回 (可用行掩码, 子值数组列表)。"""
    plan = ctx.plan
    cnids = [plan.nid_of[c] for c in node.children]
    for c in cnids:
        _ensure(ctx, c, need)
    ready = need
    for c in cnids:
        ready &= ctx.masks[c]
    return ready, cnids


def _strict(ctx, node, nid, need, vals):
    ready, cnids = _child_values(ctx, node, need)
    op = node.op
    done = 0
    for r in _bits(ready):
        args = [ctx.vals[c][r] for c in cnids]
        if op == 'not':
            v = args[0]
            vals[r] = None if v is None else (not v)
            done |= 1 << r
            continue
        if any(a is None for a in args):
            vals[r] = None
            done |= 1 << r
            continue
        try:
            if op in ('add', 'subtract'):
                out = k_addsub(op, node.type, args[0], args[1])
            elif op == 'multiply':
                out = k_multiply(node.type, args[0], args[1])
            elif op == 'divide':
                out = k_divide(node.type, node.children[0].type,
                               node.children[1].type, args[0], args[1])
            elif op == 'cast':
                out = k_cast(args[0], node.children[0].type, node.target)
            else:
                out = k_compare(op, args[0], args[1])
        except KernelError as err:
            ctx.fail(nid, r, err)
            continue
        vals[r] = out
        done |= 1 << r
    return done


def _logic(ctx, node, nid, need, vals):
    """AND/OR:左操作数已决定的行不再求值右操作数(项目短路规则)。"""
    plan = ctx.plan
    lnid = plan.nid_of[node.children[0]]
    rnid = plan.nid_of[node.children[1]]
    _ensure(ctx, lnid, need)
    lm = ctx.masks[lnid] & need
    lvals = ctx.vals[lnid]
    decide_on = False if node.op == 'and' else True
    combine = tri_and if node.op == 'and' else tri_or
    done = 0
    want = 0
    for r in _bits(lm):
        if lvals[r] is decide_on:
            vals[r] = lvals[r]
            done |= 1 << r
        else:
            want |= 1 << r
    _ensure(ctx, rnid, want)
    rm = ctx.masks[rnid] & want
    rvals = ctx.vals[rnid]
    for r in _bits(rm):
        vals[r] = combine(lvals[r], rvals[r])
        done |= 1 << r
    return done


def _case(ctx, node, nid, need, vals):
    """CASE:条件按序求值,只计算被选中分支;未选中分支的错误不产生。"""
    plan = ctx.plan
    children = node.children
    n_when = (len(children) - 1) // 2
    remaining = need
    done = 0
    for i in range(n_when):
        wnid = plan.nid_of[children[2 * i]]
        _ensure(ctx, wnid, remaining)
        wm = ctx.masks[wnid] & remaining
        wvals = ctx.vals[wnid]
        sel = 0
        for r in _bits(wm):
            if wvals[r] is True:  # null 条件视为不选中
                sel |= 1 << r
        if sel:
            tnid = plan.nid_of[children[2 * i + 1]]
            _ensure(ctx, tnid, sel)
            tm = ctx.masks[tnid] & sel
            tvals = ctx.vals[tnid]
            for r in _bits(tm):
                vals[r] = tvals[r]
                done |= 1 << r
        remaining &= ~sel
        remaining &= ~ctx.dead
        if not remaining:
            return done
    enid = plan.nid_of[children[-1]]
    _ensure(ctx, enid, remaining)
    em = ctx.masks[enid] & remaining
    evals = ctx.vals[enid]
    for r in _bits(em):
        vals[r] = evals[r]
        done |= 1 << r
    return done


def _coalesce(ctx, node, nid, need, vals):
    """COALESCE:逐参数求值,首个非 null 即停;后面参数的错误不产生。"""
    plan = ctx.plan
    last = len(node.children) - 1
    remaining = need
    done = 0
    for i, child in enumerate(node.children):
        cnid = plan.nid_of[child]
        _ensure(ctx, cnid, remaining)
        cm = ctx.masks[cnid] & remaining
        cvals = ctx.vals[cnid]
        got = 0
        for r in _bits(cm):
            v = cvals[r]
            if v is not None:
                vals[r] = v
                done |= 1 << r
                got |= 1 << r
            elif i == last:
                vals[r] = None  # 全部参数为 null -> 结果 null
                done |= 1 << r
        remaining &= ~got
        remaining &= ~ctx.dead
        if not remaining:
            break
    return done
