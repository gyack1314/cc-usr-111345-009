"""把内部值渲染成可打印形式;decimal 按 scale 还原成十进制字符串。"""
from .fptypes import Decimal


def format_value(t, v):
    if v is None:
        return None
    if isinstance(t, Decimal):
        sign = '-' if v < 0 else ''
        digits = str(abs(v)).rjust(t.scale + 1, '0')
        if t.scale:
            return f'{sign}{digits[:-t.scale]}.{digits[-t.scale:]}'
        return f'{sign}{digits}'
    return v


def format_row(plan, values):
    """{输出名: {'type': ..., 'value': ...}}"""
    return {name: {'type': str(plan.output_type(name)),
                   'value': format_value(plan.output_type(name), values[name])}
            for name, _ in plan.outputs}
