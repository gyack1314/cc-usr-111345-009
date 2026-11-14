"""错误类别与携带位置信息的异常。

错误分为三个阶段:
- 编译期:CompileError,位置是源 AST 路径(如 $.branches[0].when)。
- 运行期:EvalError,位置是编译 IR 的节点编号(#000042)或聚合位置(agg:<名>),
  并始终携带原始 row_id;首个错误按 (row_id, position) 排序,与批大小、
  分片与调度顺序无关。
- 资料装载期:同样以 EvalError 报告,position 形如 input:<列名>。
"""

DIVISION_BY_ZERO = 'division_by_zero'
OVERFLOW = 'overflow'
INVALID_INPUT = 'invalid_input'

TYPE_MISMATCH = 'type_mismatch'
UNSUPPORTED_TYPES = 'unsupported_types'
PRECISION_EXCEEDED = 'precision_exceeded'
UNKNOWN_COLUMN = 'unknown_column'
INVALID_LITERAL = 'invalid_literal'
INVALID_AST = 'invalid_ast'
VERSION_MISMATCH = 'version_mismatch'


class CompileError(ValueError):
    """编译期拒绝:类型组合不支持、超过最大精度、未知列、非法字面量等。"""

    def __init__(self, category, path, message):
        super().__init__(f'{category} @ {path}: {message}')
        self.category = category
        self.path = path
        self.message = message


class KernelError(Exception):
    """单行标量内核失败,由执行引擎补上 row_id 与节点位置。"""

    def __init__(self, category, detail):
        super().__init__(detail)
        self.category = category
        self.detail = detail


class RowFailure(Exception):
    """行解释器内已带节点位置的失败。"""

    def __init__(self, category, nid, detail):
        super().__init__(f'{category} @ node#{nid}: {detail}')
        self.category = category
        self.nid = nid
        self.detail = detail


class EvalError:
    """一次已定位的运行失败:原始 row_id + AST 节点位置 + 确定的类别。"""

    __slots__ = ('category', 'row_id', 'position', 'path', 'detail')

    def __init__(self, category, row_id, position, path, detail):
        self.category = category
        self.row_id = row_id
        self.position = position
        self.path = path
        self.detail = detail

    @property
    def sort_key(self):
        return (self.row_id, self.position)

    def __repr__(self):
        return (f'EvalError({self.category} row={self.row_id} '
                f'pos={self.position} path={self.path}: {self.detail})')


class QueryFailure(Exception):
    """一次查询的全部错误;first 与批大小、分片、调度顺序无关。"""

    def __init__(self, errors):
        self.errors = sorted(errors, key=lambda e: e.sort_key)
        super().__init__(f'{len(self.errors)} 个错误,首个: {self.first!r}')

    @property
    def first(self):
        return self.errors[0]
