"""类型系统与版本化数值规则。

类型只有四种:bool、int64、声明 precision/scale 的 decimal、以及 null
(仅用于无类型 null 字面量,不能作为列类型)。decimal 的值在引擎内部一律
以"尾数 int + 类型自带 scale"表示,从不经过 float。

Rules 是冻结数据类:版本号、最大精度、舍入模式、除法/平均值的最小 scale。
同一份查询在不同版本规则下可能得到不同类型计划,聚合状态合并时按 Rules
整体相等来校验版本一致。
"""
from dataclasses import dataclass


class Type:
    __slots__ = ()


@dataclass(frozen=True)
class Bool(Type):
    def __str__(self):
        return 'bool'


@dataclass(frozen=True)
class Int64(Type):
    def __str__(self):
        return 'int64'


@dataclass(frozen=True)
class Decimal(Type):
    precision: int
    scale: int

    def __post_init__(self):
        if not isinstance(self.precision, int) or not isinstance(self.scale, int):
            raise ValueError('precision/scale 必须为整数')
        if self.precision < 1:
            raise ValueError('precision 必须 >= 1')
        if not 0 <= self.scale <= self.precision:
            raise ValueError('scale 必须满足 0 <= scale <= precision')

    def __str__(self):
        return f'decimal({self.precision},{self.scale})'


@dataclass(frozen=True)
class Null(Type):
    def __str__(self):
        return 'null'


BOOL = Bool()
INT64 = Int64()
NULL = Null()

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
# int64 提升为 decimal 时的登记类型:19 位十进制数字恰好覆盖 int64 全域。
INT64_AS_DECIMAL = Decimal(19, 0)


def parse_type(text):
    """解析 'bool' / 'int64' / 'null' / 'decimal(p,s)'。"""
    if text == 'bool':
        return BOOL
    if text == 'int64':
        return INT64
    if text == 'null':
        return NULL
    if isinstance(text, str) and text.startswith('decimal(') and text.endswith(')'):
        body = text[len('decimal('):-1]
        parts = body.split(',')
        if len(parts) == 2:
            try:
                return Decimal(int(parts[0]), int(parts[1]))
            except ValueError:
                pass
    raise ValueError(f'无法解析类型: {text!r}')


def parse_schema_type(text):
    """列类型不允许是 null。"""
    t = parse_type(text)
    if isinstance(t, Null):
        raise ValueError('列类型不能是 null')
    return t


@dataclass(frozen=True)
class Rules:
    """版本化数值规则:类型推导、舍入、空值与错误语义都绑定到 version。

    rounding 当前唯一支持 'half_even';字段保留以便规则版本演进。
    div_min_scale / avg_min_scale 参与除法与 AVG 的结果类型计算。
    """
    version: str
    maximum_precision: int = 38
    rounding: str = 'half_even'
    div_min_scale: int = 6
    avg_min_scale: int = 6

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError('规则版本不能为空')
        if self.rounding != 'half_even':
            raise ValueError(f'不支持的舍入模式: {self.rounding!r}')
        if not isinstance(self.maximum_precision, int) or self.maximum_precision < 1:
            raise ValueError('maximum_precision 必须 >= 1')
        for name in ('div_min_scale', 'avg_min_scale'):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= self.maximum_precision:
                raise ValueError(f'{name} 必须满足 0 <= v <= maximum_precision')
