"""逐行精确标量内核:批引擎与行解释器共用,保证数值口径一致。

值表示:decimal 是尾数 int(scale 由节点类型给出),int64 是 int,
bool 是 bool,null 是 None。内核只处理非 null 输入;null 传播由引擎负责。
溢出抛 KernelError(OVERFLOW),绝不饱和、绝不变成 null。
"""
from .errors import DIVISION_BY_ZERO, OVERFLOW, KernelError
from .fpdecimal import (fits_int64, fits_precision, rescale,
                        round_quot_half_even)
from .fptypes import Decimal, Int64


def _check_result(value, t, what):
    if isinstance(t, Int64):
        if not fits_int64(value):
            raise KernelError(OVERFLOW, f'{what}结果超出 int64: {value}')
    elif isinstance(t, Decimal):
        if not fits_precision(value, t.precision):
            raise KernelError(OVERFLOW, f'{what}结果超出 {t}: 尾数 {value}')
    return value


def k_addsub(op, t, left, right):
    """同类型操作数的加减(IR 已对齐 scale)。"""
    value = left + right if op == 'add' else left - right
    return _check_result(value, t, '加法' if op == 'add' else '减法')


def k_multiply(t, left, right):
    return _check_result(left * right, t, '乘法')


def k_divide(result_t, left_t, right_t, left, right):
    """decimal 除法:精确商按 half-even 舍入到结果 scale。"""
    if right == 0:
        raise KernelError(DIVISION_BY_ZERO, '除数为零')
    numer = left * 10 ** (right_t.scale + result_t.scale)
    denom = right * 10 ** left_t.scale
    if denom < 0:
        numer, denom = -numer, -denom
    m = round_quot_half_even(numer, denom)
    return _check_result(m, result_t, '除法')


def k_cast(value, from_t, to_t):
    """显式 cast:缩小 scale 按 half-even 舍入;溢出报错,不饱和、不变 null。"""
    if from_t == to_t:
        return value
    if isinstance(to_t, Decimal):
        if isinstance(from_t, Int64):
            m = value * 10 ** to_t.scale
        else:
            m = rescale(value, from_t.scale, to_t.scale)
        return _check_result(m, to_t, 'cast ')
    if isinstance(to_t, Int64):
        m = rescale(value, from_t.scale, 0)
        return _check_result(m, to_t, 'cast ')
    return value  # bool -> bool


def k_compare(op, left, right):
    """同类型操作数的比较(数值已对齐 scale;bool 只有 eq/ne)。"""
    if op == 'eq':
        return left == right
    if op == 'ne':
        return left != right
    if op == 'lt':
        return left < right
    if op == 'le':
        return left <= right
    if op == 'gt':
        return left > right
    return left >= right


def tri_and(left, right):
    """SQL 三值 AND:false 吸收,null 次之。"""
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def tri_or(left, right):
    """SQL 三值 OR:true 吸收,null 次之。"""
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False
