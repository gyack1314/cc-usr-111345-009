"""登记资料包的端到端执行:normal 与 boundary 两个独立资料包。"""
import unittest

from decimal_data.io import load
from decimal_data.query import api
from decimal_data.query.batch import permute_batch
from decimal_data.query.engine_batch import execute_chunked
from decimal_data.query.render import format_value


class DataPackageTests(unittest.TestCase):
    def check_package(self, path, expected):
        tables = load(path)
        plan = api.compile_expression(tables, 'e1')
        self.assertEqual(str(plan.output_type('result')), 'decimal(32,6)')
        batch = api.batch_of(tables)
        ref = api.run_expression(tables, 'e1', engine='row')
        for cs in (1, 2, 3, 100):
            res = execute_chunked(plan, batch, cs)
            self.assertEqual(ref.values, res.values)
            self.assertEqual(ref.counts, res.counts)
            self.assertEqual(ref.errors, res.errors)
        got = {rid: format_value(plan.output_type('result'), v['result'])
               for rid, v in ref.values.items()}
        self.assertEqual(got, expected)
        # 重排后按 row_id 核对不变
        shuffled = permute_batch(batch, [2, 0, 1])
        res = execute_chunked(plan, shuffled, 1)
        self.assertEqual(ref.values, res.values)
        return plan, ref

    def test_normal_package(self):
        # r2 enabled=false -> ELSE;r3 enabled=null -> WHEN 不为真 -> ELSE
        self.check_package('data/normal.json', {
            'r1': '125.000000', 'r2': '0.000000', 'r3': '0.000000'})

    def test_boundary_package(self):
        # r2 enabled=true 且 tokens=0 -> 0 * 0.000125 = 0,精确零
        self.check_package('data/boundary.json', {
            'r1': '125.000000', 'r2': '0.000000', 'r3': '0.000000'})

    def test_lazy_counts_on_package(self):
        tables = load('data/normal.json')
        plan = api.compile_expression(tables, 'e1')
        res = api.run_expression(tables, 'e1')
        mul = next(i for i, n in enumerate(plan.nodes) if n.op == 'multiply')
        self.assertEqual(res.counts[mul], 1)  # 只有 r1 选中 THEN 分支

    def test_explain_shows_registered_types(self):
        tables = load('data/normal.json')
        text = api.compile_expression(tables, 'e1').explain()
        self.assertIn('decimal(32,6)', text)
        self.assertIn('decimal(12,6)', text)
        self.assertIn('version=v1', text)


if __name__ == '__main__':
    unittest.main()
