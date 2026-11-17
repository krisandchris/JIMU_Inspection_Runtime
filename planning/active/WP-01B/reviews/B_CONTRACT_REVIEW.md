# Sol Medium read-only review B

Source: native task `/root/wp01b_b_facts`.
Captured by Primary from the actual first final response; no claim of code acceptance.

`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`

已只读检查 main，HEAD=`51a10416d882fe85e5a1cfc5440eac6498fc48f4`。tracked 工作树无改动；11 个 acceptance/transition untracked 文件与任务说明一致。未写文件、安装、测试或修改 Git。

**现状与可复用基础**

- 当前实现仅 A 的基础类型；尚无 FactSchemaDefinition、Registry、Producer 或 Authority 业务模型。
- 可复用严格不可变基类：[base.py:15](src/jimu_runtime/domain/base.py:15)；类型与值约束：[values.py:140](src/jimu_runtime/domain/values.py:140)、[values.py:257](src/jimu_runtime/domain/values.py:257)。
- A 的 `stable_json()` 明确不是 JCS：[base.py:24](src/jimu_runtime/domain/base.py:24)。
- F01 双阶段边界必须保留：JSON Schema 数学 integer/number 与严格 Pydantic 的预期差异已有回归：[test_json_schema.py:185](tests/contract/test_json_schema.py:185)。B 不应放宽 A 或宣称两阶段全等价。
- A 历史证据记载两次最终 324 passed、无 failed/error/skipped：[verification_summary.json:6](planning/implementation_evidence/WP-01A/verification_summary.json:6)。本分析未重跑。

**必须在 B 计划冻结的阻塞**

1. **F04 的 JCS/摘要实施边界。**  
   [Domain:940](specs/01_Runtime_Domain_Model_V1.0.md:940)要求显式 JCS；[Domain:988](specs/01_Runtime_Domain_Model_V1.0.md:988)要求 `$id` 中的 canonical input SHA-256；[Domain:994](specs/01_Runtime_Domain_Model_V1.0.md:994)要求 child-before-parent 摘要顺序。但 [Domain:1007](specs/01_Runtime_Domain_Model_V1.0.md:1007)、[Bundle:7](specs/02_Runtime_Bundle_Specification_V1.0.md:7)将完整 artifact/Bundle 摘要留 WP-02。  
   建议明确授权范围：B 是否包含**仅 generation-input/value_schema 的受限 canonicalizer 与 input hash**。若不包含，B 只能交投影模型/生成结构，不得宣称完成 F04 canonical bytes、`$id` 或独立 digest 验收。不能以 `stable_json()` 替代 JCS。还需冻结数值不可无损 canonicalize 时拒绝的规则与测试向量，不能缩窄 A 的类型代数来绕过问题。

2. **`AuthoritativeResultSelection.selected_result_ref` 的实际 kind 尚未规定。**  
   [Domain:1076](specs/01_Runtime_Domain_Model_V1.0.md:1076)列出 observation_batch/tool_step/system_output 三组，但未明确 result target 映射。[enums.py:60](src/jimu_runtime/domain/enums.py:60)实际已有 `OBSERVATION_BATCH`、`TOOL_EXECUTION`；没有 system-output result kind。  
   建议 observation→ObservationBatch、tool_step→ToolExecution 作为待批映射；system_output 的 result kind/group identity 须另冻结。不能自行新增 RuntimeRecordKind、把单条 Fact 当整组结果，或用泛化 ref 掩盖。若 B 明确不包含完整 Runtime records，应把 Authority 具体模型及运行记录验证留后阶段，仅列接口与依赖。

3. **Producer 的条件/Tool 路径依赖不能偷渡 AST。**  
   [Domain:1021](specs/01_Runtime_Domain_Model_V1.0.md:1021)要求 `activation_condition` 是 always 或 typed Tool condition；[Domain:1049](specs/01_Runtime_Domain_Model_V1.0.md:1049)要求 value_path 从规范 ToolResult.payload 静态解包。B 不含 RuleAST/TypeChecker 时，不能用 Any、任意 JSON 或另造简化 AST 完成表面闭合；需明确这些模型/检查留后阶段或按获批依赖交付。

