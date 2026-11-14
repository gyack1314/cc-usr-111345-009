"""十进制精确读取与 half-even 舍入,全程整数运算,不经过 float。"""
import re

from .fptypes import INT64_MIN, INT64_MAX

_LITERAL_RE = re.compile(r'^([+-]?)(\d+)(?:\.(\d+))?$')


def parse_decimal(text):
    """把十进制字符串精确读成 (尾数 int, scale)。

    只接受可选符号 + 数字 + 可选小数点的形式;拒绝指数形式、float 与
    其他类型。int 输入按 scale 0 精确接受。读取本身不产生任何舍入。
    """
    if isinstance(text, bool):
        raise ValueError(f'不是十进制原文: {text!r}')
    if isinstance(text, int):
        return text, 0
    if not isinstance(text, str):
        raise ValueError(f'不是十进制原文: {text!r}')
    m = _LITERAL_RE.match(text)
    if m is None:
        raise ValueError(f'不是十进制原文: {text!r}')
    sign, int_part, frac_part = m.groups()
    digits = int_part + (frac_part or '')
    mantissa = int(digits) if digits else 0
    if sign == '-':
        mantissa = -mantissa
    return mantissa, len(frac_part or '')


def round_quot_half_even(numer, denom):
    """返回 round(numer / denom),denom 必须 > 0,平局取偶数。

    对负分子同样正确:divmod 向下取整且余数非负,平局时看商是否为奇。
    """
    if denom <= 0:
        raise ValueError('denom 必须为正')
    q, r = divmod(numer, denom)
    twice = r * 2
    if twice < denom:
        return q
    if twice > denom:
        return q + 1
    return q + (q & 1)


def rescale(mantissa, scale_from, scale_to):
    """把尾数从 scale_from 改到 scale_to;放大精确,缩小按 half-even。"""
    if scale_to >= scale_from:
        return mantissa * 10 ** (scale_to - scale_from)
    return round_quot_half_even(mantissa, 10 ** (scale_from - scale_to))


def fits_precision(mantissa, precision):
    """尾数的十进制位数不超过 precision。"""
    return -10 ** precision < mantissa < 10 ** precision


def fits_int64(value):
    return INT64_MIN <= value <= INT64_MAX
