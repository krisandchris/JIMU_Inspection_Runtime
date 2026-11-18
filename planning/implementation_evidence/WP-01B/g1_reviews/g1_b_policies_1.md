`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`

只读核对：当前分支 `main`，HEAD 为 `51a10416d882fe85e5a1cfc5440eac6498fc48f4`；未跟踪目录为 `planning/acceptance_evidence/`、`planning/active/`、`planning/transition_evidence/`。未写文件、运行测试、修改 Git 或委派。

### G1-D01：目前不能闭合

最新授权要求“完整 Policy 数据契约和 Producer”“禁 Rule AST”“只有已批准简单声明式策略可实现”，但未批准新的简单条件模型，也未授权收窄现有条件能力。

真实依赖链：

| 契约 | 文件行依据 | 冲突 |
|---|---|---|
| ApplicabilityPolicy | `specs/01_Runtime_Domain_Model_V1.0.md:1167` | `condition_ast` 必填，无 literal-only 备选 |
| Context selection | 同文件 `:1168` | `context_rules` 必须指向 ApplicabilityPolicy，不能仅保留引用后宣称完整支持 |
| ToolSkipPolicy | 同文件 `:1174` | 允许 typed `tool_condition` 或显式 true；只实现后者会收窄支持域 |
| Producer | 同文件 `:1018–1022` | activation_condition 为 always 或 typed Tool condition；只实现 always 不完整 |
| 输入域 | 同文件 `:1108–1117` | 已规定 Applicability/Rule/Tool 的不同表达式输入域 |
| 已有规划 | `planning/active/WP-01B/PLAN.md:163–170,182`；`FINDINGS.md:14–30` | 已明确 literal-only 不算完整，且 AST 节点不在 B |

A 代码只有严格基础模型、refs、枚举和 Value/TypeDefinition，没有可复用的条件 AST。`ContractModel` 已强制 strict/frozen/extra forbid；不能通过开放字段填补。

**不能采用的“闭合”方式：**

- 只保留 PolicyKind 16 个值，实际 union 只交付 14 个模型。
- 允许 ApplicabilityPolicy 无 condition、用 true 占位或伪造空条件。
- 用 `Any`、任意 dict、字符串、未解析 ForwardRef、外部 condition ref 隐藏未实现契约。
- 将操作符/操作数组改名为 `DeclarativeCondition`；只要仍表达条件语法树，实质仍是 AST。
- 把 Policy 的条件字段当不可校验数据，仅因 JSON Schema 能生成就宣布完整。

**最小用户裁定有两个实际选项：**

1. **保留完整现有契约：** 明确授权 B 提前实现现有共享条件 AST 的纯数据结构；仍禁止 RuleDefinition、TypeChecker、Evaluator。必须承认这是对最新禁 AST 边界的有限例外，不能由技术裁决推导。沿用既有 wire，变更最小。
2. **严格保持 B 无 AST：** 明确批准 B 支持域缩减及阶段验收调整：Applicability 条件、ContextRules 的实际目标校验、ToolSkip 非 true 分支、Producer 条件分支留给 C；B 不得以原“完整”目标 PASS。若想引入一套简单声明式策略，必须由用户明确批准其准确模型和语义；现有文字不足以授权代理自造新体系。

这不是反复请求 D01–D15 总体批准，而是最新分期限制与已批准必填字段的直接冲突。未裁定前，独立的非条件 Policy 可推进，但 G1-D01 不能关闭，整包不能验收 PASS。

### G1-D04：哪些可以技术裁决

最新授权允许已批准语义内的实现细节由 Astra 裁决。建议把以下内容冻结到明确附录，注明“技术裁决”及支持域，不必把每个辅助字段重新提交用户：

- Pydantic 辅助模型名、模块位置、不可变 tuple、集合唯一性、错误路径与有限检查报告。
- `logical_model_id` 作为规范字段名；部署约束在 ProviderPolicy 的 DeploymentRequirement 中保留唯一权威来源。ModelBinding 通过 pinned policy 取得约束，避免两份可写约束。
- 对精确 SchemaRef 和已批准 TypeDefinition 的路径解析、类型包含关系、可选祖先传播。
- finite injected `SchemaDescriptor` / `ReviewMetadata` 的结构；它们不新增 ArtifactKind、不成为真实审批服务。
- 本地 ToolPlan DAG 周期检查：最新授权明确要求确定性 DAG，优先于旧 PLAN 将完整 DAG 检查留 WP-02 的限制。需更新 PLAN 的测试和边界，不扩展为 Bundle compiler。

以下不能仅以“技术实现”名义决定：

- 新增上下文来源、支持任意 JSONPath、动态计算/转换、常量脚本、未声明工具输出。
- optional→required 的隐式默认、跳过后必需 Fact 的业务补值。
- 受支持分类词表、审批角色/可信根、业务评测阈值和有效期默认。
- 缩减既有 Policy 条件语义，或新增一种简单条件语言。
- 把真实 report 解析、可信审批验证、独立质量评测标为 B 已通过。

