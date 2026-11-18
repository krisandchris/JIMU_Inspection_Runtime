两项裁决均可作为既有语义内的技术收口，无需因此新增业务审批，但须满足以下条件。

**D03：可以解决原第3项。** 闭合 `SystemOutputResultDescriptor` 已具体定义空输出结果，不再仅凭通用事件存在认定成功。必要完整性条件：

- event ref 必须是既有 `EXECUTION_EVENT`，在可信有限目录中唯一解析到该 descriptor；同 ref 异内容拒绝。
- descriptor、Producer declaration、Attempt→NodeRun→Execution、selection 的 execution/scope/requirement identity/version/output_name 必须一致。System Producer 仅 execution scope，不允许混入 object/profile 归属。
- `emitted` 必须恰好引用一个候选 Fact；该 Fact 的 producer/schema/key/scope/Attempt 与声明一致，并通过五态值约束。不得将 `emitted` 等同 `known`。
- `optional_absence` 只允许 declaration 的 `zero_or_one`；要求结构化原因，禁止 Fact ref/value；`selected_fact_refs` 必须为空。`emitted` 则必须恰含其同一 Fact ref。
- 同一个声明输出及 Attempt，不允许同时 emitted 与 absence、多份结果或未声明输出。完整性检查须依据声明输出，而非请求方自行提交的子集。
- absence 只说明这个 optional 输出没有 Fact，不产生负证据，也不解除下游 required consumer 的缺失阻塞。
- selection reason、Attempt 资格、可信状态、revision/previous 链仍须完整验证。通用 event、失败 Attempt 或迟到事件不能自动获得 `validated_success`。
- 明确这只是 synthetic/可信输入关联校验；不证明实际事件产生、可信审批、持久历史或 CAS。

**Mapping：允许 required target 的 source 路径经过 optional 祖先，只要显式 `on_missing=block`。** 我此前表中的“optional 祖先不安全”应收紧为“optional 祖先且缺少合法阻塞策略”，不能一概拒绝。

这与 `specs/01_Runtime_Domain_Model_V1.0.md:1174` 的 ToolSkip `on_missing_input=block`、`:759–770` 的 Prompt required→block / optional→omit 一致。必要条件：

- 静态路径必须存在于 source schema；“可选路径”不能放宽为“未声明路径”。
- 保留所有 optional 祖先信息；有值时仍验证 source→target 类型、范围、枚举及语义兼容。
- `block` 禁止补默认值、转 null/false、跳过 required target 或带缺失输入继续执行；B 只验证策略合同，不执行阻塞流程。
- `omit` 只允许目标按其完整祖先路径可合法缺失；目标实际 required 时拒绝。
- 缺失、非known Fact、无权威 Fact、类型无效应保留不同诊断，不能都当 optional omission。
- mapping 的 missing 策略与所引用 ToolSkip/Prompt 策略必须一致；不得从 required source 显式策略推导运行时必有值。

`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`；未写文件、未运行测试。
