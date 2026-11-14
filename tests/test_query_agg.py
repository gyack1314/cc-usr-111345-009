"""分组 SUM/COUNT/AVG 的定点状态:宽累积器、延迟检查、分片合并。"""
import json
import unittest

from decimal_data.query.agg import (AggSpec, GroupStates, compile_aggregation,
                                    evaluate, evaluate_sharded, finalize,
                                    merge)
from decimal_data.query.batch import build_batch, permute_batch, split_batch
from decimal_data.query.errors import (OVERFLOW, TYPE_MISMATCH, VERSION_MISMATCH,
                                       CompileError, QueryFailure)
from decimal_data.query.expr import parse_ast
from decimal_data.query.fptypes import BOOL, INT64, Decimal, Rules

RULES = Rules(version='v1')
SCHEMA = {'g': INT64, 'x': Decimal(12, 6), 'n': INT64, 'flag': BOOL}
BIG_SCHEMA = {'g': INT64, 'x': Decimal(38, 0)}
I64_MAX = 2 ** 63 - 1
NINES = '9' * 38  # 10^38 - 1,decimal(38,0) 的最大值


def E(obj):
    return parse_ast(obj)


def COL(name):
    return {'column': name}


def make_plan(aggs, schema=SCHEMA, rules=RULES, group_by=None):
    return compile_aggregation(
        schema, rules, group_by if group_by is not None else {'g': E(COL('g'))},
        aggs)


def basic_plan():
    return make_plan({'s': AggSpec('sum', E(COL('x'))),
                      'c': AggSpec('count', E(COL('x'))),
                      'cs': AggSpec('count'),
                      'a': AggSpec('avg', E(COL('x')))})


def rows_of(entries):
    return [{'id': rid, 'g': g, 'x': x, 'n': n, 'flag': fl}
            for rid, g, x, n, fl in entries]


BASIC_ROWS = rows_of([
    ('r1', 1, '1.500000', 10, True),
    ('r2', 1, '2.500000', 20, None),
    ('r3', 1, None, 30, False),
    ('r4', 2, '0.000003', 40, None),
    ('r5', 2, '0.000003', 50, True),
    ('r6', 3, None, None, None),
])


