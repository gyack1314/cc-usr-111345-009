# 定点表达式执行语义

本文件是 `decimal_data.query` 的完整类型计划与数值依据。所有规则由
`Rules` 冻结数据类版本化(当前登记版本见各资料包 `numeric_policy` 表,
如 `v1`);同一查询在同一规则版本下,行执行与批执行、任意批大小、任意
合法分片与输入重排,结果必须一致。测试套件(`tests/test_query_*.py`)
逐条核对本文档的不变量。

## 1. 类型系统

| 类型 | 表示 | 说明 |
| --- | --- | --- |
| `bool` | `bool` | 三值逻辑:`True` / `False` / null |
| `int64` | `int` | [-2^63, 2^63-1],越界即错误 |
| `decimal(p,s)` | 尾数 `int` + 类型自带 scale | `p` 位十进制数字,`s` 位小数;`|尾数| < 10^p` |
| `null` | `None` | 仅用于无类型 null 字面量,不能作为列类型 |

- decimal 一律从十进制字符串精确读取(`fpdecimal.parse_decimal`),
  拒绝 float、指数形式;引擎内部只有整数尾数,从不经过 float。
- 输入中显式 `null`(JSON 键存在、值为 null)是 SQL null;键不存在是
  缺列,装载期拒绝(`invalid_input`)。二者严格区分。
- decimal 列装载时按列类型做 half-even 缩放(与 cast 同一规则),越界拒绝。

## 2. 版本化规则 Rules

```python
Rules(version='v1', maximum_precision=38, rounding='half_even',
      div_min_scale=6, avg_min_scale=6)
```

- `version` 绑定整套语义;聚合状态序列化携带全部规则字段,合并时按
  `Rules` 整体相等校验,不一致即 `version_mismatch`。
- `rounding` 当前唯一支持 `half_even`;需要舍入的地方只有两类:
  **显式 cast**(含装载缩放、字面量归一化)与**除法**(含 AVG 定稿)。
- 同一表达式在不同规则版本下可以得到不同类型计划
  (见 `test_rules_version_changes_type_plan`)。

## 3. 冻结的结果类型规则(编译期)

记号:`d1=decimal(p1,s1)`、`d2=decimal(p2,s2)`;`mp = maximum_precision`;
int64 与 decimal 混合时先提升为 `decimal(19,0)`(19 位恰好覆盖 int64 全域)。

| 运算 | 结果类型 | 超限处理 |
| --- | --- | --- |
| `d1 + d2`,`d1 - d2` | `decimal(max(p1-s1,p2-s2)+s+1, s)`,`s=max(s1,s2)` | `p > mp` 编译期拒绝 |
| `d1 * d2` | `decimal(p1+p2+1, s1+s2)` | `p > mp` 编译期拒绝 |
| `d1 / d2` | `s=max(div_min_scale, s1+p2+1)`,`p=(p1-s1)+s2+s` | `p > mp` 时压缩 `s` 使 `p=mp`;`s<0` 仍不够(整数部分超 `mp`)编译期拒绝 |
| int64 `+ - *` int64 | `int64` | 运行期溢出检查 |
| int64 `/` int64 | 提升后按除法规则 → `decimal(38,19)`(v1) | 同上 |
| 比较 | `bool`;数值按公共类型提升,bool 仅 `=`/`<>` | 其余组合编译期拒绝 |
| `AND/OR/NOT` | `bool`;操作数须为 bool 或无类型 null | 否则编译期拒绝 |
| `CASE/COALESCE` | 各分支公共类型:`s=max(si)`,`p=max(pi-si)+s` | `p > mp` 编译期拒绝;bool 与数值不可混合 |
| `CAST` | 数值互转允许;bool↔数值拒绝;null 可到任何类型 | 目标 `p > mp` 编译期拒绝 |

实例(v1):`tokens * price` = `decimal(19,0) * decimal(12,6)` →
`decimal(32,6)`;`price / price` → `decimal(31,19)`;
`tokens / tokens` → 压缩后 `decimal(38,19)`。

