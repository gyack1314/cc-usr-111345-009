"""十进制精确读取、half-even 舍入与标量内核的数值依据。"""
import unittest

from decimal_data.query.errors import DIVISION_BY_ZERO, OVERFLOW, KernelError
from decimal_data.query.fpdecimal import (fits_precision, parse_decimal,
                                          rescale, round_quot_half_even)
from decimal_data.query.fptypes import Decimal, Int64
from decimal_data.query.kernels import (k_addsub, k_cast, k_divide,
                                        k_multiply, tri_and, tri_or)


class ParseTests(unittest.TestCase):
    def test_exact_read(self):
        self.assertEqual(parse_decimal('0.000125'), (125, 6))
        self.assertEqual(parse_decimal('-12.34'), (-1234, 2))
        self.assertEqual(parse_decimal('42'), (42, 0))
        self.assertEqual(parse_decimal(7), (7, 0))
        self.assertEqual(parse_decimal('0.005'), (5, 3))
        self.assertEqual(parse_decimal('99999999999999999999999999999999999999'),
                         (10 ** 38 - 1, 0))

    def test_rejects_non_exact_forms(self):
        for bad in ('1e3', '1.2.3', 'abc', '', '.', '1.', '.5', 0.1, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_decimal(bad)

    def test_no_float_involved(self):
        # 0.1 的 float 是 0.1000000000000000055...,这里必须精确是 (1, 1)
        self.assertEqual(parse_decimal('0.1'), (1, 1))


class HalfEvenTests(unittest.TestCase):
    def test_ties_to_even(self):
        cases = [(5, 2, 2), (7, 2, 4), (-5, 2, -2), (-7, 2, -4),
                 (1, 2, 0), (3, 2, 2), (25, 10, 2), (35, 10, 4),
                 (-25, 10, -2), (-35, 10, -4)]
        for numer, denom, want in cases:
            self.assertEqual(round_quot_half_even(numer, denom), want,
                             f'{numer}/{denom}')

    def test_non_ties_round_nearest(self):
        self.assertEqual(round_quot_half_even(13, 5), 3)   # 2.6
        self.assertEqual(round_quot_half_even(12, 5), 2)   # 2.4
        self.assertEqual(round_quot_half_even(-13, 5), -3)
        self.assertEqual(round_quot_half_even(-12, 5), -2)

    def test_rescale(self):
        self.assertEqual(rescale(125, 6, 6), 125)
        self.assertEqual(rescale(125, 2, 6), 1250000)
        self.assertEqual(rescale(2675000, 6, 2), 268)   # 2.675 -> 2.68
        self.assertEqual(rescale(2685000, 6, 2), 268)   # 2.685 -> 2.68
        self.assertEqual(rescale(2695000, 6, 2), 270)   # 2.695 -> 2.70
        self.assertEqual(rescale(-2675000, 6, 2), -268)


class KernelTests(unittest.TestCase):
    def test_divide_exact_and_rounded(self):
        r = Decimal(38, 19)
        d = Decimal(19, 0)
        self.assertEqual(k_divide(r, d, d, 1, 3), 3333333333333333333)
        self.assertEqual(k_divide(r, d, d, 10, 4), 25000000000000000000)
        self.assertEqual(k_divide(r, d, d, -7, 2), -35000000000000000000)
        # 中点:0.0000005 -> 0.000000(0 为偶),0.0000015 -> 0.000002
        r6 = Decimal(12, 6)
        self.assertEqual(k_divide(r6, d, d, 1, 2000000), 0)
        self.assertEqual(k_divide(r6, d, d, 3, 2000000), 2)

    def test_divide_by_zero(self):
        with self.assertRaises(KernelError) as cm:
            k_divide(Decimal(38, 19), Decimal(19, 0), Decimal(19, 0), 1, 0)
        self.assertEqual(cm.exception.category, DIVISION_BY_ZERO)

    def test_cast_half_even(self):
        d1 = Decimal(3, 1)
        self.assertEqual(k_cast(25, d1, Int64()), 2)    # 2.5 -> 2
        self.assertEqual(k_cast(35, d1, Int64()), 4)    # 3.5 -> 4
        self.assertEqual(k_cast(-25, d1, Int64()), -2)  # -2.5 -> -2
        self.assertEqual(k_cast(5, Int64(), Decimal(10, 2)), 500)

    def test_overflow_is_error_not_saturate_not_null(self):
        with self.assertRaises(KernelError) as cm:
            k_addsub('add', Int64(), 2 ** 63 - 1, 1)
        self.assertEqual(cm.exception.category, OVERFLOW)
        with self.assertRaises(KernelError):
            k_multiply(Int64(), 2 ** 62, 4)
        with self.assertRaises(KernelError):
            k_cast(99999, Decimal(5, 2), Decimal(4, 2))
        with self.assertRaises(KernelError):
            k_cast(2 ** 63, Decimal(19, 0), Int64())

    def test_three_valued_logic(self):
        self.assertIs(tri_and(False, None), False)
        self.assertIs(tri_and(None, True), None)
        self.assertIs(tri_and(True, True), True)
        self.assertIs(tri_or(True, None), True)
        self.assertIs(tri_or(None, False), None)
        self.assertIs(tri_or(False, False), False)

    def test_fits_precision(self):
        self.assertTrue(fits_precision(10 ** 38 - 1, 38))
        self.assertFalse(fits_precision(10 ** 38, 38))
        self.assertTrue(fits_precision(-(10 ** 38 - 1), 38))


if __name__ == '__main__':
    unittest.main()
