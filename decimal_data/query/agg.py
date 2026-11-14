"""分组 SUM / COUNT / AVG 的定点状态。

设计要点:
- 累积器比输出宽:SUM/AVG 的和以 Python 任意精度整数尾数累积,
  只有 finalize 时才按登记输出类型检查;某个前缀超出输出范围但完整和
  可表示时不报错。整数加法满足结合交换律,最终溢出判定与分片顺序无关。
- 登记输出类型(冻结规则):SUM(int64)->int64,SUM(decimal(p,s))->
  decimal(maximum_precision, s);AVG(int64)->decimal(mp, avg_min_scale),
  AVG(decimal(p,s))->decimal(mp, max(s, avg_min_scale));COUNT->int64。
- 状态携带类型与规则版本,跨批合并时逐项校验,不一致即拒绝。
- 全 null 组:SUM/AVG 结果为 null,COUNT(列) 为 0,COUNT(*) 为行数;
  空输入不产生任何分组。
"""
import copy
from dataclasses import dataclass

from .compiler import compile_projection
from .engine_batch import execute as batch_execute
from .engine_row import execute_rows as row_execute
from .errors import (OVERFLOW, TYPE_MISMATCH, VERSION_MISMATCH, CompileError,
                     EvalError, QueryFailure)
from .fpdecimal import fits_int64, fits_precision, round_quot_half_even
from .fptypes import INT64, Bool, Decimal, Int64, Null

SUM = 'sum'
COUNT = 'count'
AVG = 'avg'


@dataclass(frozen=True)
class AggSpec:
    kind: str           # SUM | COUNT | AVG
    arg: object = None  # Expr;COUNT(*) 时为 None


@dataclass(frozen=True)
class AggDesc:
    name: str
    kind: str
    out_type: object
    arg_scale: object    # sum/avg 累积尾数的 scale;count 为 None
    arg_output: object   # 表达式计划中的输出名;count(*) 为 None
    position: str        # 错误定位:agg:<名>


class AggPlan:
    def __init__(self, rules, expr_plan, group_names, aggs):
        self.rules = rules
        self.expr_plan = expr_plan
        self.group_names = group_names  # tuple[str] 表达式计划输出名
        self.aggs = aggs                # tuple[AggDesc]


def compile_aggregation(schema, rules, group_by, aggs):
    """group_by / aggs 均为 {名字: 表达式或 AggSpec},顺序即输出顺序。"""
    named = {}
    group_names = []
    for name, e in group_by.items():
        out = f'g:{name}'
        named[out] = e
        group_names.append(out)
    pending = []
    for name, spec in aggs.items():
        if spec.kind not in (SUM, COUNT, AVG):
            raise CompileError(TYPE_MISMATCH, f'agg:{name}',
                               f'未知聚合: {spec.kind!r}')
        if spec.kind == COUNT and spec.arg is None:
            pending.append((name, spec, None))
            continue
        if spec.arg is None:
            raise CompileError(TYPE_MISMATCH, f'agg:{name}',
                               f'{spec.kind} 需要参数表达式')
        out = f'a:{name}'
        named[out] = spec.arg
        pending.append((name, spec, out))
    expr_plan = compile_projection(schema, rules, named)
    descs = []
    for name, spec, arg_out in pending:
        pos = f'agg:{name}'
        if spec.kind == COUNT:
            descs.append(AggDesc(name, COUNT, INT64, None, arg_out, pos))
            continue
        arg_t = expr_plan.output_type(arg_out)
        if isinstance(arg_t, (Bool, Null)):
            raise CompileError(TYPE_MISMATCH, pos,
                               f'{spec.kind} 不支持参数类型 {arg_t}')
        if isinstance(arg_t, Int64):
            arg_scale = 0
        else:
            arg_scale = arg_t.scale
        mp = rules.maximum_precision
        if spec.kind == SUM:
            out_t = INT64 if isinstance(arg_t, Int64) else Decimal(mp, arg_scale)
        else:  # AVG
            out_t = Decimal(mp, max(arg_scale, rules.avg_min_scale))
        descs.append(AggDesc(name, spec.kind, out_t, arg_scale, arg_out, pos))
    return AggPlan(rules, expr_plan, tuple(group_names), tuple(descs))


class _State:
    __slots__ = ('mantissa', 'count')

    def __init__(self):
        self.mantissa = 0   # sum/avg:和尾数;count:未用
        self.count = 0      # sum/avg:非 null 行数;count:计数


class GroupStates:
    """groups: 键 -> [首行 row_id, [_State, ...]];键是带类型标签的值元组。"""

    def __init__(self, plan):
        self.plan = plan
        self.groups = {}

    def to_dict(self):
        r = self.plan.rules
        return {
            'rules': {'version': r.version,
                      'maximum_precision': r.maximum_precision,
                      'rounding': r.rounding,
                      'div_min_scale': r.div_min_scale,
                      'avg_min_scale': r.avg_min_scale},
            'aggs': [{'name': d.name, 'kind': d.kind,
                      'out_type': str(d.out_type), 'arg_scale': d.arg_scale}
                     for d in self.plan.aggs],
            'groups': [
                {'key': [list(item) for item in key],
                 'first_row_id': entry[0],
                 'states': [{'mantissa': str(st.mantissa), 'count': st.count}
                            for st in entry[1]]}
                for key, entry in sorted(self.groups.items(),
                                         key=lambda kv: kv[1][0])],
        }

    @staticmethod
    def from_dict(plan, data):
        r = plan.rules
        want = {'version': r.version, 'maximum_precision': r.maximum_precision,
                'rounding': r.rounding, 'div_min_scale': r.div_min_scale,
                'avg_min_scale': r.avg_min_scale}
        if data.get('rules') != want:
            raise QueryFailure([EvalError(
                VERSION_MISMATCH, '-', 'agg:merge', '-',
                f"状态规则版本不一致: {data.get('rules')} != {want}")])
        states = GroupStates(plan)
        for g in data.get('groups', []):
            key = tuple(tuple(item) for item in g['key'])
            entry = [g['first_row_id'], []]
            for sd in g['states']:
                st = _State()
                st.mantissa = int(sd['mantissa'])
                st.count = sd['count']
                entry[1].append(st)
            states.groups[key] = entry
        return states


