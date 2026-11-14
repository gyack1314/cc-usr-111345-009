"""批引擎与行解释器的一致性、惰性边界、错误定位与确定性。

不变量:同一查询在不同批大小、不同输入重排下,按 row_id 核对的值、
错误集合、首个错误与每节点执行计数都必须一致。
"""
import random
import unittest

from decimal_data.query.batch import build_batch, permute_batch
from decimal_data.query.compiler import compile_projection
from decimal_data.query.engine_batch import execute as batch_execute
from decimal_data.query.engine_batch import execute_chunked
from decimal_data.query.engine_row import execute_rows
from decimal_data.query.errors import (DIVISION_BY_ZERO, INVALID_INPUT,
                                       OVERFLOW, QueryFailure)
from decimal_data.query.expr import parse_ast
from decimal_data.query.fptypes import BOOL, INT64, Decimal, Rules

SCHEMA = {'tokens': INT64, 'price': Decimal(12, 6),
          'enabled': BOOL, 'flag': BOOL}
RULES = Rules(version='v1')
I64_MAX = 2 ** 63 - 1


def E(obj):
    return parse_ast(obj)


def LIT(text, t):
    return {'literal': text, 'type': t}


def DIV(l, r):
    return {'op': 'divide', 'left': l, 'right': r}


def row(rid, tokens=None, price='0.000000', enabled=None, flag=None):
    return {'id': rid, 'tokens': tokens, 'price': price,
            'enabled': enabled, 'flag': flag}


def err_set(res):
    return sorted((e.row_id, e.category) for e in res.errors)


class EngineCase(unittest.TestCase):
    def run_both(self, named_exprs, rows, chunk_sizes=(1, 2, 3, 5, 1000)):
        plan = compile_projection(
            SCHEMA, RULES, {k: E(v) for k, v in named_exprs.items()})
        batch = build_batch(SCHEMA, RULES, rows)
        ref = execute_rows(plan, batch)
        whole = batch_execute(plan, batch)
        self.assertEqual(ref.values, whole.values, '整批与逐行值不一致')
        self.assertEqual(err_set(ref), err_set(whole), '整批与逐行错误不一致')
        self.assertEqual(ref.counts, whole.counts, '整批与逐行执行计数不一致')
        for cs in chunk_sizes:
            res = execute_chunked(plan, batch, cs)
            self.assertEqual(ref.values, res.values, f'chunk={cs} 值不一致')
            self.assertEqual(err_set(ref), err_set(res), f'chunk={cs} 错误不一致')
            self.assertEqual(ref.counts, res.counts, f'chunk={cs} 计数不一致')
        return plan, ref


class LazyCaseTests(EngineCase):
    EXPR = {'op': 'case', 'when': {'column': 'enabled'},
            'then': {'column': 'tokens'},
            'else': DIV(LIT('1', 'int64'), LIT('0', 'int64'))}

    def test_error_branch_selected_per_row(self):
        plan, res = self.run_both({'r': self.EXPR}, [
            row('r1', tokens=7, enabled=True),
            row('r2', tokens=7, enabled=False),
            row('r3', tokens=7, enabled=None)])
        self.assertEqual(res.values['r1']['r'], 7 * 10 ** 19)
        self.assertEqual(err_set(res),
                         [('r2', DIVISION_BY_ZERO), ('r3', DIVISION_BY_ZERO)])
        div_nid = next(i for i, n in enumerate(plan.nodes) if n.op == 'divide')
        self.assertEqual(res.counts[div_nid], 2)  # 只对 r2/r3 求值

    def test_error_branch_never_selected(self):
        plan, res = self.run_both({'r': self.EXPR}, [
            row('r1', tokens=1, enabled=True),
            row('r2', tokens=2, enabled=True)])
        self.assertEqual(res.errors, [])
        div_nid = next(i for i, n in enumerate(plan.nodes) if n.op == 'divide')
        self.assertEqual(res.counts[div_nid], 0)  # 从未求值

    def test_null_condition_goes_else(self):
        _, res = self.run_both(
            {'r': {'op': 'case', 'when': {'column': 'enabled'},
                   'then': LIT('1', 'int64'), 'else': LIT('2', 'int64')}},
            [row('r1', enabled=None), row('r2', enabled=False),
             row('r3', enabled=True)])
        self.assertEqual(res.values['r1']['r'], 2)
        self.assertEqual(res.values['r2']['r'], 2)
        self.assertEqual(res.values['r3']['r'], 1)