**建议 Schema / 接口清单**

- B 的核心：FactSchemaDefinition、GlobalTypeLimits、ValueSchemaGenerationSpec、TypeDefinitionProjection、ValueSchemaGenerationInput、FactSchemaCatalogue/Registration、结构化 ValidationIssue、不可变 Registry。
- Definition-side 可纳入候选：FactSelector、三个 ProducerSelector、ToolFactOutputDeclaration/Mapping、Producer 分支；但若涉及完整 ToolDefinition/AST/Runtime source，必须在清单注明依赖与阶段，不以占位模型覆盖。
- 完整 FactRecord、ObservationBatch、ToolExecution、Authority revision persistence、RuleAST/TypeChecker、Bundle hasher 不应因关系审查而进入 B。
- Registry 建议采用构建期 `register(exact_ref, definition)`、`freeze()`、`resolve(exact_ref)`、`validate_payload(ref, raw_value)`、`describe_type(ref, field_path)`。原建议接口在 [WP-01 plan:272](planning/WP-01_IMPLEMENTATION_PLAN.md:272)。
- definition 不包含自身 digest，因此单独 `register(definition)` 不足以接收 exact ref。采用有限 injected registration，明确只检查引用身份与冲突，**不宣称已验证 digest 对应真实内容**。完整内容验证属于后续 compiler/admission。
- `validate_fact` 依赖完整 FactRecord，应后移；`compare_compatibility` 须定义保守结果与规则，不能把“同 shape”当可替换语义。

**关系不变量**

- Question：每 eligible Object/accepted N04 Attempt 恰一个 primary；Tool：每 step Attempt 可多个有名 outputs：[Domain:1027](specs/01_Runtime_Domain_Model_V1.0.md:1027)。
- V1 N05 Fact outputs 仅 object scope；execution Facts 使用 system producer：[Domain:1033](specs/01_Runtime_Domain_Model_V1.0.md:1033)。
- 编译碰撞域是 `(profile_ref, object, fact_key)` 或全 Profiles 的 `(execution, fact_key)`；同 Schema 仍不得多 producer：[Domain:1064](specs/01_Runtime_Domain_Model_V1.0.md:1064)。
- 候选 Fact 保留 Attempt；整批/整 step authority，不跨 Attempt 挑最好值，不按最后写入、时间或置信度选权威：[Domain:1069](specs/01_Runtime_Domain_Model_V1.0.md:1069)。
- FactSelector 固定 schema/key/scope/producer/field_path，不包含 runtime attempt/fact ID，也不回指 derived catalogue：[Domain:1094](specs/01_Runtime_Domain_Model_V1.0.md:1094)。

**建议验收测试（均未运行）**

- Registry 精确解析、旧版本并存、同 ID/version 不同 digest 拒绝、freeze 后注册拒绝、外部输入变更不影响快照。
- 投影独立性：改 parent ID/version/semantic metadata 不改 child；Record/enum 来源顺序变化不改规范输出；generation spec/limits 变化明确处理。
- 不可无损数值、非有限值、重复字段/成员、额外字段、递归、越限、ambient limits 拒绝。
- payload Schema 检验结构；严格 Domain 阶段检验 numeric representation、enum/Record/type conformance；分别记录两阶段结果。
- Producer/selector 有限目录：Question primary 1:1、Tool output 1:N、scope/key/Schema/field_path 精确匹配、冲突拒绝；不冒充完整 Bundle producer closure。
- 后阶段 Authority 回归：整批/step 选择、跨 Attempt cherry-pick 拒绝、错 execution/Profile/Object/Attempt/result kind 拒绝、旧 revision 不覆盖、迟到结果不改历史。

自身 model/effort 安全核对只读取五个白名单环境字段，均 `UNAVAILABLE`；可用工具没有实际当前 model/effort 查询接口，不能据继承声明伪造已验证身份。未再委派。分析结束，停止。
