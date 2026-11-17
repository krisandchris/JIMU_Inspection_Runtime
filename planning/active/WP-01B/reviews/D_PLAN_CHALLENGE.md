# Sol Medium PLAN-1 Challenge D

Reviewed PLAN-1 SHA-256: `fe633537098c658c2fa7701fff045916ec076f928cd9e2886a817fcb6ea62417`.
Reviewed FINDINGS SHA-256: `e4306de5231e15d145d1021b55db071034eaaac161f0c5d2fd9ce3203672ca6b`.
Actual second final response, preserved by Primary; dispositions in REVIEW.md.

结论：**CONDITIONAL，仅规划审查；不能开始实施。** 已披露 B-F01–04 确实阻塞，PLAN 没有把待批选项当成批准。新增两项计划缺陷、两项既有阻塞的补充关闭条件如下。

| Finding | 类型与依据 | 修订建议 |
|---|---|---|
| D-C01：实现顺序与模块依赖冲突 | **新增计划缺陷。** PLAN.md:94 表示 Tool依赖Producer声明；:121安排B.3先交Tool，:123才由B.5b创建Producer；文件owner :244/:246也如此。B.3可能无法导入或独立验收。 | 将`ToolFactOutputDeclaration/Mapping`等底层声明提前到B.5a，或拆独立低层模块并指定owner；更新DAG、文件表及targeted测试顺序，禁止临时重复定义。 |
| D-C02：`describe_type`返回值丢失路径可选性 | **新增接口缺陷。** PLAN.md:211返回`TypeDefinition`，:215又要求保留optional信息。A `RecordFieldDefinition.required`在values.py:212之外的字段定义中，取叶子TypeDefinition后无法表达祖先任一字段可缺失。 | 冻结只读`ResolvedFieldType(type_definition, field_path, presence/optional_ancestors)`等返回形状；添加“required叶子位于optional父Record内”的正负例。不得误报该路径必定有值。 |
| D-C03：Authority支持输入还需可信批次资格与Attempt归属 | **既有B-F03关闭条件补充，当前不是已解决缺陷。** FINDINGS.md:68列result/Attempt/Question集合，但尚未明确trustworthy envelope/association、Attempt所属NodeRun及其状态/迟到资格。PLAN.md:225要求完整选定组，仅靠成员集合会允许伪造的完整组。Domain:1082要求“entire trustworthy batch”；Node spec:544–546要求终态与迟到结果不覆写。 | 冻结输入分支至少能表达批次关联可信状态、Attempt→NodeRun→Execution关系及结果是否可选权；明确每种reason允许的选权资格。负例覆盖完整但不可信批次、foreign NodeRun、超时后迟到结果被标validated_success；不实现CAS或真实lease。 |
| D-C04：Fact不可变ID冲突负例缺失 | **新增测试遗漏。** PLAN.md:224、:272只约束candidate复合键同键异内容；Domain:1064同时规定`fact_id`是不可变候选身份。相同fact_id换Execution/Attempt/key会改变复合键，可能绕过冲突检查。 | 同时检查`fact_id→immutable candidate`唯一性；增加“同fact_id、不同candidate key/bytes拒绝”和“不同fact_id、同candidate key不同bytes拒绝”。同内容判重规则仍按B-F03冻结，不拿stable_json冒充原始bytes身份。 |

F04边界与S/D/A划分目前表达正确：FINDINGS.md:42–49明确JCS来源、数值无损规则、UTF-8领域排序与JCS排序分开，以及A默认上界的输入存在性问题；这些是**已披露待冻结项**，不是实现证据。PLAN.md:257明确保留S接受/D拒绝，未弱化A的F01。关闭B-F02时应把这些要求逐项映射到golden/负向用例，不能只做“两次输出相同”。

文件写入控制清楚，当前只有Primary写规划目录；未发现把Sol审查当批准、把synthetic关联校验当真实调用，或把历史324 passed当本轮测试的表述。

`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。本轮只读，无修改、测试、安装或Git变更；停止于Challenge。
