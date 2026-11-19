# WP-01C C1 契约盘点

基线为 PR #2 合并提交 `2552affeb69fbc2594fe70724f895b0db0967d47`，所在工作树为 detached HEAD。盘点前 `AGENTS.md`、`planning/DEVELOPMENT_ROADMAP.md`、`planning/WP-01_IMPLEMENTATION_PLAN.md`、`planning/active/PLATFORM_GOAL.md` 已有变更，WP-01C 的计划目录尚未跟踪。实际产品基线有 `src/jimu_runtime/domain/`、`tests/{unit,contract,negative,fixtures}/` 和 `schemas/generated/`。三个场景示例只作为规格说明。

盘点覆盖[总计划 §5 全部三张清单和后续字段约束](../../WP-01_IMPLEMENTATION_PLAN.md#5-待冻结-pydantic-schema-清单)、§7 AST 和 §15 适用性。源码路径相对 `src/jimu_runtime/domain/`。表中 `S(模块.符号)` 是 [生成索引](../../../schemas/generated/index.json) 中的 `模块.符号.schema.json`，`S(模块.符号) → 分支` 是该 Schema 下的嵌套定义或字段。索引有 162 项，区分定义根、运行根、命名联合、基础类型、定义组件及有限输入/输出。测试路径相对 `tests/`，fixture 目录相对 `tests/fixtures/`。本次只读测试源码和 fixture。

状态含义：**已有**表示存在对应契约；**映射**表示用既有名称、字段或分支表达；**有限**表示已有可复用的局部数据，完整契约仍待交付；**缺口**表示缺所列契约。标注 C2/C3/C4 的条目由该批补齐，C5 统一更新生成器/索引并复核有效及无效 fixture；C2–C4 各自交付定向测试。涉及独立 wire 或规范冲突的条目见末尾 Finding。

## 基础、定义、枚举和值

| 计划符号 | C1 结论、源码和 Schema | 已有静态证据 / 归属 |
|---|---|---|
| ContractModel | 已有 `base.py:ContractModel`；公共严格/冻结配置，出现在所生成模型上。 | `unit/test_foundation.py`、`contract/test_json_schema.py`；C5 回归 |
| PersistedContract | 映射到 `definition_support.py:DefinitionContract` 的定义根版本以及 `runtime/facts.py`、`runtime/authority.py` 中明确的运行根版本；独立同名基类缺失。 | `contract/test_b_schema_inventory.py` 的缺版本拒绝；C2/C4 在各自新根显式写版本，C5 复核 |
| ArtifactIdentity | `references.py:_ArtifactReference` 与 `ContentRef` 已表达 kind/id/version/digest；无独立同名 Schema。IdentitySelector 的仅身份选择语义单列。 | `negative/test_references.py`、`foundation_contracts.json`；C4 核对 inventory 项的身份复用，见 Finding C1-03 |
| ContentRef、IdentitySelector、SchemaRef、RuntimeRecordRef | 已有 `references.py` 四个同名类型及各自 S(references.同名符号)。RuntimeRecordRef 是在拥有者范围解释的 ID；Object ID 按 Execution 解析。 | `contract/test_json_schema.py`、`negative/test_references.py`、`foundation_contracts.json`；C5 回归 |
| MediaRef | 缺口；静态制品描述符和 Tool mapping 的 ImageFieldSelector 提供局部字段。 | `contract/test_b_ontology.py`、`contract/test_b_mapping_contracts.py` 仅邻接边界；C4 |
| EvidenceRef | 已有 `runtime/facts.py:EvidenceRef`，S(runtime.facts.FactRecord) → EvidenceRef（限定记录种类）。 | `contract/test_b_fact_records.py`、`wp01b/a1/contracts.json`；C5 回归 |
| ClockProvenance | 缺口；`definition_support.py:UTCDateTime` 有索引，表达规范 UTC 时间，采集原时区/偏移/时钟来源须补。 | `unit/test_b_utc_nested.py`；C4 |
| TraceContext | 缺口；现有 ValidationIssue 表达诊断路径。 | C4，Execution/Node 共用 |
| ValidationIssue | 已有 `validation/issues.py:ValidationIssue`，S(validation.issues.ValidationIssue)；有界字段/索引路径和精确引用。 | `unit/test_b_validation_reports.py`、`contract/test_b_producer_authority_contracts.py`；C3/C4 可复用 |
| ArtifactKind | 已有 `enums.py` 同名枚举、S(enums.ArtifactKind)，25 种。 | `unit/test_foundation.py`、`contract/test_json_schema.py`；C5 保持集合 |
| PolicyKind | 已有 `definition_enums.py` 同名枚举，S(policies.PolicyArtifact) 表达 16 个分支的限定值。 | `contract/test_b_complete_policies.py`、`wp01b/x1/contracts.json`；C5 保持集合 |
| TaskStatus、ExecutionStatus、NodeRunStatus、AttemptStatus、FactStatus、DecisionResult | 已有 `enums.py` 六个独立枚举和 S(enums.各同名符号)；持久根及转移检查另盘点。 | `unit/test_foundation.py`、`contract/test_b_fact_records.py`；C4 复用，C5 回归 |
| RuleTruth | 缺口；RulePolicy 配置与 AST 布尔值已有，待补独立 T/F/U 结果域。 | C4 与 C3 对齐谓词类型；拒绝把 not_evaluated 当 truth |
| CoverageStatus、DiscoveryOutcome、GroundingOutcome | 缺口，各自独立于已有任务和节点状态。 | C4 |
| ProducerKind、ScopeKind | ProducerKind 已有；ScopeKind 对应目前 Fact 范围的 `FactScope`。均来自 `definition_enums.py`，S(producers.FactProducerDeclaration) 或 S(runtime.facts.FactRecord) 中嵌套。其它记录族的 scope 由所属规范定义。 | `contract/test_b_producers_contracts.py`、`contract/test_b_fact_records.py`；C4 复用，C5 回归 |
| NodeId | 有限；`validation/authority_inputs.py:NodeRunDescriptor.node_id` 已限定 N01–N06，S(validation.authority_inputs.NodeRunDescriptor) 字段；缺共享集中类型。 | `contract/test_b_fact_records.py`；C4，保持六个 wire 值 |
| ReplayKind | 缺口。 | C4 |
| ApplicabilityLevel、ApplicabilityEvaluationStatus、ApplicabilityDisposition、ApplicabilityEvaluationOwner | 缺口；配置适用性作用域可供复用，运行 trace 的地位、评估状态、处置、owner 仍需各自域；同时落实 §5 文末 reason code 和 N02 skip reason。 | C4 |
| ProviderRoutingMode、RouteKind | ProviderRoutingMode 映射到 `policies.py:ProviderPolicy` 的 routing_mode 判别字段，S(policies.ProviderPolicy)；RouteKind 及共享审计域待补。 | `contract/test_b_policy_contracts.py`、`wp01b/p1/contracts.json`；C4 |
| SelectionInvocationMode、ToolInvocationOriginKind | 有限；`policies.py:SelectionCallPolicy.mode` 明确调用配置，`validation/authority_inputs.py:ToolStepResultDescriptor.invocation_origin` 只接受 tool_plan_step，各有同名 S。运行态选择模式及工具来源封闭联合待补。 | `contract/test_b_complete_policies.py`、`contract/test_b_fact_records.py`；C4 |
| ProviderVersionResolution、ImplementationVersionResolution | 缺口；B 描述符里实际实现版本为必填 Version，仅覆盖能绑定的 Tool step 候选。 | C4；见 F02/F05 |
| 显式 release/tool/validation 状态 | 有限；`validation/issues.py:CheckStatus` 是 S(validation.issues.CheckResult) 的有限校验状态，`validation/authority_inputs.py` 的 arrival/envelope/association qualification 按描述符使用。Release 生命周期、ToolResult/ToolExecution 状态与 N01/N04 校验结果的目标契约待补。 | `unit/test_b_validation_reports.py`、`contract/test_b_fact_records.py`；C4 |
| BoolValue | 映射至 `values.py:BooleanValue`，S(values.BooleanValue)；既有 wire tag 为 boolean。 | `contract/test_json_schema.py`、`negative/test_values.py`、`foundation_contracts.json`；C5 回归 |
| IntegerValue、NumberValue、StringValue、EnumValue、ListValue、RecordValue | 已有 `values.py` 六个同名类、相应 S(values.各同名符号) 及 S(values.Value)。 | 同上及 `unit/test_value_boundaries.py`；C5 回归 |
| 各对应 TypeDefinition | 已有 `values.py:BooleanTypeDefinition, IntegerTypeDefinition, NumberTypeDefinition, StringTypeDefinition, EnumTypeDefinition, ListTypeDefinition, RecordTypeDefinition`，全部有各自 S(values.同名符号)，另有 S(values.TypeDefinition)、S(values.RecordFieldDefinition)。 | 同上；C3 消费类型，C5 回归 |
| EntityDefinition、RegionDefinition | 已有 `definitions/ontology.py`，各有 S(definitions.ontology.同名符号)。 | `contract/test_b_ontology.py`、`negative/test_b_ontology_invalid.py`、`wp01b/s1/contracts.json`；C5 |
| AssetRef | 映射到 `definition_support.py:StaticAssetContentRef`，S(definitions.ontology.RegionDefinition) → StaticAssetContentRef；精确 static_asset 引用。 | `contract/test_b_ontology.py`、`wp01b/s1/contracts.json`；C5 |
| ProfileDefinition、FacetDefinition、QuestionDefinition | 已有 `definitions/capabilities.py`，S(definitions.capabilities.各同名符号)。`validation/definitions.py` 承担有限的交集、Profile/Question 关联检查。 | `contract/test_b_capabilities.py`、`unit/test_b_definition_relations.py`、`negative/test_b_definition_relations_invalid.py`、`wp01b/o1/contracts.json`；C5 |
| PrimaryFactOutput | 映射到 QuestionDefinition 的 `primary_fact_key` 和 `primary_fact_schema_ref` 两字段；保留现行单主输出 wire。 | `contract/test_b_capabilities.py`、`negative/test_b_capabilities_invalid.py`；C5 |
| ModelGroundingBinding、ToolGroundingBinding | 已有 `definitions/capabilities.py` 定义态分支，S(definitions.capabilities.GroundingBinding) → 两分支；N03 实际调用来源另计。 | `contract/test_b_capabilities.py`、`wp01b/o1/contracts.json`；C4 复用 exact binding |
| ContextFieldSelector | 已有 `definition_support.py` 同名，S(definition_support.ContextFieldSelector)。 | `contract/test_b_capabilities.py`、`contract/test_b_mapping_contracts.py`；C3/C4 复用 |
| QuestionSelectionPolicy | 已有 `policies.py` 同名，S(policies.QuestionSelectionPolicyArtifact) → policy；执行记录另计。 | `contract/test_b_complete_policies.py`、`wp01b/x1/contracts.json`；C4 复用 |
| ToolDefinition、ToolPlanDefinition、ToolStep | 已有 `definitions/tools.py`；前两项各有定义根，S(definitions.tools.ToolPlanDefinition) → ToolStep。 | `contract/test_b_models_tools.py`、`unit/test_b_tool_plans.py`、`wp01b/m1/contracts.json`；C4 复用 |
| StepDependency | 映射到 ToolStep.depends_on 有向边；`definitions/tools.py:plan_ancestors` 校验 DAG、引用唯一性和上游约束。 | `unit/test_b_tool_plans.py`、`negative/test_b_models_tools_invalid.py`；C3 复用 |
| InputBinding | 已有 `tool_mappings.py:InputBinding`，S(tool_mappings.InputBinding)，对应 Context/Object/Image/Fact/StepOutput 输入分支。 | `contract/test_b_mapping_contracts.py`、`negative/test_b_mapping_invalid.py`、`wp01b/s2/contracts.json`；C3 复用 |
| OutputBinding | 已确认的 Fact 输出映射由 `tool_outputs.py:ToolFactOutputMapping` 及 ToolStep.fact_output_mappings 表达，S(definitions.tools.ToolPlanDefinition) → ToolFactOutputMapping。通用独立 OutputBinding 需先确认所需额外行为。 | `contract/test_b_mapping_contracts.py`、`unit/test_b_tool_plans.py`；C3/C4，见 C1-03 |
| ConditionalBranch | 映射到 ToolStep 的 skip/failure policy 引用及 `policies.py:ToolSkipPolicy.condition_ast`、`producers.py:ToolConditionActivation` 的各自使用位；S(policies.ToolSkipPolicyArtifact)、S(producers.ActivationCondition)。 | `contract/test_b_complete_policies.py`、`contract/test_b_producers_contracts.py`；C3 验证类型及使用位置 |
| ResourceRequirements、ToolExecutionPolicy | 已有 `policies.py`，S(policies.ResourceRequirements)、S(policies.ResourceRequirementsArtifact)、S(policies.ToolExecutionPolicyArtifact)。 | `contract/test_b_policy_contracts.py`、`contract/test_b_complete_policies.py`；C5 |
| ModelBinding | 已有 `definitions/models.py:ModelBinding`、S(definitions.models.ModelBinding)。 | `contract/test_b_models_tools.py`、`negative/test_b_models_tools_invalid.py`、`wp01b/m1/contracts.json`；C4 复用 |
| ProviderPolicy、DeploymentRequirement、VersionConstraint、FallbackPolicy、LocalPrimaryException | 已有 `policies.py` 同名符号（VersionConstraint 来自 `definition_support.py`）；S(policies.ProviderPolicy)、S(policies.ProviderPolicyArtifact) → DeploymentRequirement/LocalPrimaryException，S(definition_support.VersionConstraint)、S(policies.FallbackPolicy)、S(policies.FallbackPolicyArtifact)。 | `contract/test_b_policy_contracts.py`、`negative/test_b_policy_invalid.py`、`wp01b/p1/contracts.json`；C4 沿用配置，另补实际调用审计 |
| RuleDefinition、RuleSetDefinition | 缺口；现有 `definitions/capabilities.py:RuleSetContentRef` 和 `policies.py` 中 RulePolicy/ConflictPolicy 可复用。 | C2；精确配置引用、依赖声明、collect_all 与 advisory 候选策略 |
| RulePolicy、ConflictPolicy、FactSelector | 已有 `policies.py` 前两项及各自 policy artifact，`fact_selectors.py:FactSelector` 有 S(fact_selectors.FactSelector)。 | `contract/test_b_complete_policies.py`、`contract/test_b_fact_selectors.py`、`negative/test_b_fact_selectors_invalid.py`；C2/C3 复用 |

## §5 运行态与节点契约

对缺口所列邻接证据只检验既有模型；C4 为目标契约新增正负 fixture，C5 注册和审查生成结构。

| 计划符号 | C1 结论、已有边界及后续责任 |
|---|---|
| BundleManifest、ArtifactInventoryEntry、SourceProvenance、CompatibilityRequirement、IntegrityEnvelope、ChecksumEntry | 缺口；`references.py:ContentRef`、`primitives.py:Digest`、`definition_support.py:VersionConstraint, ExternalCapabilityRequirement` 可复用。B 的 child schema digest 和索引文件 SHA-256 提供不同的摘要语义。C4 依据规格 02 §3–5 补纯数据，C5 注册。 |
| AdmissionReceipt、CapabilityReport、ReleaseRecord、ApprovalReceipt、ActivationRecord、ActivationScope | 缺口；`definition_support.py:ReviewReportRef` 及 S(validation.review_inputs.ReviewMetadata) 是审查引用/评估输入；S(validation.producer_authority_inputs.ExecutionDefinitionPin) 只有调用方提供的 ID、摘要和目录 pins。工程测试 `contract/test_b_mapping_contracts.py`、`contract/test_b_producer_authority_inputs.py` 覆盖这些局部边界。C4 分开技术准入、业务批准与激活，C5 注册。 |
| TaskSubmission、AcceptedTask、MediaAsset、TaskContext、ContextSnapshot、ContextValueProvenance | 缺口；TaskStatus、UTCDateTime、静态资产尺寸及 S(tool_mappings.ContextTypeRegistration) 属基础或已声明类型视图。ContextProvenancePolicy 描述配置需求。任务示例为说明性输入。C4 补一个 RGB 图、采集 provenance、request/accepted/Release pin、ContextSnapshot；C5 注册。 |
| ExecutionCoverage、ProfileCoverage、MandatoryRuleCoverage、MandatoryRuleObligation | 缺口；S(policies.CoveragePolicyArtifact) 是配置，RuntimeRecordKind 有 execution_coverage。C4 补范围、计数、已知排除和未决义务/选定修订，处理空集合与部分有效结果；C5 注册。 |
| ExecutionRecord、ExecutionPins | 有限；`validation/producer_authority_inputs.py:ExecutionDefinitionPin` 有 S，只有 execution_id、release_id、bundle_digest、producer_catalogue_ref、configured_profile_refs。`contract/test_b_producer_authority_inputs.py` 与 `wp01b/x3/inputs.json` 覆盖有限视图。C4 补生命周期、完整 pins 和 scope。 |
| NodeRun、Attempt | 有限；`validation/authority_inputs.py:NodeRunDescriptor, AttemptDescriptor, AttemptOwner` 各有 S；稳定引用、枚举和有限 owner/编号已有。生命周期事件、转换守卫、不可逆终态和完整持久记录待补。`contract/test_b_fact_records.py`、`contract/test_b_authority_history.py`、`wp01b/a1/contracts.json`、`wp01b/a2/contracts.json` 覆盖当前描述符/历史。C4。 |
| ExecutionEvent | 缺口；`runtime/facts.py:ExecutionEventRef` 嵌套在 FactSource、Authority 的导出中；`SystemOutputResultDescriptor` 已消费该引用。C4 补记录。 |
| ConfiguredModelPin、ActualDeploymentIdentity | 缺口；`ModelBinding` 和 `DeploymentRequirement` 属定义，有限 ReviewMetadata 提供审查输入。C4 补配置 pin 与分开记录的实际观测身份，保留不可核实时的明确分支。 |
| ObjectRecord | 有限；`validation/authority_inputs.py:ObjectDescriptor` 的 S 仅 object_ref、execution_id、profile_ref，`contract/test_b_fact_records.py` 覆盖。C4 补 grounding、原图坐标、selection 继承及范围。 |
| BBoxXYXY、ImageDimensions、CoordinateSystem、TransformRecord、GroundingRecord、ProfileGroundingResult | 缺口；`definition_support.py:AssetDimensions`（S）是静态资源尺寸，`fact_schemas/models.py:CoordinateMetadata`（S）是 Fact Schema 元数据，`RuntimeRecordKind` 已列 grounding_record。C4 补感知坐标/变换和 no_target/failed/partial 等结果。 |
| FactRecord、KnownFact、UnknownFact、ErrorFact、NotApplicableFact、NotEvaluatedFact | 已有 `runtime/facts.py` 五个严格运行根，S(runtime.facts.各同名符号) 和按 status 判别的 S(runtime.facts.FactRecord)。`contract/test_b_fact_records.py`、`negative/test_b_authority_models_invalid.py`、`wp01b/a1/contracts.json` 有证据。C4 复用，C5 保持 A/B wire 和状态。 |
| FactProducer、FactScope、FactResult | 映射：定义侧 `producers.py:Producer` 与运行来源 `runtime/facts.py:FactSource` 各有 S；`definition_enums.py:FactScope` 配合 `runtime/facts.py:ScopedRecord` 的 object/execution 条件字段，S(runtime.facts.FactRecord) 可见。已有 FactRecord 表达单条主 Fact 的结果，N04 批内结果仍待交付。证据 `contract/test_b_producers_contracts.py`、`contract/test_b_fact_records.py`。C4 对接，见 C1-03。 |
| AuthoritativeResultSelection | 已有 `runtime/authority.py` 同名根联合及 3 个 selection_group 分支，各有 S；`contract/test_b_fact_records.py`、`contract/test_b_authority_history.py`、`wp01b/a1/contracts.json`、`wp01b/a2/contracts.json` 覆盖版本、作用域、修订和有限凭据。C4 对接实际结果记录与 Guard/Decision，历史注入验证的边界见 C1-02。 |
| ToolFactOutputDeclaration、ToolFactOutputMapping、FactSelector | 已有 `tool_outputs.py` 两类出现在 ToolDefinition/ToolPlanDefinition 的对应 S 下；`fact_selectors.py:FactSelector` 有 S。`contract/test_b_models_tools.py`、`contract/test_b_fact_selectors.py`、`negative/test_b_mapping_invalid.py`。C3 消费，C4 引用。 |
| ToolExecution、GroundingBindingOrigin、ToolPlanStepOrigin | 有限；S(validation.authority_inputs.ToolStepResultDescriptor) 仅 tool_plan_step、必填实际版本、映射 outputs；S(definitions.capabilities.GroundingBinding) 是配置。C4 补执行记录的来源联合和公共 refs，见 F05。 |
| ToolResult、ToolOutputState、ToolAttemptResult、ToolError、ToolResourceUsage | 缺口；`schema_types.py:ToolResultSchemaDescriptor` 是声明的 Schema 描述符，`validation/authority_inputs.py:MappedOutputResult` 是已产出 Fact/显式可选缺席两分支的有限视图，各自 S 入口为 S(schema_types.SchemaDescriptor)、S(validation.authority_inputs.MappedOutputResult)。`contract/test_b_mapping_contracts.py`、`contract/test_b_fact_records.py` 覆盖局部形状。C4 补 payload、命名状态、诊断/资源等完整数据。 |
| EvidenceAsset、EvidenceScope、StorageReference、RetentionPolicyRef、AssetStage | 缺口；现有 `runtime/facts.py:EvidenceRef`、`definition_enums.py:EvidenceType`、`policies.py:EvidencePolicy`，另有 definition-side StaticAssetDescriptor。C4 补存储及 staged/finalized 状态契约，无上传实现。 |
| ModelInvocation、InvocationIdentity、ConfiguredProviderIdentity、ActualProviderIdentity、ProviderRouteTrace、InvocationOutcome、InvocationError、UsageRecord | 缺口；`runtime/facts.py:ModelInvocationRef` 已出现在 FactSource 的 S 中。定义侧 ProviderPolicy/FallbackPolicy 和 review views 提供配置证据。C4 补真实身份/版本解析、请求关联、来源路由、调用目的和审计，见 F02/F03。 |
| ProfileEligibilityEvaluation | 缺口；ArtifactKind/NodeId 无需扩展，RuntimeRecordKind 已列该记录。C4 补 Workflow 所有的配置全集、三分区与 trace refs；Spec 01 §3.8.1 / 03 §3.1。 |
| RuleSetApplicabilityEvaluation | 缺口；RuntimeRecordKind 已列。C4 补 Profile 父评价引用、目标 RuleSet/trace，区分真实 N06 与祖先投影。 |
| RuleApplicabilityEvaluation | 缺口；RuntimeRecordKind 已列。C4 补 RuleSet 父评价引用、目标 Rule、真实 N06 或祖先投影。 |
| ApplicabilityTrace | 缺口；RuntimeRecordKind 已列。C4 补 pin、目标/父链、指定修订、处置与 decisive level。 |
| ApplicabilityLevelTrace | 缺口；C4 实现 ApplicabilityTrace 内嵌值（独立保存才需要根版本），评估后才有 truth/owner/source；未执行层引用 decisive ancestor。 |
| UnresolvedObligation | 缺口；RuntimeRecordKind 已列。C4 补义务的类型分支及共享 obligation_id，覆盖无 Object、父级阻断和 Coverage/Decision 引用。 |
| CandidatePrediction、CandidateCategory | 缺口；RuntimeRecordKind 已列 candidate_prediction；`runtime/candidates.py` 实际是 Fact 候选解析与快照，S(runtime.candidates.CandidateParseOutcome) 的正负 fixture 不构成 N06 候选预测。C4 补 N06 候选类别和模型归因。 |
| DecisionRecord、RuleEvaluation、RuleEvaluationOutcome、ConflictResolutionTrace、ExplanationTrace | 缺口；DecisionResult、RulePolicy、ConflictPolicy 已有。C4 按 §7/§15 补全 Rule 引用分桶、独立 Required Evidence Guard 与 predicate truth/outcome，以及 collect_all 追溯。 |
| ArtifactReplayRequest、InferenceReplayRequest、ReplayRun | 缺口；RuntimeRecordKind 已列 replay_run。C4 补种类、source/target pins、重算和重新推理的不同请求数据。 |
| NodeInputEnvelope、NodeOutputEnvelope、NodeError、ArtifactOutputRef | 缺口；RuntimeRecordRef 和 ValidationIssue 可以复用，目标通用输入/输出和有界错误契约由 C4 完成。 |
| ContextBuildInput、ContextBuildOutput、ContextValidationReport（N01） | 缺口；C4 接 ContextSnapshot 和上下文 provenance，区分 required failure 与可用 Snapshot。B 通用 ValidationReport 服务有限校验，拥有单独的 report scope。 |
| DiscoveryInput、DiscoveryOutput、ProfileHit、DiscoveryProfileDiagnostic（N02） | 缺口；Definition 约束/Policy 已有。C4 补 eligible-only/evaluation ref、固定有效 Questions、输出异常诊断、空 eligible 的 skipped/not_evaluated，以及真实 no_hits；禁止框/实例关联字段。 |
| QuestionSelectionRecord、AllDeclaredSelectionRecord、ContextRuleSelectionRecord、ModelAdvisorySelectionRecord、FallbackSelectionRecord、FallbackSelectionEvidence（N02） | 有限；`validation/authority_inputs.py:QuestionSelectionDescriptor` 有 S，仅选择 ref、Execution/Profile、资格 qualification 和有效 refs。`contract/test_b_fact_records.py`、`wp01b/a1/contracts.json` 有证据。C4 补 method 分支、发现/额外调用、pins 和 fallback 证据。 |
| SelectionCallPolicy、SharedN02SelectionCallPolicy、AdditionalSelectionCallPolicy（N02） | 已有 `policies.py` 三个符号，S(policies.SelectionCallPolicy) 及两个内嵌分支；见 F03 和 `contract/test_b_complete_policies.py`。C4 直接复用配置与预算；补调用证据。 |
| GroundingInput、GroundingOutput、ModelGroundingProvenance、ToolGroundingProvenance（N03） | 缺口；S(definitions.capabilities.GroundingBinding) 提供定义分支，有限 ObjectDescriptor 可作消费者。C4 补独立 grounding、继承 selection、实际 ModelInvocation/ToolExecution 的 provenance。 |
| ObservationInput、ObservationBatch、QuestionResult、QuestionValidationResult（N04） | 有限；`validation/authority_inputs.py:ObservationResultDescriptor, QuestionPrimaryCandidate` 及 S(validation.authority_inputs.ObservationResultDescriptor) 仅批引用、实际调用引用、有效 Questions、envelope/association/arrival qualification 和 Fact 候选引用。`contract/test_b_fact_records.py`、`wp01b/a1/contracts.json` 覆盖界面。C4 补正常路径一对象一批次、逐 Question 验证以及可信关联。 |
| ToolNodeInput、ToolNodeOutput、ToolPlanRunSummary（N05） | 缺口；`ToolStepResultDescriptor` 和 ToolPlanDefinition Schema 可复用，C4 补 N05 节点数据并保持 ToolPlan 独立于候选。 |
| DecisionInput、DecisionOutput、MandatoryRuleCoverage（N06） | 前两项缺口；MandatoryRuleCoverage 同上并由 N06 引用，C4 补候选/Rule 结果隔离、mandatory 扫描及完整性数据。 |

## §5 Registry/AST 与 §7 操作符

| 计划符号 | C1 结论、源码和 Schema | 已有静态证据 / 归属 |
|---|---|---|
| FactSchemaDefinition、ValueSchemaGenerationSpec、GlobalTypeLimits、ValueSchemaGenerationInput | 已有 `fact_schemas/models.py` 同名模型；各有 S(fact_schemas.models.同名符号)。 | `contract/test_b_fact_schema.py`、`negative/test_b_fact_schema_invalid.py`、`unit/test_b_projection.py`、`wp01b/jcs_vectors.json`；C3 复用，C5 回归 |
| TypeDefinitionProjection | 映射到 `fact_schemas/models.py:normalize_type` 的有界规范化 `values.py:TypeDefinition`；S(fact_schemas.models.ValueSchemaGenerationInput) 的 projection 指向 TypeDefinition 嵌套联合。 | 同上；保留规范化和独立 child digest 逻辑 |
| FactSchemaCatalogue | 有限；`fact_schemas/registry.py:FactSchemaRegistryBuilder, FactSchemaRegistry` 实现不可变内存目录、精确解析及校验。没发现同名模型或持久目录 Schema。 | `unit/test_b_registry.py`、`contract/test_b_registry_contract.py`、`negative/test_b_registry_invalid.py`；C3 消费，C4/C5 按 C1-03 判定独立 wire |
| ProducerCatalogue、FactProducerDeclaration | 已有 `producers.py` 同名模型，S(producers.ProducerCatalogue)、S(producers.FactProducerDeclaration)，后者为嵌套定义组件。 | `contract/test_b_producers_contracts.py`、`negative/test_b_producers_invalid.py`、`wp01b/x2/contracts.json`；C3 复用 |
| QuestionProducerSelector、ToolProducerSelector、SystemProducerSelector | 已有 `fact_selectors.py` 同名模型，S(fact_selectors.ProducerSelector) 内三分支。 | `contract/test_b_fact_selectors.py`、`negative/test_b_fact_selectors_invalid.py`；C3 复用 |
| OperatorDescriptor、TypeCheckResult | 缺口；`definition_enums.py:ComparisonOperator` 有 S；S(validation.condition_inputs.ConditionReferenceResult) 对应已有引用校验输出，仅作诊断/引用边界。 | C2 定义封闭签名表及检查输出类型；C3 交付纯检查入口和测试，C5 导出约定的入口 |
| Literal、FactRefExpr、RecordFieldRefExpr、ContextRefExpr | 已有 `conditions.py:LiteralExpr, FactRefExpr, RecordFieldRefExpr, ContextRefExpr`，S(conditions.ConditionExpr) 内分支。同一字段路径由 FactSelector/ContextFieldSelector 承载，值约束由 ValueContract/TypeDefinition 表达。 | `contract/test_b_conditions_schema.py`、`unit/test_b_conditions.py`、`negative/test_b_conditions_invalid.py`、`wp01b/x1/contracts.json`；C3 补推导、枚举身份、路径 |
| eq、ne、lt、le、gt、ge | 已有 `conditions.py:ComparisonExpr` 的 S(conditions.ConditionExpr) 嵌套结构及显式枚举；操作数类型/单位兼容缺口。 | 同上；C2 签名、C3 语义检查 |
| in、all、any、not | 已有 `conditions.py:InExpr, AllExpr, AnyExpr, NotExpr`，S(conditions.ConditionExpr) 内不同分支；完整推导、根为 Boolean 等语义检查缺口。 | 同上；C2 签名、C3 类型检查 |
| 批准的追加节点 LocalToolFactSelector、LocalToolFactRefExpr 及 ConditionInputDomain | 已有 `conditions.py` 中 selector/node，分别有 S(conditions.LocalToolFactSelector) 和 S(conditions.ConditionExpr) 嵌套；`definition_enums.py` 的 ConditionInputDomain 三域有 S。`validation/conditions.py:validate_condition_references` 对使用位置绑定，不执行谓词。 | `contract/test_b_condition_bindings_schema.py`、`unit/test_b_condition_bindings.py`、`negative/test_b_condition_bindings_invalid.py`、`wp01b/x4/support.py`；C3 保持仅 tool_condition 内有效、以当前 Plan 严格祖先映射输出绑定 |

## §5 R2 补充表

| 计划符号 | C1 结论、源码和 Schema | 已有静态证据 / 归属 |
|---|---|---|
| ArtifactKind | 已有 `enums.py`，S(enums.ArtifactKind)；基础表同项。 | `unit/test_foundation.py`；C5 保持现有 25 种 |
| PromptTemplateDefinition、PromptVariable | 已有 `definitions/prompts.py`，S(definitions.prompts.PromptTemplateDefinition) 及其 PromptVariable 分支。 | `contract/test_b_capabilities.py`、`unit/test_b_prompts.py`、`wp01b/o1/contracts.json`；C5 |
| VocabularyDefinition、StaticAssetDescriptor | 已有 `definitions/resources.py`，S(definitions.resources.VocabularyDefinition)、S(definitions.resources.StaticAssetDescriptor)。 | `contract/test_b_ontology.py`、`negative/test_b_ontology_invalid.py`、`wp01b/s1/contracts.json`；C5 |
| GeometryResourceDefinition | 缺口；`definition_support.py:GeometryResourceContentRef` 位于 S(definitions.ontology.RegionDefinition) 下，Kind 已有。几何资源实体及有界几何/provenance 至今缺失。 | 现有 Region fixture 证明引用形状；C4 先解决 C1-04，C5 核对根和正负 fixture |
| ReviewReportRef、ExternalCapabilityRequirement、VersionConstraint | 已有 `definition_support.py` 同名模型及各自 S。 | `contract/test_b_ontology.py`、`unit/test_b_support.py`、`wp01b/s1/contracts.json`；C4 复用 |
| DeploymentRequirement | 已有 `policies.py` 同名模型，S(policies.ProviderPolicyArtifact) → DeploymentRequirement。 | `contract/test_b_policy_contracts.py`、`wp01b/p1/contracts.json`；C4 复用 |
| QuestionSelectionPolicy、ContextSelectionRule | 已有 `policies.py` 同名模型，S(policies.QuestionSelectionPolicyArtifact) 的 policy 与 ContextSelectionRule 子定义。 | `contract/test_b_complete_policies.py`、`negative/test_b_complete_policies_invalid.py`、`wp01b/x1/contracts.json`；C4 复用 |
| QuestionSelectionRecord、ExcludedQuestionReason | 运行记录有限，已有 QuestionSelectionDescriptor 的 S；排除原因和与有效集合、选择策略的对应契约缺失。 | `contract/test_b_fact_records.py`；C4，与 N02 同一记录族 |
| PolicyArtifact、PolicyKind | 已有 `policies.py:PolicyArtifact` 及 `definition_enums.py:PolicyKind`；S(policies.PolicyArtifact) 封闭 16 分支。 | `contract/test_b_complete_policies.py`、`contract/test_b_schema_inventory.py`；C5 |
| ApplicabilityPolicy、UnknownPolicy、QuestionValidationPolicy、EvidencePolicy、ToolExecutionPolicy、ToolFailurePolicy、ToolSkipPolicy、ResourceRequirements | 已有 `policies.py` 八种配置；分别导出 S(policies.ApplicabilityPolicyArtifact)、S(policies.UnknownPolicyArtifact)、S(policies.QuestionValidationPolicyArtifact)、S(policies.EvidencePolicyArtifact)、S(policies.ToolExecutionPolicyArtifact)、S(policies.ToolFailurePolicyArtifact)、S(policies.ToolSkipPolicyArtifact)、S(policies.ResourceRequirementsArtifact)，配置嵌套在 policy。 | `contract/test_b_complete_policies.py`、`unit/test_b_complete_policies_binding.py`、`negative/test_b_complete_policies_invalid.py`；C2/C3/C4 复用，C5 回归 |
| ProviderPolicy、FallbackPolicy、RulePolicy、ConflictPolicy、CoveragePolicy、ContextProvenancePolicy、ExecutionPolicy、QuestionSelectionPolicy | 已有 `policies.py` 八种配置；分别导出 S(policies.ProviderPolicyArtifact)、S(policies.FallbackPolicyArtifact)、S(policies.RulePolicyArtifact)、S(policies.ConflictPolicyArtifact)、S(policies.CoveragePolicyArtifact)、S(policies.ContextProvenancePolicyArtifact)、S(policies.ExecutionPolicyArtifact)、S(policies.QuestionSelectionPolicyArtifact)。 | 同上及 `contract/test_b_policy_contracts.py`；C2/C3/C4 复用，C5 回归 |
| ProducerCatalogue、FactProducerDeclaration、QuestionProducer、ToolStepProducer、SystemProducer | 已有 `producers.py` 同名模型；目录与声明各有 S，三个 Producer 在 S(producers.Producer) 内。 | `contract/test_b_producers_contracts.py`、`unit/test_b_producers_validation.py`、`negative/test_b_producers_validation_invalid.py`；C3/C4 复用 |
| ToolFactOutputDeclaration、ToolFactOutputMapping、ProducerSelector | 已有 `tool_outputs.py` 的声明/映射作为 Tool/Plan 嵌套值；`fact_selectors.py` 的联合有 S(fact_selectors.ProducerSelector)。 | `contract/test_b_models_tools.py`、`contract/test_b_fact_selectors.py`、`negative/test_b_mapping_invalid.py`；C3/C4 复用 |
| AuthoritativeResultSelection、ActorRef | 已有 `runtime/authority.py`；S(runtime.authority.AuthoritativeResultSelection) 包含 ActorRef。 | `contract/test_b_fact_records.py`、`contract/test_b_authority_history.py`、`wp01b/a1/contracts.json`；C4 复用 |
| FactSchemaDefinition.semantic_key/type_definition/payload_schema_ref | 已有 `fact_schemas/models.py:FactSchemaDefinition` 的三个字段；payload_schema_ref 为精确引用独立 value_schema 的分支。 | `contract/test_b_fact_schema.py`、`unit/test_b_projection.py`、`wp01b/jcs_vectors.json`；C5 |
| RecordFieldRefExpr、ContextFieldSelector | 已有 `conditions.py:RecordFieldRefExpr` 在 S(conditions.ConditionExpr) 下；`definition_support.py:ContextFieldSelector` 单独导出，复用统一 FieldPath。 | `contract/test_b_conditions_schema.py`、`contract/test_b_fact_selectors.py`、`contract/test_b_mapping_contracts.py`；C3 |
| InvocationContractSnapshot | 缺口；ModelBinding.output_schema_ref、Question PromptTemplate 的响应契约及 ObservationResultDescriptor 的 effective refs 各自存在，缺记录请求时配置输出 Schema、compiler 版本、派生响应摘要和证据的完整嵌套值。 | `contract/test_b_models_tools.py`、`contract/test_b_fact_records.py`；C4，随 ModelInvocation 导出 |

## §5 R2.1 / R2.1.1 字段核对

| F 项 | 已有证据和待实现字段 | 归属 |
|---|---|---|
| F01 applicability | `policies.py:ApplicabilityPolicy` 与 `validation/conditions.py` 覆盖配置/引用。运行态六项逐一在上表登记。必须补三级 target/policy/input refs、evaluated/NE 与 truth 限制、owner/source/decisive ancestor、Coverage/Decision 的 applicability_trace_refs 和选定 revision。配置/范围见 `contract/test_b_complete_policies.py`、`contract/test_b_condition_bindings_schema.py`。 | C4；C3 负责 AST 输入域，C5 核对 G12 |
| F02 Provider | S(policies.ProviderPolicyArtifact) 已有 routing_mode、primary_deployment_id、selection、LocalPrimaryException 和 DeploymentRequirement.provider_id；`contract/test_b_policy_contracts.py` 及 `wp01b/p1/contracts.json` 检验两个 primary 分支。ConfiguredProviderIdentity、ActualProviderIdentity、ProviderRouteTrace 和版本 resolution 缺口。 | C4，C5 |
| F03 Question selection | S(policies.SelectionCallPolicy) 已有 shared_n02/explicit_additional；后者具备 proposal binding、resource policy 及正数预算字段。运行 union 及 discovery_model_invocation_ref、条件调用 ref/mode、FallbackSelectionEvidence、ModelInvocation.purposes/source_discovery_invocation_ref 缺口。邻接证据为 `contract/test_b_complete_policies.py`、`contract/test_b_fact_records.py`。 | C4，C5 |
| F04 projection | 已有 FactSchemaDefinition.value_schema_generation、ValueSchemaGenerationSpec/GlobalTypeLimits、规范化的 TypeDefinitionProjection 和 ValueSchemaGenerationInput；`fact_schemas/projection.py:generate_value_schema, validate_child_reference` 计算并匹配独立 payload_schema_ref。S(fact_schemas.models.FactSchemaDefinition)、S(fact_schemas.models.ValueSchemaGenerationInput) 等导出工程形状。向量、规范化、child digest 证据见 `wp01b/jcs_vectors.json`、`unit/test_b_projection.py`、`contract/test_b_fact_schema.py`。 | C3 复用；C5 回归 |
| F05 ToolExecution | ToolStepResultDescriptor 的 S 表示有限 N05 候选关联，已区分 tool_plan_step 与纯配置 grounding binding，实际版本必填。ToolExecution 公共 refs、actual-version resolution、GroundingBindingOrigin/ToolPlanStepOrigin 运行联合尚缺；C4 验证 N03 定位来源不进入 N05 Tool producer authority。邻接证据见 `contract/test_b_fact_records.py`、`contract/test_b_producer_authority_contracts.py`、`wp01b/a1/contracts.json`。 | C4，C5 |

## Findings 与批次门禁

| Finding | 待交付证据 |
|---|---|
| C1-01 规则定义与语义 TypeChecker | C2 补规则定义、可枚举签名和类型检查结果。C3 复用既有 AST 与引用校验，补输入域、类型/单位/枚举/路径、root Boolean、事实依赖一致性，以及同 Plan 本地祖先引用的负向用例。 |
| C1-02 有限 authority 视图与持久运行记录 | C4 保留 `ObjectDescriptor`、`NodeRunDescriptor`、`AttemptDescriptor`、`QuestionSelectionDescriptor`、`ObservationResultDescriptor`、`ToolStepResultDescriptor` 和 `ExecutionDefinitionPin` 的 B 输入 wire，明确各视图如何从 C4 实际记录取得有限输入。覆盖批次/Attempt/组完整性、已选修订、真实与投影引用、实际版本 unavailable、无 Object/N06、空 eligibility 与 mandatory 覆盖。实际历史完备性、外部真实性、Consumer 权威执行和服务联通性由后续工作包提供证据。 |
| C1-03 名称/根边界需收口 | PersistedContract、ArtifactIdentity、PrimaryFactOutput、StepDependency、ConditionalBranch、OutputBinding、FactProducer、FactResult、TypeDefinitionProjection、FactSchemaCatalogue 应保留表中的现有实现和语义分界。C2/C3/C4 在消费位置确认映射；C5 对照 G1 记载有独立根、仅嵌套、或纯工程组件的依据。独立 ArtifactIdentity、通用 OutputBinding 或独立 FactSchemaCatalogue 的新 wire 需要明确消费者与契约；目前未找到足以冻结额外形状的规范字段。 |
| C1-04 GeometryResourceDefinition 的形状 | 01 §9 列出 ID/version、坐标系、单位、有界几何和 provenance，尚未定义可用 shape、坐标约束、大小及来源结构；既有 S1 保留引用并未冻结资源实体。C4 先核查已批准的几何形状。若需业务或兼容性裁决，只将依赖几何资源实体的任务报为待定，现有 Region 引用及独立批次可继续。 |
| C1-05 Schema 与验收闭环 | C5 对照本盘点补 C2/C3/C4 入口，区分持久根、命名联合、嵌套值及有限输入/输出。持久根含显式版本，旧 wire 不收紧，离线 Draft 2020-12 与两次导出可重复，每个已确认根具备有效/无效 fixture。有限约束和跨记录检查保留模型测试。按计划记录 G0–G12 与历史 exporter mypy 限制的实际结果。 |

C1 静态交付：本文件为唯一写入。未修改任何契约、依赖、迁移、服务或项目测试。静态盘点完成，C2/C3/C4/C5 的实现与最终 WP-01 验收仍依赖上述门禁，其中 C1-03/C1-04 的未冻结形状只影响相应条目。**STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN**。
