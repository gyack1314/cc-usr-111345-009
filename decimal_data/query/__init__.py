"""定点表达式查询层:类型化编译 IR、按掩码批执行、行解释器对照、分组聚合。"""
from . import agg, api
from .batch import Batch, build_batch, permute_batch, shard_batch, split_batch
from .compiler import Plan, compile_projection
from .engine_batch import execute as execute_batch, execute_chunked
from .engine_row import execute_rows
from .errors import (DIVISION_BY_ZERO, INVALID_INPUT, OVERFLOW,
                     PRECISION_EXCEEDED, TYPE_MISMATCH, UNKNOWN_COLUMN,
                     UNSUPPORTED_TYPES, VERSION_MISMATCH, CompileError,
                     EvalError, QueryFailure)
from .expr import parse_ast
from .fptypes import (BOOL, INT64, NULL, Bool, Decimal, Int64, Null, Rules,
                      parse_type)
from .render import format_value

__all__ = [
    'agg', 'api', 'Batch', 'build_batch', 'permute_batch', 'shard_batch',
    'split_batch', 'Plan', 'compile_projection', 'execute_batch',
    'execute_chunked', 'execute_rows', 'parse_ast', 'format_value',
    'BOOL', 'INT64', 'NULL', 'Bool', 'Decimal', 'Int64', 'Null', 'Rules',
    'parse_type', 'CompileError', 'EvalError', 'QueryFailure',
    'DIVISION_BY_ZERO', 'OVERFLOW', 'INVALID_INPUT', 'TYPE_MISMATCH',
    'UNSUPPORTED_TYPES', 'PRECISION_EXCEEDED', 'UNKNOWN_COLUMN',
    'VERSION_MISMATCH',
]
