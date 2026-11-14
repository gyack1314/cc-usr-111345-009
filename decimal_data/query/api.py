"""把登记资料(表集合)接到查询层:schema、规则、表达式、执行入口。"""
from .batch import build_batch
from .compiler import compile_projection
from .engine_batch import execute as batch_execute, execute_chunked
from .engine_row import execute_rows as row_execute
from .errors import INVALID_AST, CompileError
from .expr import parse_ast
from .fptypes import Rules, parse_schema_type


def schema_of(tables):
    """从 schema 表得到 {列名: 类型}。"""
    schema = {}
    for row in tables['schema']:
        schema[row['id']] = parse_schema_type(row['type']) if \
            row['type'] != 'decimal' else _decimal_of(row)
    return schema


def _decimal_of(row):
    from .fptypes import Decimal
    return Decimal(row['precision'], row['scale'])


def rules_of(tables):
    """从 numeric_policy 表得到版本化规则;只能登记一个版本。"""
    rows = tables['numeric_policy']
    if len(rows) != 1:
        raise CompileError(INVALID_AST, 'numeric_policy',
                           f'数值策略必须恰好登记一条,实际 {len(rows)} 条')
    row = rows[0]
    return Rules(version=row['id'], rounding=row['rounding'],
                 maximum_precision=row['maximum_precision'])


def expression_of(tables, expr_id):
    """按 id 取表达式并解析为 AST。"""
    for row in tables['expressions']:
        if row['id'] == expr_id:
            return parse_ast(row['ast'])
    raise CompileError(INVALID_AST, 'expressions', f'未知表达式: {expr_id!r}')


def compile_expression(tables, expr_id, output='result'):
    """schema + 规则 + 表达式 -> 投影计划。"""
    return compile_projection(schema_of(tables), rules_of(tables),
                              {output: expression_of(tables, expr_id)})


def batch_of(tables):
    """rows 表 -> 列式批次。"""
    return build_batch(schema_of(tables), rules_of(tables), tables['rows'])


def run_expression(tables, expr_id, engine='batch', chunk_size=None):
    """对资料包执行一个表达式,返回 Result。"""
    plan = compile_expression(tables, expr_id)
    batch = batch_of(tables)
    if engine == 'row':
        return row_execute(plan, batch)
    if chunk_size is not None:
        return execute_chunked(plan, batch, chunk_size)
    return batch_execute(plan, batch)