编译器在 IR 中插入的**隐式 cast 只放大 scale/精度,永远精确**;只有查询里
显式写出的 cast 与除法会舍入。`Plan.explain()` 输出完整类型计划(每个
节点的 op、类型、隐式/显式转换、源路径)。

## 4. 舍入与溢出的数值依据

`round_quot_half_even(n, d)`:对精确有理数 `n/d` 取最近整数,平局取偶。
对负数同样成立(divmod 向下取整、余数非负,平局看商奇偶)。

| 输入 | 目标 | 结果 | 依据 |
| --- | --- | --- | --- |
| 2.5 | int64 | 2 | 平局,2 偶 |
| 3.5 | int64 | 4 | 平局,4 偶 |
| -2.5 | int64 | -2 | 平局,-2 偶 |
| 2.675 | decimal(12,2) | 2.68 | 精确读取为 2675/1000;267.5 平局取偶 268。**float 路径得 2.67**(2.675 的二进制近似为 2.67499…),此处必须 2.68 |
| 2.685 | decimal(12,2) | 2.68 | 268.5 平局取偶 268 |
| 0.005 | decimal(12,2) | 0.00 | 0.5 平局取偶 0 |
| 1/2000000 | decimal(12,6) | 0.000000 | 商 0.0000005 平局取偶 |
| 3/2000000 | decimal(12,6) | 0.000002 | 商 0.0000015 平局取偶 2 |
| 1/3 | decimal(38,19) | 0.3333333333333333333 | 精确商舍入到 scale 19 |

溢出(`int64` 越界、decimal 尾数超 `p`、cast 目标放不下)**是错误,不饱和、
不变成 null**;唯一豁免是聚合累积器(见第 8 节)。运行期溢出来源只有
int64 四则、显式 cast 与聚合定稿:decimal 加/减/乘的结果类型按构造留有
充足空间;除法结果类型的整数部分按 `(p1-s1)+s2` 构造,可证最大商舍入后
仍容纳得下(余量 `k = mp - p1 >= 0`),因此除法只有 `division_by_zero`
一类运行期错误。

成本仓场景:`0.000037 × 123456`,float 得 `4.5678719999999995`,本层精确
得 `4.567872`(尾数 4567872,scale 6)。

## 5. 三值逻辑与项目短路规则

- 结果表:`false AND anything = false`;`true OR anything = true`;
  `null AND false = false`;`null OR true = true`;其余含 null 组合为 null。
- 短路规则(确定、可计数):`AND` 只在左操作数**不为 false** 的行上求值
  右操作数;`OR` 只在左操作数**不为 true** 的行上求值右操作数。
  因此 `false AND (1/0>0)` 为 false 无错误,而 `null AND (1/0>0)` 必须
  求值右侧,错误照常产生。

## 6. 惰性求值边界与公共子表达式

- `CASE` 按序求值条件,只计算被选中分支;条件为 null 视为不选中。
  `COALESCE` 只求到首个非 null。未被求值的表达式(包括其中的除零、
  溢出)**不产生错误**。
- 编译期按结构哈希共享公共子表达式(同一子式只有一个 IR 节点);
  执行期按"需求掩码"记忆化:每个节点每行至多求值一次,且只在被需求的
  行上求值——共享**不越过惰性边界**。同一子式对某些行可执行,不代表
  其它行也会执行它(见 `test_shared_node_computed_once_and_not_beyond_lazy_boundary`:
  `tokens+1` 被两个 CASE 共享,`enabled`/`flag` 均为 null 且
  `tokens=int64 最大值` 的行从不触发求值,整批无错误)。
- `counts[nid]` 记录每个 IR 节点实际执行的行求值次数;批引擎与行解释器
  逐点相等,且与批大小无关。

## 7. 编译 IR 与按掩码求值

