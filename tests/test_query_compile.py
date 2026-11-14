"""冻结的类型推导:结果类型、提升规则、编译期拒绝。"""
import unittest

from decimal_data.query.compiler import compile_projection
from decimal_data.query.errors import (INVALID_LITERAL, PRECISION_EXCEEDED,
                                       TYPE_MISMATCH, UNKNOWN_COLUMN,
                                       UNSUPPORTED_TYPES, CompileError)
from decimal_data.query.expr import parse_ast
from decimal_data.query.fptypes import BOOL, INT64, Decimal, Rules

SCHEMA = {'tokens': INT64, 'price': Decimal(12, 6), 'enabled': BOOL}
RULES = Rules(version='v1')


def E(obj):
    return parse_ast(obj)


def LIT(text, t):
    return {'literal': text, 'type': t}


def plan_of(expr, schema=SCHEMA, rules=RULES):
    return compile_projection(schema, rules, {'r': E(expr)})


def type_of(expr, schema=SCHEMA, rules=RULES):
    return plan_of(expr, schema, rules).output_type('r')


class ResultTypeTests(unittest.TestCase):
    def test_arithmetic_result_types(self):
        mul = {'op': 'multiply', 'left': {'column': 'tokens'},
               'right': {'column': 'price'}}
        self.assertEqual(type_of(mul), Decimal(32, 6))  # 19+12+1, 0+6
        add = {'op': 'add', 'left': {'column': 'price'},
               'right': {'column': 'price'}}
        self.assertEqual(type_of(add), Decimal(13, 6))  # max(6,6)+6+1
        div = {'op': 'divide', 'left': {'column': 'price'},
               'right': {'column': 'price'}}
        # s = max(6, 6+12+1) = 19, p = 6+6+19 = 31
        self.assertEqual(type_of(div), Decimal(31, 19))
        div_i = {'op': 'divide', 'left': {'column': 'tokens'},
                 'right': {'column': 'tokens'}}
        # s = max(6, 0+19+1) = 20, p = 19+0+20 = 39 -> 压缩到 decimal(38,19)
        self.assertEqual(type_of(div_i), Decimal(38, 19))
        div_m = {'op': 'divide', 'left': {'column': 'price'},
                 'right': {'column': 'tokens'}}
        # s = max(6, 6+19+1) = 26, p = 6+0+26 = 32
        self.assertEqual(type_of(div_m), Decimal(32, 26))

    def test_int64_stays_int64_except_divide(self):
        for op in ('add', 'subtract', 'multiply'):
            e = {'op': op, 'left': {'column': 'tokens'},
                 'right': {'column': 'tokens'}}
            self.assertEqual(type_of(e), INT64, op)
        mixed = {'op': 'add', 'left': {'column': 'tokens'},
                 'right': {'column': 'price'}}
        self.assertEqual(type_of(mixed), Decimal(26, 6))  # max(19,6)+6+1

    def test_case_and_coalesce_common_type(self):
        case = {'op': 'case', 'when': {'column': 'enabled'},
                'then': {'column': 'price'},
                'else': LIT('0.00', 'decimal(10,2)')}
        self.assertEqual(type_of(case), Decimal(14, 6))  # max(6,8)+6
        coal = {'op': 'coalesce',
                'args': [{'column': 'tokens'}, {'column': 'price'}]}
        self.assertEqual(type_of(coal), Decimal(25, 6))  # max(19,6)+6
        no_else = {'op': 'case', 'when': {'column': 'enabled'},
                   'then': {'column': 'tokens'}}
        self.assertEqual(type_of(no_else), INT64)  # 缺省 ELSE 是 null

    def test_comparison_result_bool(self):
        e = {'op': 'le', 'left': {'column': 'price'},
             'right': {'column': 'tokens'}}
        self.assertEqual(type_of(e), BOOL)
        e = {'op': 'ne', 'left': {'column': 'enabled'},
             'right': {'column': 'enabled'}}
        self.assertEqual(type_of(e), BOOL)

    def test_explain_lists_full_type_plan(self):
        p = plan_of({'op': 'multiply', 'left': {'column': 'tokens'},
                     'right': {'column': 'price'}})
        text = p.explain()
        self.assertIn('decimal(32,6)', text)
        self.assertIn('[implicit]', text)  # tokens -> decimal(19,0)
        self.assertIn('version=v1', text)


