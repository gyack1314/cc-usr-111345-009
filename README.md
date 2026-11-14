# 成本仓定点表达式的类型推导与惰性向量执行

本项目保存仓库与湖表开发所需的本地资料,提供内容摘要、记录身份与登记关系检查;并在 `decimal_data.query` 查询层中提供定点表达式执行能力:受限 AST 的类型化编译、按掩码的批执行、独立的逐行解释器对照,以及分组 SUM/COUNT/AVG 的定点状态。

资料表包含schema、rows、expressions、numeric_policy。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料,data/boundary.json登记特殊业务情形,data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。

运行环境为 Python 3.11及以上,当前仅使用标准库,无需额外服务。包内io.py负责资料读取,domain.py负责表集合与引用检查,cli.py提供本地检查入口,query/为定点表达式查询层。

## 执行语义

类型(bool、int64、声明 precision/scale 的 decimal、null)、舍入(half-even)、空值与错误位置都由 numeric_policy 登记的版本化规则决定;decimal 从十进制字符串精确读取,不经过 float;溢出报错,不饱和、不变 null。CASE/COALESCE/AND/OR 按惰性边界求值,未选中的错误分支不产生错误;行执行与批执行对同一查询给出同样结果。完整类型计划与数值依据见 docs/execution-semantics.md。

```sh
python -m unittest discover -s tests -v
python -m compileall -q decimal_data tests
python -m decimal_data.cli data/normal.json
python -m decimal_data.cli data/boundary.json
python -m decimal_data.cli data/normal.json --explain e1   # 打印类型计划
python -m decimal_data.cli data/normal.json --run e1       # 批引擎执行
python -m decimal_data.cli data/normal.json --run e1 --engine row
python -m decimal_data.cli data/normal.json --run e1 --chunk-size 1
```

容器内使用同样的检查入口:

```sh
docker build -t decimal-data .
docker run --rm decimal-data
```