- IR 节点:`op`、结果类型、子节点、载荷(常量值/列名/cast 目标/隐式标记);
  `nid` 按输出顺序先序分配,是错误定位与计数的稳定坐标。
- 批执行:行集合用 Python 大整数位掩码表示;`ensure(node, mask)` 只计算
  `mask & ~已算 & ~dead` 的行;严格算子(四则、比较、cast、NOT)对子节点
  全量需求后逐行应用内核;惰性算子(CASE/COALESCE/AND/OR)把掩码划分成
  子集递归需求。出错行进入 `dead` 掩码,后续需求自动跳过;每行最多记录
  一个错误,与行解释器"首个错误即中止该行"一致。
- 行解释器是独立对照实现:逐行递归、左到右、同样的短路规则,共用同一套
  标量内核(`kernels.py`),数值口径因此一致。

## 8. 错误模型

- 运行期类别:`division_by_zero`、`overflow`;装载期:`invalid_input`;
  编译期:`type_mismatch`、`unsupported_types`、`precision_exceeded`、
  `unknown_column`、`invalid_literal`、`invalid_ast`;
  合并期:`version_mismatch`。
- 每个运行期错误携带:原始 `row_id`、IR 节点位置(`#000042`,可回溯到
  源 AST 路径)、类别与详情。
- **首个错误 = 按 `(row_id, position)` 排序的最小者**,与批大小、分片、
  调度顺序无关(`test_first_error_independent_of_order_and_chunking`)。

## 9. 分组 SUM / COUNT / AVG 的定点状态

| 聚合 | 状态 | 登记输出类型(v1) |
| --- | --- | --- |
| `SUM(int64)` | 任意精度整数和 + 非 null 计数 | `int64` |
| `SUM(decimal(p,s))` | 任意精度尾数和(scale s)+ 计数 | `decimal(38, s)` |
| `COUNT(*)/COUNT(x)` | 计数 | `int64` |
| `AVG(int64)` | 整数和 + 计数 | `decimal(38, 6)` |
| `AVG(decimal(p,s))` | 尾数和(scale s)+ 计数 | `decimal(38, max(s,6))` |

- **累积器比输出宽**:和用 Python 任意精度整数累积,定稿(finalize)时才
  按登记输出类型检查。某个前缀超出输出范围、但完整和可表示时不得提前
  报错(`test_prefix_overflow_then_cancel`:10^38-1 与 1 先加出 10^38,
  再被负值抵消为 0,任意分片都不报错)。
- 整数加法满足结合交换律,最终和与分片顺序无关,**最终溢出无法靠改变
  分片顺序绕过**(`test_final_overflow_cannot_be_bypassed_by_sharding`)。
- AVG 定稿:`round_half_even(和尾数 × 10^输出scale / (计数 × 10^参数scale))`,
  全程整数,中点取偶(见 `test_avg_half_even_midpoints`)。
- 全 null 组:`SUM`/`AVG` 为 null,`COUNT(x)` 为 0,`COUNT(*)` 为行数;
  空输入不产生任何分组。
- 状态可序列化(尾数按字符串),携带规则版本与登记类型;跨批合并逐项
  校验,不一致即 `version_mismatch`。分组按组内最小 `row_id` 排序输出,
  定稿错误定位到该 `row_id` 与 `agg:<名>`。

## 10. 确定性不变量(测试保证)

1. 行执行 = 批执行:值、错误集合、每节点执行计数逐点相等。
2. 批大小不变:chunk ∈ {1,2,3,5,1000} 结果与计数不变。
3. 重排不变:输入任意重排后按 `row_id` 核对的值、错误、首个错误不变。
4. 分片不变:任意合法分片的聚合合并结果 = 单批结果;最终溢出判定不变。
5. 惰性边界:未选中分支求值计数为 0,其错误不产生;共享子式不越界。
6. 溢出语义:报错而非饱和/置 null;聚合只在定稿时按登记类型检查。
