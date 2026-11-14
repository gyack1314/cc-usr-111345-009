"""检查资料的完整性并列出业务记录数量;可选执行登记的表达式。"""
import argparse
import json

from .domain import inspect_tables
from .io import DataError, load
from .query import api
from .query.errors import QueryFailure
from .query.render import format_row


def main():
    parser = argparse.ArgumentParser(description='检查业务资料的身份与完整性')
    parser.add_argument('manifest')
    parser.add_argument('--run', metavar='EXPR', help='执行登记的表达式')
    parser.add_argument('--explain', metavar='EXPR', help='打印表达式的类型计划')
    parser.add_argument('--engine', choices=['batch', 'row'], default='batch')
    parser.add_argument('--chunk-size', type=int, default=None,
                        help='批引擎按该大小分批执行')
    args = parser.parse_args()
    try:
        tables = load(args.manifest)
        if args.explain:
            print(api.compile_expression(tables, args.explain).explain())
            return
        if args.run:
            result = api.run_expression(tables, args.run, engine=args.engine,
                                        chunk_size=args.chunk_size)
            values = result.unwrap()
            out = {rid: format_row(result.plan, values[rid])
                   for rid in sorted(values)}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        print(json.dumps(inspect_tables(tables), ensure_ascii=False, indent=2))
    except (DataError, ValueError, QueryFailure) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
