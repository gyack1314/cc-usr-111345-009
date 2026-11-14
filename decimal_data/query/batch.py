"""列式输入批次:显式 null 与缺列严格区分,decimal 只从字符串精确读取。"""
from .errors import INVALID_INPUT, EvalError, QueryFailure
from .fpdecimal import fits_int64, fits_precision, parse_decimal, rescale
from .fptypes import Bool, Decimal, Int64


class Batch:
    """row_ids + 按列存放的已校验值(decimal 为尾数 int,null 为 None)。"""

    __slots__ = ('row_ids', 'columns')

    def __init__(self, row_ids, columns):
        self.row_ids = tuple(row_ids)
        self.columns = columns

    def __len__(self):
        return len(self.row_ids)


def _validate_value(t, value):
    """把 JSON 值校验并转换为内部表示;失败抛 ValueError。"""
    if value is None:
        return None
    if isinstance(t, Bool):
        if isinstance(value, bool):
            return value
        raise ValueError(f'bool 列只接受 true/false/null: {value!r}')
    if isinstance(t, Int64):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'int64 列只接受整数: {value!r}')
        if not fits_int64(value):
            raise ValueError(f'int64 列越界: {value!r}')
        return value
    # decimal:只从 str/int 精确读取,拒绝 float
    m, scale = parse_decimal(value)
    m = rescale(m, scale, t.scale)
    if not fits_precision(m, t.precision):
        raise ValueError(f'值 {value!r} 超出 {t}')
    return m


def build_batch(schema, rules, rows, id_field='id'):
    """把行字典列表构建成 Batch。

    缺列(键不存在)是资料错误;显式 None 是 SQL null,二者严格区分。
    decimal 列的值按列类型做 half-even 缩放(与 cast 同一套规则),
    越界即拒绝。所有装载错误汇总后按 (row_id, 列) 排序抛出。
    """
    del rules  # 装载规则当前不依赖版本字段,保留参数以稳定接口
    errors = []
    row_ids = []
    columns = {name: [] for name in schema}
    seen = set()
    for row in rows:
        rid = row.get(id_field)
        if not isinstance(rid, str) or not rid or rid in seen:
            errors.append(EvalError(INVALID_INPUT, str(rid), 'input:id',
                                    id_field, '行身份为空或重复'))
            continue
        seen.add(rid)
        row_ids.append(rid)
        for name, t in schema.items():
            if name not in row:
                errors.append(EvalError(INVALID_INPUT, rid, f'input:{name}',
                                        name, '缺列(显式 null 不是缺列)'))
                columns[name].append(None)
                continue
            try:
                columns[name].append(_validate_value(t, row[name]))
            except ValueError as exc:
                errors.append(EvalError(INVALID_INPUT, rid, f'input:{name}',
                                        name, str(exc)))
                columns[name].append(None)
    if errors:
        raise QueryFailure(errors)
    return Batch(row_ids, {k: tuple(v) for k, v in columns.items()})


def take_rows(batch, indexes):
    idx = list(indexes)
    return Batch([batch.row_ids[i] for i in idx],
                 {name: tuple(col[i] for i in idx)
                  for name, col in batch.columns.items()})


def split_batch(batch, sizes):
    """按给定行数切分成连续分片。"""
    shards = []
    start = 0
    for size in sizes:
        shards.append(take_rows(batch, range(start, start + size)))
        start += size
    if start != len(batch):
        raise ValueError('分片行数之和必须等于总行数')
    return shards


def shard_batch(batch, n_shards):
    """切成 n_shards 个尽量均匀的连续分片。"""
    n = len(batch)
    if n_shards < 1:
        raise ValueError('分片数必须 >= 1')
    base, extra = divmod(n, n_shards)
    sizes = [base + (1 if i < extra else 0) for i in range(n_shards)]
    sizes = [s for s in sizes if s > 0]
    return split_batch(batch, sizes) if sizes else [batch]


def permute_batch(batch, order):
    """按给定下标顺序重排;order 必须是 0..n-1 的排列。"""
    if sorted(order) != list(range(len(batch))):
        raise ValueError('order 不是合法排列')
    return take_rows(batch, order)