class CompileRejectionTests(unittest.TestCase):
    def assert_compile_fails(self, expr, category):
        with self.assertRaises(CompileError) as cm:
            plan_of(expr)
        self.assertEqual(cm.exception.category, category, cm.exception)

    def test_precision_exceeded(self):
        big = {'op': 'cast', 'arg': {'column': 'price'},
               'type': 'decimal(38,0)'}
        add = {'op': 'add', 'left': big, 'right': big}
        self.assert_compile_fails(add, PRECISION_EXCEEDED)  # p=39
        mul = {'op': 'multiply',
               'left': {'op': 'cast', 'arg': {'column': 'price'},
                        'type': 'decimal(20,10)'},
               'right': {'op': 'cast', 'arg': {'column': 'price'},
                         'type': 'decimal(20,10)'}}
        self.assert_compile_fails(mul, PRECISION_EXCEEDED)  # p=41
        div = {'op': 'divide',
               'left': {'op': 'cast', 'arg': {'column': 'price'},
                        'type': 'decimal(38,0)'},
               'right': {'op': 'cast', 'arg': {'column': 'price'},
                         'type': 'decimal(38,20)'}}
        self.assert_compile_fails(div, PRECISION_EXCEEDED)  # 整数部分 58 位
        self.assert_compile_fails(
            {'op': 'cast', 'arg': {'column': 'price'}, 'type': 'decimal(39,0)'},
            PRECISION_EXCEEDED)

    def test_type_mismatch(self):
        self.assert_compile_fails(
            {'op': 'add', 'left': {'column': 'enabled'},
             'right': {'column': 'tokens'}}, TYPE_MISMATCH)
        self.assert_compile_fails(
            {'op': 'and', 'left': {'column': 'enabled'},
             'right': {'column': 'price'}}, TYPE_MISMATCH)
        self.assert_compile_fails(
            {'op': 'lt', 'left': {'column': 'enabled'},
             'right': {'column': 'enabled'}}, TYPE_MISMATCH)
        self.assert_compile_fails(
            {'op': 'case', 'when': {'column': 'enabled'},
             'then': {'column': 'enabled'},
             'else': {'column': 'tokens'}}, TYPE_MISMATCH)

    def test_unsupported_cast(self):
        self.assert_compile_fails(
            {'op': 'cast', 'arg': {'column': 'enabled'}, 'type': 'int64'},
            UNSUPPORTED_TYPES)
        self.assert_compile_fails(
            {'op': 'cast', 'arg': {'column': 'tokens'}, 'type': 'bool'},
            UNSUPPORTED_TYPES)

    def test_unknown_column(self):
        self.assert_compile_fails({'column': 'nope'}, UNKNOWN_COLUMN)

    def test_invalid_literal(self):
        self.assert_compile_fails(LIT('999', 'decimal(2,0)'), INVALID_LITERAL)
        self.assert_compile_fails(LIT('1e3', 'decimal(12,2)'), INVALID_LITERAL)
        self.assert_compile_fails(LIT('1.5', 'int64'), INVALID_LITERAL)
        self.assert_compile_fails(
            LIT('9223372036854775808', 'int64'), INVALID_LITERAL)
        self.assert_compile_fails(LIT('x', 'bool'), INVALID_LITERAL)

    def test_literal_rounding_at_compile_is_half_even(self):
        p = plan_of(LIT('0.005', 'decimal(12,2)'))
        consts = [n for n in p.nodes if n.op == 'const']
        self.assertEqual(consts[0].value, 0)   # 0.005 -> 0.00(0 为偶)
        p = plan_of(LIT('0.015', 'decimal(12,2)'))
        consts = [n for n in p.nodes if n.op == 'const']
        self.assertEqual(consts[0].value, 2)   # 0.015 -> 0.02

    def test_rules_version_changes_type_plan(self):
        e = {'op': 'divide', 'left': {'column': 'tokens'},
             'right': {'column': 'tokens'}}
        # 上限 20:除法规则压缩 scale 到 1,仍可编译
        narrow = Rules(version='v2-narrow', maximum_precision=20)
        self.assertEqual(type_of(e, rules=narrow), Decimal(20, 1))
        # 上限 10:整数部分 19 位已超上限,编译期拒绝
        tiny = Rules(version='v3-tiny', maximum_precision=10)
        with self.assertRaises(CompileError):
            type_of(e, rules=tiny)
        self.assertEqual(type_of(e), Decimal(38, 19))


if __name__ == '__main__':
    unittest.main()
