# 业务资料约定

price保留十进制原文，tokens为int64；AST字段只代表资料约定，不执行资料内代码。数值策略版本绑定precision上限与舍入，不代表未提供的正式类型推导表已实现。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，未知版本或内容变化应重新登记。业务身份、金额、版本及测量数值均为本地资料，不代表任何现实机构的经营数据。

- schema：id、type、precision、scale。
- rows：id、tokens、price、enabled。
- expressions：id、ast。
- numeric_policy：id、rounding、maximum_precision。

normal与boundary是相互独立的资料包，不能直接拼接重复id。引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，不替代仓库查询语义、快照可见性或递归完备性判断。