### 推荐严格 Mapping 与 Descriptor 契约

依据：Domain `:690–711,735–750,1039–1055,1133–1148`；Nodes `:255–290`；Bundle `:132–163`。

- **FieldSelector**：闭合来源分支、exact SchemaRef、bounded field-name tuple。空 tuple 表示整值。只允许声明的 Record 字段；不支持 wildcard、JSONPath、任意列表索引或属性调用。来源分支限定规范已有输入，不能从 provenance.allowed_sources 照搬：后者描述可信元数据来历，不是映射读取地址。
- **Context selector**：分别标明 ContextSnapshot / RegionDefinition / Profile metadata 的来源；注入有限类型环境，不能为 Profile 暗增 `context_schema_ref`。
- **Tool input mapping**：明确 source selector、target input field path、required/optional；Facts 必须带已批准 ProducerSelector/FactSchemaRef，读取 validated、权威候选的要求属于运行时前提。上游工具来源必须明确 step 身份和 dependency 边，拒绝 N06 Candidate/Decision 来源。
- **Tool output mapping**：保留现有 `ToolFactOutputMapping(output_name,target_fact_key)`，target 必须等于 Tool 声明，不能做运行 alias。`value_path` 从 ToolResult.payload 开始；evidence_paths 从 evidence 区开始。不要以一个通用 path 对所有 envelope 字段通吃。
- **ResolvedFieldType**：返回准确类型、规范路径、沿途 optional 祖先，不能只返回叶类型。required leaf 位于 optional parent 中，映射源仍可能缺失。
- **SchemaDescriptor**：exact schema ref + 闭合 descriptor kind + TypeDefinition/有限规范 envelope 描述。Context/Attribute/Response/Tool IO 各自限制允许 kind。Tool descriptor 必须区分 succeeded/partial 可有 payload、failed 无成功 payload、evidence 区和 output_states；不能仅给“允许路径列表”。
- **兼容性**：无 coercion；bool≠integer≠number；enum 源值域必须包含于目标；字符串/列表/数值范围源不得宽于目标；Record 递归检查；来源可缺失时不能满足 required 目标。Facts 还必须检查 exact FactSchema identity、scope、semantic_key/units/coordinate_system，不能只比较 shape。无法证明兼容时返回 mismatch/incomplete，不能接受。
- Descriptor 是调用者提供的有限校验输入。B 能校验其内部一致性和引用一致性，不能证明外部 schema bytes 已被可信验证；实际可信载入/完整闭包留 WP-02。

### ReviewMetadata / TransitionAssessment

依据：Domain `:379–392,715–719,1176–1177`；PLAN `:155,188–201`。

建议有限输入包含：

- pinned ReviewReportRef；
- source/target 的 provider、deployment、logical model、确切 deployment/model 版本；
- capability、批准 scope、覆盖 trigger；
- 明确 verdict 和 aware UTC 有效区间；
- 显式 `validation_at`，不读隐式当前时钟；
- 与 LocalPrimaryException 的 approval_ref/exception scope 对应关系。

区分报告身份、报告覆盖内容与可信解析来源。没有注入可信元数据时，只能报告“引用格式检查完成，质量/授权验证未执行”；不能从 Bundle 自述 `approved=true` 推出可信批准。

### 必须纳入的负例

1. 未知 PolicyKind、额外字段、分支外字段（含 null）、省略必填策略值。
2. Context/Fact/Decision 来源混用；SchemaRef kind 不符；同 ID/version 不同 digest。
3. 未声明字段、JSONPath/wildcard、可选父中的 required 叶、optional 源映射 required 目标。
4. bool→integer、integer→number 隐式转换；enum/数值范围/列表边界不包含。
5. Tool 输入读取非依赖 step、依赖自身、悬空 dependency、循环 DAG。
6. Tool 输出 alias、重复 output_name、错 payload/evidence 基准、known 缺路径、non-known 带 value、未知 output state。
7. report 存在但 source/target/model version/capability/trigger/scope 不匹配；过期、尚未生效、naive timestamp。
8. remote_primary 选 local；缺 primary 或重复 deployment；remote 模式携带 local exception；local exception 能力/有效期不匹配。
9. fallback disabled 仍有 triggers/transitions；未声明 endpoint、未经覆盖的 transition、IO 不兼容；timeout/list order 被当作切换授权。
10. 本地 CPU/GPU Tool 被错误要求先访问 Remote Primary。

RemotePrimary 必须保留已有显式配置规则，不设置 `routing_mode`、primary、fallback、预算或 local exception 的默认值。Tool worker placement 与 Model Gateway routing 严格分开（Domain `:407–412`）。本次意见不是工作包批准。
