# WP-01C C3 静态类型检查交接

## 范围与基线

基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。开始时工作树已有 AGENTS、路线图、总计划和授权记录的修改，以及 C1 的计划与证据目录。依据平台持续授权和主控的 C3 派工，实施只涉及三种输入域的静态类型、引用和依赖检查及隔离 CPU 测试。规则运行、平台准入、Git 发布与业务配置均不在 C3 范围内。

## 代码证据

| 文件 | 实现 |
|---|---|
| `src/jimu_runtime/domain/validation/rule_type_inputs.py` | 有限使用上下文、目录、RuleSet/Rule 位置、显式单位声明与已知前序 Producer 声明 |
| `src/jimu_runtime/domain/validation/rule_types.py` | `check_condition_types` 消费 C2 操作符签名和结果类型、A/B ConditionExpr、Fact Schema Registry、B 条件绑定与真实 Producer 目录 |
| `tests/{unit,contract,negative}/test_c_rule_type*.py` 与 `tests/fixtures/wp01c/rule_types/support.py` | 三域、严格 Schema 和负例。测试数据由真实 A/B/C 模型与合成定义构建 |

- Rule condition 接收整个 RuleDefinition，重新核对 required/optional selectors 的精确并集、重复、重叠和 Profile→RuleSet→Rule 成员关系。对象 Fact 只在当前 Profile 内匹配；execution Fact 使用系统 Producer。Fact key、Schema、输出、归属及主声明必须吻合。
- 适用性读取已声明元数据。Tool guard 与 Producer activation 逐使用点绑定。Tool step 的本地引用只允许同一 Plan 的严格祖先及其映射输出。N04 的 Question 产出可作为 N05 的先行来源，N04 自身的 activation 不可读取同批 Question 或后续 Tool 输出。其他前序关系需要有限上下文显式声明。AST 失败返回首个类型或引用诊断，结构错误最多展开 64 条。路径超过既有 ValidationIssue 上限时用省略标记保留首尾。
- `eq/ne` 比较同型标量，排除整 List 与 Record 深比较；数值排序严格区分 integer 和 number，单位只按精确 vocabulary pin 与 member 相等判定。`in` 要求标量和同型非空有界 List；闭合 Record 路径由既有 Registry/描述符解析，允许合法可选字段。逻辑操作符和根要求 Boolean。
- B 的 literal wire 原样保留。枚举常量由另一侧的精确枚举 Schema/字段约束，完整成员集必须匹配；两侧都为未绑定枚举常量时返回 incomplete。单位元数据可由供应方按同一次检查的字段/常量位置明确声明，缺少所需绑定或存在冲突时返回定位诊断。没有持久契约调整或数据迁移。

## 验证与边界

- `TEST_PASSED`。锁定 CPU 环境中执行 323 个 C3、相邻 B/C2 和 Domain 导入边界用例，退出码 0，无跳过。Ruff lint、6 个自有文件格式检查和 2 个 C3 源文件的 strict mypy 均退出码 0。[精确命令与退出码](commands.tsv)、[JUnit](pytest.xml)、[环境](environment.log)、[锁摘要](lock_digest.log)、[源码与 fixture 摘要](INPUT_SHA256SUMS)。
- `RUNTIME_NOT_RUN`。静态类型由规则形状和有限声明推导。记录与 digest pin 的外部真实性、前序声明的来源以及 mandatory 消费者的完整生产可用性由调用方及后续完整 Bundle admission 验证。使用点提供目录中的 Policy 时，检查候选 AST 与已注册的 Policy 内容一致。
- C3 工程门禁 **PASS**。C5 负责整包离线回归、生成 Schema 与独立复审。后续 WP-02 的准入工作依照总路线图安排。

回退范围为表中 C3 独占文件及本目录。WP-01B 的持久 Schema、已合并契约与历史证据均未改动。
