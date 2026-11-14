"""执行结果:值、每节点执行计数、错误列表。"""
from .errors import QueryFailure


class Result:
    """values: row_id -> {输出名: 值};counts: 每个 IR 节点的行求值次数。"""

    __slots__ = ('plan', 'values', 'counts', 'errors')

    def __init__(self, plan, values, counts, errors):
        self.plan = plan
        self.values = values
        self.counts = tuple(counts)
        self.errors = list(errors)

    def unwrap(self):
        """有错误则抛 QueryFailure,否则返回值表。"""
        if self.errors:
            raise QueryFailure(self.errors)
        return self.values