class CoalesceTests(EngineCase):
    def test_stops_at_first_non_null(self):
        expr = {'op': 'coalesce',
                'args': [{'column': 'tokens'},
                         DIV(LIT('1', 'int64'), LIT('0', 'int64'))]}
        plan, res = self.run_both({'r': expr}, [
            row('r1', tokens=5), row('r2', tokens=None)])
        self.assertEqual(res.values['r1']['r'], 5 * 10 ** 19)
        self.assertEqual(err_set(res), [('r2', DIVISION_BY_ZERO)])
        div_nid = next(i for i, n in enumerate(plan.nodes) if n.op == 'divide')
        self.assertEqual(res.counts[div_nid], 1)  # 只有 r2 需要第二参数

    def test_all_null_gives_null(self):
        expr = {'op': 'coalesce',
                'args': [LIT(None, 'null'), {'column': 'tokens'}]}
        _, res = self.run_both({'r': expr}, [row('r1', tokens=None)])
        self.assertIsNone(res.values['r1']['r'])


class ShortCircuitTests(EngineCase):
    ERR_CMP = {'op': 'gt', 'left': DIV(LIT('1', 'int64'), LIT('0', 'int64')),
               'right': LIT('0', 'int64')}

    def test_and_short_circuit_on_false(self):
        expr = {'op': 'and', 'left': {'column': 'enabled'},
                'right': self.ERR_CMP}
        _, res = self.run_both({'r': expr}, [
            row('r1', enabled=False),   # False,右側不求值
            row('r2', enabled=True),    # 必须求值 -> 错误
            row('r3', enabled=None)])   # null 不能决定 AND -> 错误
        self.assertIs(res.values['r1']['r'], False)
        self.assertEqual(err_set(res), [('r2', DIVISION_BY_ZERO),
                                        ('r3', DIVISION_BY_ZERO)])

    def test_or_short_circuit_on_true(self):
        expr = {'op': 'or', 'left': {'column': 'enabled'},
                'right': self.ERR_CMP}
        _, res = self.run_both({'r': expr}, [
            row('r1', enabled=True),    # True,右側不求值
            row('r2', enabled=False),   # 必须求值 -> 错误
            row('r3', enabled=None)])   # null 不能决定 OR -> 错误
        self.assertIs(res.values['r1']['r'], True)
        self.assertEqual(err_set(res), [('r2', DIVISION_BY_ZERO),
                                        ('r3', DIVISION_BY_ZERO)])

    def test_three_valued_results(self):
        expr = {'a': {'op': 'and', 'left': {'column': 'enabled'},
                      'right': {'column': 'flag'}},
                'o': {'op': 'or', 'left': {'column': 'enabled'},
                      'right': {'column': 'flag'}},
                'n': {'op': 'not', 'arg': {'column': 'enabled'}}}
        _, res = self.run_both(expr, [
            row('t_f', enabled=True, flag=False),
            row('n_f', enabled=None, flag=False),
            row('n_t', enabled=None, flag=True),
            row('n_n', enabled=None, flag=None)])
        v = res.values
        self.assertIs(v['t_f']['a'], False)
        self.assertIs(v['t_f']['o'], True)
        self.assertIs(v['n_f']['a'], False)   # null AND false = false
        self.assertIs(v['n_f']['o'], None)    # null OR false = null
        self.assertIs(v['n_t']['a'], None)
        self.assertIs(v['n_t']['o'], True)
        self.assertIs(v['n_n']['a'], None)
        self.assertIs(v['n_n']['n'], None)


class NullSemanticsTests(EngineCase):
    def test_null_propagates_without_error(self):
        exprs = {'a': {'op': 'add', 'left': {'column': 'price'},
                       'right': LIT(None, 'null')},
                 'c': {'op': 'eq', 'left': LIT(None, 'null'),
                       'right': LIT('1', 'int64')},
                 'd': {'op': 'divide', 'left': {'column': 'price'},
                       'right': LIT(None, 'null')}}
        _, res = self.run_both(exprs, [row('r1', price='1.500000')])
        self.assertEqual(res.errors, [])
        for out in res.values['r1'].values():
            self.assertIsNone(out)

    def test_explicit_null_is_not_missing_column(self):
        rows = [row('r1', price=None)]  # 显式 null:合法
        plan = compile_projection(SCHEMA, RULES, {'r': E({'column': 'price'})})
        batch = build_batch(SCHEMA, RULES, rows)
        self.assertIsNone(batch_execute(plan, batch).values['r1']['r'])
        bad = [{'id': 'r2', 'tokens': 1, 'enabled': True, 'flag': None}]
        with self.assertRaises(QueryFailure) as cm:
            build_batch(SCHEMA, RULES, bad)  # 缺 price 列:资料错误
        self.assertEqual(cm.exception.first.category, INVALID_INPUT)
        self.assertIn('缺列', cm.exception.first.detail)

    def test_float_input_rejected(self):
        with self.assertRaises(QueryFailure):
            build_batch(SCHEMA, RULES, [row('r1', price=0.1)])