class BasicAggTests(unittest.TestCase):
    def test_sum_count_avg_by_group(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        rows = finalize(plan, evaluate(plan, batch))
        self.assertEqual([r['g'] for r in rows], [1, 2, 3])
        g1, g2, g3 = rows
        self.assertEqual(g1['s'], 4000000)      # 4.000000,scale 6
        self.assertEqual(g1['c'], 2)            # null 不计入 count(x)
        self.assertEqual(g1['cs'], 3)           # count(*) 计全部行
        self.assertEqual(g1['a'], 2000000)      # 2.000000
        self.assertEqual(g2['s'], 6)            # 0.000006
        self.assertEqual(g2['a'], 3)            # 0.000003
        self.assertIsNone(g3['s'])              # 全 null:SUM 为 null
        self.assertIsNone(g3['a'])              # 全 null:AVG 为 null
        self.assertEqual(g3['c'], 0)
        self.assertEqual(g3['cs'], 1)

    def test_registered_output_types(self):
        plan = make_plan({'s': AggSpec('sum', E(COL('x'))),
                          'a': AggSpec('avg', E(COL('x'))),
                          'sn': AggSpec('sum', E(COL('n'))),
                          'an': AggSpec('avg', E(COL('n')))})
        types = {d.name: d.out_type for d in plan.aggs}
        self.assertEqual(types['s'], Decimal(38, 6))
        self.assertEqual(types['a'], Decimal(38, 6))
        self.assertEqual(types['sn'], INT64)
        self.assertEqual(types['an'], Decimal(38, 6))

    def test_engines_agree(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        a = finalize(plan, evaluate(plan, batch, engine='batch'))
        b = finalize(plan, evaluate(plan, batch, engine='row'))
        self.assertEqual(a, b)

    def test_empty_input_gives_no_groups(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, [])
        self.assertEqual(finalize(plan, evaluate(plan, batch)), [])

    def test_null_group_key_groups_together(self):
        plan = make_plan({'cs': AggSpec('count')})
        rows = rows_of([('r1', None, '1.000000', 1, None),
                        ('r2', None, '2.000000', 2, None),
                        ('r3', 7, '3.000000', 3, None)])
        batch = build_batch(SCHEMA, RULES, rows)
        out = finalize(plan, evaluate(plan, batch))
        self.assertEqual([(r['g'], r['cs']) for r in out],
                         [(None, 2), (7, 1)])

    def test_sum_of_bool_rejected(self):
        with self.assertRaises(CompileError) as cm:
            make_plan({'s': AggSpec('sum', E(COL('flag')))})
        self.assertEqual(cm.exception.category, TYPE_MISMATCH)

    def test_group_by_arbitrary_expression(self):
        plan = compile_aggregation(
            SCHEMA, RULES,
            {'parity': E({'op': 'case', 'when': {'column': 'flag'},
                          'then': {'literal': '1', 'type': 'int64'},
                          'else': {'literal': '0', 'type': 'int64'}})},
            {'cs': AggSpec('count')})
        rows = rows_of([('r1', 1, None, 1, True), ('r2', 2, None, 2, False),
                        ('r3', 3, None, 3, None), ('r4', 4, None, 4, True)])
        batch = build_batch(SCHEMA, RULES, rows)
        out = finalize(plan, evaluate(plan, batch))
        self.assertEqual([(r['parity'], r['cs']) for r in out],
                         [(1, 2), (0, 2)])  # null 条件走 ELSE


class CancellationTests(unittest.TestCase):
    """前缀超出输出范围但完整和可表示时不得提前报错。"""

    def rows(self, values):
        return [{'id': f'r{i}', 'g': 1, 'x': v}
                for i, v in enumerate(values, 1)]

    def test_prefix_overflow_then_cancel(self):
        plan = make_plan({'s': AggSpec('sum', E(COL('x')))}, schema=BIG_SCHEMA)
        rows = self.rows([NINES, '1', '-' + NINES, '-1'])
        batch = build_batch(BIG_SCHEMA, RULES, rows)
        for sizes in ([4], [2, 2], [1, 3], [1, 1, 1, 1], [3, 1]):
            states = evaluate_sharded(plan, split_batch(batch, sizes))
            self.assertEqual(finalize(plan, states)[0]['s'], 0,
                             f'分片 {sizes} 结果应为 0')

    def test_final_overflow_cannot_be_bypassed_by_sharding(self):
        plan = make_plan({'s': AggSpec('sum', E(COL('x')))}, schema=BIG_SCHEMA)
        rows = self.rows([NINES, '1'])  # 和为 10^38,超出 decimal(38,0)
        batch = build_batch(BIG_SCHEMA, RULES, rows)
        firsts = []
        for sizes in ([2], [1, 1]):
            states = evaluate_sharded(plan, split_batch(batch, sizes))
            with self.assertRaises(QueryFailure) as cm:
                finalize(plan, states)
            err = cm.exception.first
            self.assertEqual(err.category, OVERFLOW)
            firsts.append((err.row_id, err.position, err.category))
        self.assertEqual(firsts[0], firsts[1])  # 分片不改变首个错误

    def test_int64_sum_overflow_at_finalize_only(self):
        plan = make_plan({'s': AggSpec('sum', E(COL('n')))})
        rows = rows_of([('r2', 1, None, I64_MAX, None),
                        ('r1', 1, None, I64_MAX, None)])
        batch = build_batch(SCHEMA, RULES, rows)
        states = evaluate(plan, batch)  # 累积不报错
        with self.assertRaises(QueryFailure) as cm:
            finalize(plan, states)
        self.assertEqual(cm.exception.first.category, OVERFLOW)
        self.assertEqual(cm.exception.first.row_id, 'r1')  # 组内最小 row_id
        self.assertEqual(cm.exception.first.position, 'agg:s')


class AvgRoundingTests(unittest.TestCase):
    def test_avg_half_even_midpoints(self):
        plan = make_plan({'a': AggSpec('avg', E(COL('x')))})
        rows = rows_of([('r1', 1, '0.000001', None, None),
                        ('r2', 1, '0.000000', None, None),
                        ('r3', 2, '0.000001', None, None),
                        ('r4', 2, '0.000002', None, None)])
        batch = build_batch(SCHEMA, RULES, rows)
        out = finalize(plan, evaluate(plan, batch))
        # 尾数和 1 / 行数 2 = 0.5 -> 取偶 0;3 / 2 = 1.5 -> 取偶 2
        self.assertEqual([r['a'] for r in out], [0, 2])

    def test_avg_exact(self):
        plan = make_plan({'a': AggSpec('avg', E(COL('n')))})
        rows = rows_of([('r1', 1, None, 1, None), ('r2', 1, None, 2, None)])
        batch = build_batch(SCHEMA, RULES, rows)
        out = finalize(plan, evaluate(plan, batch))
        self.assertEqual(out[0]['a'], 1500000)  # 1.500000,scale 6


class MergeTests(unittest.TestCase):
    def test_merge_order_irrelevant(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        shards = split_batch(batch, [2, 3, 1])
        forward = evaluate_sharded(plan, shards)
        backward = evaluate_sharded(plan, list(reversed(shards)))
        self.assertEqual(finalize(plan, forward), finalize(plan, backward))
        whole = finalize(plan, evaluate(plan, batch))
        self.assertEqual(finalize(plan, forward), whole)

    def test_merge_rejects_version_mismatch(self):
        plan_v1 = basic_plan()
        plan_v2 = compile_aggregation(
            SCHEMA, Rules(version='v2'),
            {'g': E(COL('g'))}, {'s': AggSpec('sum', E(COL('x')))})
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        s1 = evaluate(plan_v1, batch)
        s2 = evaluate(plan_v2, batch)
        with self.assertRaises(QueryFailure) as cm:
            merge(s1, s2)
        self.assertEqual(cm.exception.first.category, VERSION_MISMATCH)

    def test_state_serialization_roundtrip(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        states = evaluate(plan, batch)
        data = json.loads(json.dumps(states.to_dict()))
        restored = GroupStates.from_dict(plan, data)
        self.assertEqual(finalize(plan, states), finalize(plan, restored))

    def test_from_dict_rejects_foreign_rules(self):
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        data = evaluate(plan, batch).to_dict()
        data['rules']['version'] = 'v9'
        with self.assertRaises(QueryFailure) as cm:
            GroupStates.from_dict(plan, data)
        self.assertEqual(cm.exception.first.category, VERSION_MISMATCH)


class ReorderAggTests(unittest.TestCase):
    def test_finalize_independent_of_input_order(self):
        import random
        plan = basic_plan()
        batch = build_batch(SCHEMA, RULES, BASIC_ROWS)
        want = finalize(plan, evaluate(plan, batch))
        rng = random.Random(7)
        for _ in range(5):
            order = list(range(len(BASIC_ROWS)))
            rng.shuffle(order)
            shuffled = permute_batch(batch, order)
            got = finalize(plan, evaluate(plan, shuffled))
            self.assertEqual(got, want)


if __name__ == '__main__':
    unittest.main()