def _tag(t, v):
    if v is None:
        return ('n',)
    if isinstance(t, Bool):
        return ('b', v)
    if isinstance(t, Int64):
        return ('i', v)
    return ('d', v)  # decimal:尾数,scale 由该输出的类型固定


def evaluate(agg_plan, batch, engine='batch'):
    """对一批数据求分组状态;表达式错误直接以 QueryFailure 抛出。"""
    if engine == 'batch':
        res = batch_execute(agg_plan.expr_plan, batch)
    elif engine == 'row':
        res = row_execute(agg_plan.expr_plan, batch)
    else:
        raise ValueError(f'未知引擎: {engine!r}')
    if res.errors:
        raise QueryFailure(res.errors)
    plan = agg_plan.expr_plan
    group_types = [plan.output_type(g) for g in agg_plan.group_names]
    states = GroupStates(agg_plan)
    for rid in batch.row_ids:
        row = res.values[rid]
        key = tuple(_tag(t, row[g])
                    for t, g in zip(group_types, agg_plan.group_names))
        entry = states.groups.get(key)
        if entry is None:
            entry = [rid, [_State() for _ in agg_plan.aggs]]
            states.groups[key] = entry
        elif rid < entry[0]:
            entry[0] = rid
        for desc, st in zip(agg_plan.aggs, entry[1]):
            if desc.kind == COUNT:
                if desc.arg_output is None:
                    st.count += 1
                elif row[desc.arg_output] is not None:
                    st.count += 1
            else:
                v = row[desc.arg_output]
                if v is not None:
                    st.mantissa += v
                    st.count += 1
    return states


def merge(left, right):
    """合并两份状态;规则版本与登记类型必须一致,顺序不影响结果。"""
    if left.plan.rules != right.plan.rules:
        raise QueryFailure([EvalError(
            VERSION_MISMATCH, '-', 'agg:merge', '-',
            f'规则版本不一致: {left.plan.rules} 与 {right.plan.rules}')])
    if [ (d.kind, d.out_type, d.arg_scale) for d in left.plan.aggs] != \
            [(d.kind, d.out_type, d.arg_scale) for d in right.plan.aggs]:
        raise QueryFailure([EvalError(
            VERSION_MISMATCH, '-', 'agg:merge', '-', '聚合登记类型不一致')])
    merged = GroupStates(left.plan)
    merged.groups = copy.deepcopy(left.groups)
    for key, entry in right.groups.items():
        target = merged.groups.get(key)
        if target is None:
            merged.groups[key] = copy.deepcopy(entry)
            continue
        if entry[0] < target[0]:
            target[0] = entry[0]
        for st, other in zip(target[1], entry[1]):
            st.mantissa += other.mantissa
            st.count += other.count
    return merged


def evaluate_sharded(agg_plan, shards, engine='batch'):
    """逐分片求状态再合并;与单批执行结果相同。"""
    acc = None
    for shard in shards:
        states = evaluate(agg_plan, shard, engine=engine)
        acc = states if acc is None else merge(acc, states)
    return acc if acc is not None else GroupStates(agg_plan)


def finalize(agg_plan, states):
    """按登记输出类型检查并产生结果行;溢出在此刻、且只在此刻报错。

    分组按组内最小 row_id 排序输出,首个错误与分片、调度顺序无关。
    """
    if states.plan.rules != agg_plan.rules:
        raise QueryFailure([EvalError(
            VERSION_MISMATCH, '-', 'agg:finalize', '-', '规则版本不一致')])
    plan = agg_plan.expr_plan
    group_types = [plan.output_type(g) for g in agg_plan.group_names]
    rows = []
    errors = []
    for key in sorted(states.groups, key=lambda k: states.groups[k][0]):
        first_rid, sts = states.groups[key]
        out = {}
        for t, g, item in zip(group_types, agg_plan.group_names, key):
            out[g[2:]] = None if item[0] == 'n' else item[1]
        for desc, st in zip(agg_plan.aggs, sts):
            if desc.kind == COUNT:
                out[desc.name] = st.count
                continue
            if st.count == 0:
                out[desc.name] = None  # 全 null:SUM/AVG 为 null
                continue
            if desc.kind == SUM:
                m = st.mantissa
            else:  # AVG:精确商按 half-even 舍入到输出 scale
                numer = st.mantissa * 10 ** desc.out_type.scale
                denom = st.count * 10 ** desc.arg_scale
                m = round_quot_half_even(numer, denom)
            if isinstance(desc.out_type, Int64):
                ok = fits_int64(m)
            else:
                ok = fits_precision(m, desc.out_type.precision)
            if not ok:
                errors.append(EvalError(
                    OVERFLOW, first_rid, desc.position, desc.position,
                    f'{desc.kind} 结果超出登记类型 {desc.out_type}'))
                continue
            out[desc.name] = m
        rows.append(out)
    if errors:
        raise QueryFailure(errors)
    return rows