class OverflowTests(EngineCase):
    def test_int64_overflow_is_error_not_saturate(self):
        expr = {'op': 'add', 'left': {'column': 'tokens'},
                'right': {'column': 'tokens'}}
        _, res = self.run_both({'r': expr}, [
            row('r1', tokens=I64_MAX), row('r2', tokens=3)])
        self.assertNotIn('r1', res.values)          # 不饱和、不变 null
        self.assertEqual(res.values['r2']['r'], 6)
        self.assertEqual(err_set(res), [('r1', OVERFLOW)])

    def test_divide_by_zero_reports_row_and_position(self):
        expr = DIV(LIT('10', 'int64'), {'column': 'tokens'})
        plan, res = self.run_both({'r': expr}, [
            row('r9', tokens=0), row('r2', tokens=2)])
        self.assertEqual(err_set(res), [('r9', DIVISION_BY_ZERO)])
        err = res.errors[0]
        self.assertEqual(err.row_id, 'r9')
        self.assertTrue(err.position.startswith('#'))
        node = plan.nodes[int(err.position[1:])]
        self.assertEqual(node.op, 'divide')
        self.assertTrue(plan.paths[node].startswith('$'))


class CastRoundingTests(EngineCase):
    def test_half_even_midpoints_exact(self):
        expr = {'op': 'cast', 'arg': {'column': 'price'},
                'type': 'decimal(12,2)'}
        _, res = self.run_both({'r': expr}, [
            row('a', price='2.675000'),   # 中点 -> 2.68(267.5 取偶 268)
            row('b', price='2.685000'),   # 中点 -> 2.68
            row('c', price='2.695000'),   # 中点 -> 2.70
            row('d', price='-2.675000'),  # 中点 -> -2.68
            row('e', price='0.005000')])  # 中点 -> 0.00
        self.assertEqual(res.values['a']['r'], 268)
        self.assertEqual(res.values['b']['r'], 268)
        self.assertEqual(res.values['c']['r'], 270)
        self.assertEqual(res.values['d']['r'], -268)
        self.assertEqual(res.values['e']['r'], 0)
        # 对照:float 路径会把 2.675 舍成 2.67,这里必须是 2.68
        self.assertNotEqual(round(float('2.675'), 2), 2.68)


class SharedSubexpressionTests(EngineCase):
    def test_shared_node_computed_once_and_not_beyond_lazy_boundary(self):
        add = {'op': 'add', 'left': {'column': 'tokens'},
               'right': LIT('1', 'int64')}
        exprs = {'y1': {'op': 'case', 'when': {'column': 'enabled'},
                        'then': add, 'else': LIT('0', 'int64')},
                 'y2': {'op': 'case', 'when': {'column': 'flag'},
                        'then': add, 'else': LIT('0', 'int64')}}
        plan, res = self.run_both(exprs, [
            row('r1', tokens=10, enabled=True, flag=False),
            row('r2', tokens=20, enabled=False, flag=True),
            row('r3', tokens=I64_MAX, enabled=None, flag=None)])
        adds = [i for i, n in enumerate(plan.nodes) if n.op == 'add']
        self.assertEqual(len(adds), 1, '公共子表达式应共享为一个节点')
        # r1/r2 各需求一次;r3 两个分支都不选中,绝不求值(否则溢出)
        self.assertEqual(res.counts[adds[0]], 2)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.values['r1']['y1'], 11)
        self.assertEqual(res.values['r2']['y2'], 21)
        self.assertEqual(res.values['r3']['y1'], 0)
        self.assertEqual(res.values['r3']['y2'], 0)


class FirstErrorDeterminismTests(EngineCase):
    EXPRS = {'o1': DIV(LIT('1', 'int64'), {'column': 'tokens'}),
             'o2': {'op': 'add', 'left': {'column': 'tokens'},
                    'right': LIT(str(I64_MAX), 'int64')}}

    def test_first_error_independent_of_order_and_chunking(self):
        rows = [row('r1', tokens=5),   # o2 溢出
                row('r2', tokens=0)]   # o1 除零
        plan = compile_projection(SCHEMA, RULES,
                                  {k: E(v) for k, v in self.EXPRS.items()})
        batch = build_batch(SCHEMA, RULES, rows)
        expected = None
        for order in ([0, 1], [1, 0]):
            shuffled = permute_batch(batch, order)
            for cs in (1, 2, 1000):
                res = execute_chunked(plan, shuffled, cs)
                first = QueryFailure(res.errors).first
                key = (first.row_id, first.position, first.category)
                if expected is None:
                    expected = key
                self.assertEqual(key, expected)
        self.assertEqual(expected[0], 'r1')   # 按 row_id 排序的首个错误
        self.assertEqual(expected[2], OVERFLOW)


