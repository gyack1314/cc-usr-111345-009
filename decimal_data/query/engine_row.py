"""逐行精确解释器:批执行引擎的独立对照实现。

与批引擎共用同一套标量内核(kernels),但按行递归解释 IR,左到右、
按项目短路规则求值。每行内部对共享节点做单行记忆化,使公共子表达式
每行只求值一次(与批引擎的 memo 掩码语义一致),因此 counts 可逐点对照。
"""
from .errors import EvalError, KernelError, RowFailure
from .kernels import (k_addsub, k_cast, k_compare, k_divide, k_multiply,
                      tri_and, tri_or)
from .result import Result

_STRICT_BINARIES = ('add', 'subtract', 'multiply', 'divide',
                    'eq', 'ne', 'lt', 'le', 'gt', 'ge')


def execute_rows(plan, batch):
    counts = [0] * len(plan.nodes)
    errors = []
    values = {}
    for r, rid in enumerate(batch.row_ids):
        memo = {}
        out = {}
        try:
            for name, node in plan.outputs:
                out[name] = _ev(plan, node, r, batch, counts, memo)
        except RowFailure as err:
            node = plan.nodes[err.nid]
            errors.append(EvalError(err.category, rid, f'#{err.nid:06d}',
                                    plan.paths.get(node, '?'), err.detail))
            continue
        values[rid] = out
    return Result(plan, values, counts, errors)


def _ev(plan, node, r, batch, counts, memo):
    nid = plan.nid_of[node]
    if nid in memo:
        return memo[nid]
    counts[nid] += 1
    op = node.op
    try:
        if op == 'column':
            out = batch.columns[node.name][r]
        elif op == 'const':
            out = node.value
        elif op == 'not':
            v = _ev(plan, node.children[0], r, batch, counts, memo)
            out = None if v is None else (not v)
        elif op in _STRICT_BINARIES:
            left = _ev(plan, node.children[0], r, batch, counts, memo)
            right = _ev(plan, node.children[1], r, batch, counts, memo)
            if left is None or right is None:
                out = None
            elif op in ('add', 'subtract'):
                out = k_addsub(op, node.type, left, right)
            elif op == 'multiply':
                out = k_multiply(node.type, left, right)
            elif op == 'divide':
                out = k_divide(node.type, node.children[0].type,
                               node.children[1].type, left, right)
            else:
                out = k_compare(op, left, right)
        elif op == 'cast':
            v = _ev(plan, node.children[0], r, batch, counts, memo)
            out = None if v is None else k_cast(
                v, node.children[0].type, node.target)
        elif op == 'and':
            left = _ev(plan, node.children[0], r, batch, counts, memo)
            if left is False:
                out = False
            else:
                out = tri_and(left, _ev(plan, node.children[1], r, batch,
                                        counts, memo))
        elif op == 'or':
            left = _ev(plan, node.children[0], r, batch, counts, memo)
            if left is True:
                out = True
            else:
                out = tri_or(left, _ev(plan, node.children[1], r, batch,
                                       counts, memo))
        elif op == 'case':
            children = node.children
            n_when = (len(children) - 1) // 2
            out = None
            matched = False
            for i in range(n_when):
                c = _ev(plan, children[2 * i], r, batch, counts, memo)
                if c is True:
                    out = _ev(plan, children[2 * i + 1], r, batch, counts, memo)
                    matched = True
                    break
            if not matched:
                out = _ev(plan, children[-1], r, batch, counts, memo)
        elif op == 'coalesce':
            out = None
            for child in node.children:
                v = _ev(plan, child, r, batch, counts, memo)
                if v is not None:
                    out = v
                    break
        else:  # pragma: no cover
            raise AssertionError(f'未知节点: {op}')
    except KernelError as err:
        raise RowFailure(err.category, nid, err.detail) from err
    memo[nid] = out
    return out