class ReorderTests(EngineCase):
    def test_results_keyed_by_row_id_survive_shuffle(self):
        exprs = {'amt': {'op': 'case', 'when': {'column': 'enabled'},
                         'then': {'op': 'multiply',
                                  'left': {'column': 'tokens'},
                                  'right': {'column': 'price'}},
                         'else': LIT('0.000000', 'decimal(12,6)')},
                 'ok': {'op': 'or', 'left': {'column': 'enabled'},
                        'right': {'column': 'flag'}}}
        rows = [row(f'r{i}', tokens=i * 7 - 10, price=f'0.00000{i}',
                    enabled=[True, False, None][i % 3],
                    flag=[None, True, False][i % 3]) for i in range(1, 9)]
        plan = compile_projection(SCHEMA, RULES,
                                  {k: E(v) for k, v in exprs.items()})
        batch = build_batch(SCHEMA, RULES, rows)
        ref = execute_rows(plan, batch)
        rng = random.Random(20261114)
        for _ in range(5):
            order = list(range(len(rows)))
            rng.shuffle(order)
            shuffled = permute_batch(batch, order)
            for cs in (1, 3, 8):
                res = execute_chunked(plan, shuffled, cs)
                self.assertEqual(ref.values, res.values)
                self.assertEqual(err_set(ref), err_set(res))
                self.assertEqual(ref.counts, res.counts)


class NestedLazyTests(EngineCase):
    def test_coalesce_wrapping_case_with_error_branch(self):
        expr = {'op': 'coalesce', 'args': [
            {'op': 'case', 'branches': [
                {'when': {'column': 'enabled'}, 'then': {'column': 'tokens'}},
                {'when': {'column': 'flag'},
                 'then': DIV(LIT('1', 'int64'), LIT('0', 'int64'))}],
             'else': LIT(None, 'null')},
            LIT('9', 'int64')]}
        plan, res = self.run_both({'r': expr}, [
            row('a', tokens=3, price=None, enabled=True, flag=None),
            row('b', tokens=None, price=None, enabled=False, flag=False),
            row('c', tokens=None, price=None, enabled=None, flag=None)])
        self.assertEqual(res.errors, [])  # 除零分支从未被选中
        self.assertEqual(res.values['a']['r'], 3 * 10 ** 19)
        self.assertEqual(res.values['b']['r'], 9 * 10 ** 19)
        self.assertEqual(res.values['c']['r'], 9 * 10 ** 19)
        div_nid = next(i for i, n in enumerate(plan.nodes) if n.op == 'divide')
        self.assertEqual(res.counts[div_nid], 0)


class LoadRoundingTests(EngineCase):
    def test_load_applies_half_even_to_column_type(self):
        batch = build_batch(SCHEMA, RULES, [
            row('a', price='0.0000005'),   # 超出列 scale,中点取偶 -> 0
            row('b', price='0.0000015')])  # 中点取偶 -> 0.000002
        self.assertEqual(batch.columns['price'], (0, 2))

    def test_division_result_type_always_contains_quotient(self):
        # 结果类型的整数部分按 (p1-s1)+s2 构造,最大商必能容纳
        schema = {'x': Decimal(38, 0), 'y': Decimal(38, 0)}
        plan = compile_projection(schema, RULES, {'r': E(
            DIV({'column': 'x'}, {'column': 'y'}))})
        batch = build_batch(schema, RULES, [
            {'id': 'a', 'x': '9' * 38, 'y': '1'}])
        res = batch_execute(plan, batch)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.values['a']['r'], 10 ** 38 - 1)


class CostScenarioTests(EngineCase):
    def test_tiny_price_times_large_tokens_exact(self):
        # 成本仓场景:0.000037 * 123456,float 得 4.5678719999999995
        expr = {'op': 'multiply', 'left': {'column': 'tokens'},
                'right': {'column': 'price'}}
        _, res = self.run_both({'r': expr}, [row('r1', tokens=123456,
                                                 price='0.000037')])
        self.assertEqual(res.values['r1']['r'], 4567872)  # 4.567872,scale 6
        self.assertNotEqual(float('0.000037') * 123456, 4.567872)


if __name__ == '__main__':
    unittest.main()
