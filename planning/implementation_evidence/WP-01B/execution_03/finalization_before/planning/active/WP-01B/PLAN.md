# WP-01B — Definition & Fact Registry

Revision: PLAN-4-A，2026-11-18。调度：**DEPENDENCY_DRIVEN / D01_OPTION_A_APPROVED**。
当前执行基线c25bef668136acc651e7313ba608ec6fdefd39a5；main原基线保持不变。
字段规范附录[CONTRACT_FIELDS.md](CONTRACT_FIELDS.md)属于本唯一PLAN，不能独立扩大范围。
用户已授权 WP-01B 实施、CPU-only 安装/测试、独立 worktree/开发分支；不是仍待整个 WP 重复授权。
执行策略修订前的G1证据为STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN；
本轮按依赖推进；前段证据封存于execution_02/，已批准条件分支及整合的新证据写execution_03/。

## 1. 基线、授权和唯一计划

基线 main HEAD：`51a10416d882fe85e5a1cfc5440eac6498fc48f4`。
开发分支：`dev/wp-01b-definition-fact-registry`。
独立 worktree：`../JIMU_Inspection_Runtime_WP01B`。
原 main 的143个 tracked 文件、27个已知未跟踪规划/验收/合并文件均核对原摘要，无非预期变化。
G1初始化时只把这27个已核对文件按原字节复制到新 worktree，彼时源树未改。
execution_02曾发生Sol-B误写9个新文件到原worktree后迁回的已纠正偏差；170保护文件
及原main HEAD复核未变化，详见FINDINGS I02及审计日志，不宣称本轮从未触碰源树。
PLAN-1.1/FINDINGS/REVIEW/摘要封套保存在本轮 evidence 的 before_planning/，旧 reviews 不重写。

最新用户 Goal 原文保存在 `planning/implementation_evidence/WP-01B/USER_AUTHORIZATION.md`，
SHA-256 `0b492c517bcd40b84d84234087beed6a7019f808161580738007bf4e3a256863`。
它取代上一轮“仅 Planning”权限；G1-D01–04 与历史架构 D01–D15 不同。
历史 D01–D15 已批准，不重新审批，也不把旧 pending 标签重写为伪造历史批准。
Commit/Push/PR/Merge/main 文件修改/部署没有授权。

本文件为唯一实施计划。FINDINGS 是当前门禁，REVIEW 是实际核查记录；原始子报告不是独立实施授权。
Primary 实测 gpt-6-astra/xhigh，四个新 Sol 实测 gpt-6.1-sol/medium，原生会话证据在 agent_sessions.json。
原生 spawn 共享文件系统；worktree 由主控创建，并不自动给每个子智能体隔离。所有写入任务显式指定开发目录。

## 2. 目标、非目标与依赖

八个目标不缩减：Entity/Region；Profile/Facet/Question；ModelBinding/Tool/ToolPlan；
Policy Catalog；FactSchemaDefinition；FactProducerDeclaration；AuthoritativeResultSelection；Fact Schema Registry。
必要支持包含严格有限 Schema/Mapping 描述、Fact/Attempt/Result 关联输入、局部 value_schema 生成、CPU测试。
不重复 A，不改 A 数值和 ref wire 语义。

禁止：RuleDefinition/RuleSetDefinition、共享条件数据模型之外的Rule AST功能、TypeChecker/Evaluator、
Gate/Workflow、完整 Bundle graph/compiler/root digest/signature/admission、Release Manager、
DB/ORM/API、Model Gateway、真实模型/GPU、需求平台。
ToolPlan **本地 DAG 周期与可达性验证纳入 B**；它不等于 WP-02 全 Bundle 闭包。
RuleSet refs 可检查 kind/exact identity；不虚报 Rule body、mandatory Rule producer completeness 已验证。

模块依赖 DAG（箭头为前置→消费者）：

```text
A foundation → shared support / typed descriptors / diagnostics
shared → ontology, resources, prompt contracts, model bindings
shared → FactSchema projection + selectors + tool output declarations
shared + [G1-D01 条件数据依赖] → Policy / Producer
tool output declarations + mapping descriptors → Tool / ToolPlan
ontology + binding refs + policy refs → Profile / Facet / Question
FactSchema + Producer selectors → Fact / Attempt / Result / Authority
全部有限目录 → Registry / cross-validation → schema inventory / integration tests
```

跨定义通过 ContentRef，底层模型不反向导入 Registry/validation；共享 Tool 输出只有一个定义。
不使用全局 mutable registry，不用目录扫描/远程取回代替有限输入。仅单文件单 writer；独立模块可并行。

## 3. G1 四项裁决

### G1-D01 — option A explicitly approved; data interfaces frozen for Challenge

用户明确批准共享条件AST数据模型前移B，仍禁止RuleDefinition/TypeChecker/Evaluator。
原A/B选择与批准前审查保存在execution_02和execution_03/before；未默认选择或缩减目标。
用户随后明确批准X-C01局部Tool Fact selector，修复同Plan反向ContentRef摘要环。
完整字段、纯接口、文件写权与测试见CONTRACT_FIELDS §11–13；经定向Sol Challenge后实施。
两项均为明确范围授权，不再等待用户；保留运行器/编译器/真实API/Git出版禁令。

### G1-D02 — 局部 JCS / value_schema：范围已批准，契约技术冻结

Schema：GlobalTypeLimits、ValueSchemaGenerationSpec、TypeDefinitionProjection、
ValueSchemaGenerationInput、GeneratedValueSchema（非持久产物描述）、FactSchemaDefinition。

- 注册生成 profile/version=1.0.0，generator_id=jimu.value-schema，generator_version=1.0.0；
  dialect 固定完整2020-12 URI，canonicalization_id=JCS；无隐式选项。
- limits 显式正整数，分别不超过 A 的4096字符/256项/64字段/8层容器。
- 七类型沿用 A；只投影 payload kind/约束；Record/enum数组按 UTF-8 字节排序；
  JCS 对象键排序独立遵循 RFC8785，不把两个排序步骤混淆。
- 原始 TypeDefinition 输入先按显式 limits 补省略的 string/list 下界0和上界，再校验。
  已构造 A 类型实例按其有效约束处理；超过更小 limits 拒绝，不能截断。
- 输入只有 normalized projection + generation_spec。先 JCS input SHA-256，生成
  urn:jimu:value-schema:<projection_profile_version>:<input_sha256> 的 $id，
  再生成 closed kind/value Schema、JCS bytes、child SHA-256。
- child inventory ID 用 value-schema.<input_sha256>，version=projection_profile_version；
  exact value_schema ContentRef 指向 child_digest；父 FactSchema 再持有该引用。
  不把 parent ID/version/semantic metadata/digest、Bundle root或环境状态加入 child。
- 不缩减 A 的 StrictInt。生成边界要求数字可无损进入所选 canonicalizer：
  大整数转 binary64 前后不等即拒绝；有限 float 保留 binary64 意义。
  如库支持域更窄，必须披露并测试，不谎称安全整数区间外全部违反 RFC。
  float(huge_int)溢出也须转换为带路径的验证错误；测试2^53−1、2^53、2^53+1、2^60和极大整数，
  区分binary64可精确表示与具体库支持域；生成/bytes入口在字典化前拒绝重复JSON键。
- ordinary sort_keys/stable_json 不是 JCS。实现前审查有界 CPU canonicalizer版本/来源及
  RFC独立 golden vectors，再记录lock变化；库选择和测试未完成，不宣称兼容已验证。
- B 校验生成 child 的精确 bytes/digest。父 FactSchema exact ref 由调用者注入；
  B不计算/证明父制品真实性，不将格式合法 digest 当内容校验。父/全图/root/签名留 WP-02。

必测：RFC固定字节向量、负零/指数边界/Unicode键顺序/孤立surrogate/非有限/精度丢失；
同输入重复、父metadata变化不改child、Record/member乱序等价；错误childref/bytes拒绝。
规范来源：[RFC8785](https://www.rfc-editor.org/rfc/rfc8785)。
此为设计冻结，不是JCS测试PASS。

### G1-D03 — Fact / Attempt / Authority：字段技术冻结，按依赖验证

持久根 FactRecord、AuthoritativeResultSelection 显式 schema_version=1.0.0。
运行关联支持使用有限非持久 descriptors，不建设完整节点运行记录/存储服务。

| 支持类型 | 输入/身份和约束 |
|---|---|
| ObjectFactScope / ExecutionFactScope | execution_id；object分支另有profile_ref/object_ref；execution分支禁止Object字段 |
| FactRecord五分支 | fact_id、scope、key、exact schema、catalogue ref/declaration_id、producer identity、Attempt/source/evidence/time/trace；known必填Value，其余禁止value包括null、必填结构化reason |
| Question/Tool/System FactSource | Question关联batch+invocation；Tool仅N05 step ToolExecution；system精确capability/独立event结果，不伪造模型调用 |
| AttemptDescriptor / NodeRunDescriptor / ObjectDescriptor | 明确Attempt→NodeRun→Execution、owner/node、number/status、Object所属Profile；无N07、无生命周期执行 |
| QuestionSelectionDescriptor | exact selection ref、execution/profile、已验证资格、pinned effective_question_refs；ObservationResult集合必须与它一致，不能自证完整 |
| ObservationResultDescriptor | OBSERVATION_BATCH、同Object/Attempt、selection/effective Questions、invocation、envelope/association验证、完整候选集合 |
| ToolStepResultDescriptor | TOOL_EXECUTION、N05/tool_plan_step、plan/step/tool、同Object/Attempt、完整mapped output状态/候选/absence证据 |
| SystemOutputResultDescriptor | 既有EXECUTION_EVENT、scope/Attempt、exact system requirement/output；emitted(fact_ref)或optional_absence(reason)闭合联合 |
| AuthorityValidationInput | 上述可信有限目录、声明、Facts、完整历史selection；缺关联输入返回incomplete/error，不推断调用真实发生 |

System scope 与其声明一致；现有规范不把所有 SystemProducer 限制为 execution-only。
execution Facts 必须来自 system；N05 Tool Facts 只允许object。object system仍须完整Object/Profile归属。
System event不是通用事件承诺：同ref只能一份匹配descriptor，emitted恰一Fact；
absence仅zero_or_one、禁止Fact/value，selection.selected_fact_refs为空；不解除消费者缺必需Fact的阻塞。

Authority组身份：
N04=(execution,profile,object)；N05再加exact plan/step；
system=(execution,scope,Object/Profile若适用,registered producer identity,output_name)。
选择结果kind分别固定OBSERVATION_BATCH/TOOL_EXECUTION/EXECUTION_EVENT。
所有Object/NodeRun/Attempt/Result/Fact/Selection目录先检查scoped ref唯一内容，禁止构造dict时覆盖。
Object仍Execution-local；Fact按下文有限目录身份约束。结果descriptor必须显式提供可信
arrival_qualification=on_time/late/unresolved；late拒选，unresolved或缺字段返回incomplete/error，
即使Attempt.status=succeeded也不能代替到达资格。B不测量真实deadline、不伪证未迟到。

完整性与资格：
- 所有effective N04 Questions各恰一个primary，optional已被选入后也不能缺项。
- N05逐step检查映射输出全集，不是Tool未使用输出全集。每个mapped output有唯一状态；
  required=true/exactly_one必须恰一候选Fact（合法nonknown可接受）；optional/zero_or_one
  必须在emitted(fact_ref)与optional_absence(reason)中二选一，不同时存在。absence无Fact/value，
  已产生unknown/error Fact属于emitted且不得从selected集合丢弃。该union是校验descriptor，
  不改原ToolOutputState五态wire；状态/payload路径/absence原因必须一致。Tool required、
  Producer cardinality和step mapping相矛盾拒绝，不能从空Fact列表猜测optional成功。
- selected_fact_refs等于整个选定Result的候选集合；不得跨Attempt拼接、只挑known或最高confidence。
- 非known但结构合法的Fact可以留在可信完整批次；完整不等于全known。
- transport失败、不可信envelope/association、missing required输出不可伪造成成功批次。
- 三种selection reason均要求选定Attempt succeeded且归属/结果完整验证满足；
  manual_review/explicit_retry_resolution不能救活failed/timed_out/cancelled/superseded或late Attempt。
  这是B保守验证资格支持边界，不宣称原规格已批准全部人工恢复策略。
  若未来需要人工接纳失败/迟到结果，另定业务规则；B不提供该隐式特权。
  validated_success不代表所有Facts known或解除下游required evidence guard。
- revision首项1无previous，后续同组+1且精确previous；fork/断链/跨组拒绝。
  选择早成功Attempt合法；不自动选最新，历史Fact/Attempt/selection保留。

fact_id在本次有限目录中对整条immutable candidate单值；同时检查规范candidate复合键。
接收原始候选bytes的入口保留并严格解析：在字典化前拒绝重复键（含嵌套），拒绝非法
Unicode/非有限数/额外字段，解析完整typed content必须等于该提交Fact，bytes/model不符拒绝。
只有同键完全同bytes才算原始重复；
模型相等/稳定JSON不是传输bytes去重证据。不同bytes冲突，不覆盖旧值。
Registry返回不可变快照/显式选权视图；不实现DB CAS/fencing/幂等服务。
冲突检查只证明提交目录/快照内一致；新快照须显式携带前序历史，不能证明调用者未漏传
外部历史，也不声称全存储全局去重。FactSchema metadata及Fact/Attempt/Result/Authority字段
已在附录§5/7.1冻结；模型之外的有限关联检查须保留上述信任边界。

必测：跨Attempt cherry-pick、错scope/node/producer、optional已选缺项、必需输出缺失、
失败/late资格、错误resultkind、ID重绑/复合键冲突、revision fork、旧快照不变、system absence无假Fact。

### G1-D04 — 严格字段和 Mapping：字段技术冻结，代码按模块独立复核

下列技术约束与CONTRACT_FIELDS.md构成唯一计划；附录已完成descriptor/source union、
review resolver scope、metadata/Prompt、Model/Tool/14个非表达式Policy的具体字段及定向Challenge。
冻结是技术实现依据，不是代码验收或生产配置批准；不修改A语义。
D01仅阻塞完整表达式Policy/Producer准入；各独立模块继续实施和代码复核。

- 所有B持久根显式 Literal["1.0.0"]；ID/Version/Digest/ref使用A；branch禁止字段含null也拒绝。
  name/member等短文本1..128，描述1..4096，模板最多65536字符；普通目录集合最多1024，
  mapped fields最多64，路径最多8段，字段名沿A FieldName。预算仍规范64-bit显式范围。
- entity_type/region_type/scene_type/lifecycle用VocabularyMemberRef；精确词表/成员/semantic_domain验证。
  lifecycle仅元数据，不发明状态迁移。词表成员由调用方配置，synthetic例子不批准生产值。
- Facet.tags为有界唯一VocabularyMemberRef tuple。metadata如使用，采用有界namespace+明确
  attribute_schema_ref+受限RecordValue，调用者提供exact typed descriptor并做双阶段验证；
  不支持无Schema metadata、任意dict或未声明键，不静默丢弃optional字段。
- ContextFieldSelector.source=context_snapshot/region_metadata/profile_metadata，
  exact context SchemaRef +非空field_path；这些是读取域，不能复用provenance五种来源枚举。
  不给Profile添加context_schema_ref；类型环境由显式注入且绑定Region/Profile身份。
- FieldSelector为exact schema_ref +字段tuple；空path仅在通用/Fact整体值选择时允许。
  path逐个精确字段名解析，("a.b",)是一个字段名，不是JSONPath；无索引/wildcard/跨Object。
- ResolvedFieldType保存type_definition、field_path、optional_ancestor_paths（含可选叶）。
  SchemaDescriptor明确表示层raw DTO/typed Value，typed record语义路径与wire kind/value包装不混用。
  ToolIO descriptor封闭描述normalized payload/evidence/output_states及成功/失败/partial分支。
  仅有限typed树，不导入 arbitrary JSON Schema，不以路径字符串列表证明schema结构/真实性。
- InputBinding：来源为声明Context、当前Object/image引用、FactSelector或已声明祖先step输出；
  target为Tool input Schema中的明确字段路径；required/on_missing显式；禁止N06来源。
  Fact来源必须保留schema/producer/scope身份；上游step必须为DAG祖先。无计算/转换脚本。
- required target允许可选source，但必须on_missing=block；optional target才可omit。
  不补null/false/默认值；unknown/error/noauthority与字段缺失分开诊断。
  重复/父子重叠target写入拒绝，所有必需target有确定映射。
- 类型兼容只做可证明包含：kind/表示层一致，enum源子集、数值/string/list边界不宽于目标、
  Record逐字段递归/required与closed extra一致；无法证明拒绝。无int→number coercion。
  Fact语义仍要求exact FactSchema和scope/units/coordinate_system，不以shape替代。
- ToolFactOutputDeclaration/Mapping沿Domain §13字段；target key必须等声明key。
  value_path只从ToolResult.payload解包；evidence paths另有显式基准。known必须有值，
  nonknown该path禁止值，缺state/path不推导unknown。只选择已声明的named outputs。
- ToolStep持有step_id、exact tool_ref、unique depends_on、input_bindings、
  fact_output_mappings、requirement、failure/skip policy refs；本地检测空/单步/分叉/汇合、
  self/两步/长cycle、悬空/重复依赖、非祖先输入。无执行器/拓扑调度。
- ModelBinding.logical_model_id为受限规范ID；部署版本约束权威在pinned ProviderPolicy。
  remote_primary必须显式配置；local例外、fallback审查、预算均无行为默认。
  Tool worker位置与模型provider路由分离。
- ReviewMetadata/TransitionAssessment注入pinned report、exact from/to deployment/model版本、
  capability/scope/trigger、verdict/UTC有效窗；validation_at显式。报告存在不等于覆盖该转换，
  未注入可信材料就不报review/quality PASS。不实现真实信任解析/质量评测。
- Prompt仅声明命名占位符（技术语法冻结为{identifier}，转义{{/}}）；变量名ASCII标识1..128、
  unique注册、模板使用域一致；partials exact refs且有限目录无环。无loops/expression/render服务。

新wire约束仅作用于本轮新增、尚未发布的B类型；示例仍illustrative。既有A、历史已发布制品不迁移。
geometry正文未在B冻结，保留exact geometry_resource ref及明确未执行内容检查，不假装几何语义PASS。

## 4. Pydantic / 纯接口清单

| 模块路径（domain/） | 模型与交付 | writer |
|---|---|---|
| definition_support.py；validation/issues.py | ReviewReportRef、ExternalCapabilityRequirement、VocabularyMemberRef、VersionConstraint、ContextFieldSelector、bounded路径/UTC/预算；ValidationIssue/Report | A先交共享基础 |
| schema_types.py；validation/schema_descriptors.py；review_inputs.py | FieldSelector/ResolvedFieldType/表示层纯类型放低层schema_types.py；validation仅有限目录校验及Review输入，底层模型不得导入validation | B；其他消费者前交付 |
| definitions/ontology.py；resources.py | EntityDefinition、RegionDefinition、VocabularyDefinition、StaticAssetDescriptor、受限FacetMetadata辅助 | A |
| definitions/capabilities.py；prompts.py | Profile/Facet/Question、Model/ToolGroundingBinding、PromptTemplate/PromptVariable | A |
| definitions/models.py；tools.py | ModelBinding、ToolDefinition、ToolPlan/ToolStep/InputBinding/StepDependency | B |
| policies.py | PolicyArtifact +16 Policy；ContextSelectionRule、SelectionCallPolicy两分支、ContextFieldRequirement、DeploymentRequirement、FallbackTransition、LocalPrimaryException | B；条件部分待D01 |
| fact_schemas/models.py；projection.py | FactSchemaDefinition、GenerationSpec/Input/Projection/Limits、GeneratedValueSchema | C |
| fact_selectors.py；tool_outputs.py | FactSelector、Question/Tool/SystemProducerSelector、ToolFactOutputDeclaration/Mapping | C先交，B复用 |
| producers.py | ProducerCatalogue、FactProducerDeclaration、Question/ToolStep/SystemProducer | C；conditional待D01 |
| runtime/facts.py；authority.py | 五Fact分支/source/reason/scope、AuthoritativeResultSelection三组、ActorRef | C |
| validation/authority.py | typed Attempt/NodeRun/Object/Result descriptors、完整性/历史验证 | C |
| fact_schemas/registry.py；validation/definitions.py；producers.py | builder/freeze/exact resolve、payload/path/scope/producer/authority有限验证 | D |
| scripts/generate_schemas.py；schemas/generated/** | 全B模型/union索引、Pydantic2020-12 --write/--check | D |

PolicyKind恰16：applicability、question_selection、unknown、question_validation、evidence、
tool_execution、tool_failure、tool_skip、resource_requirements、provider、fallback、rule、
conflict、coverage、context_provenance、execution。G1-D01不关闭就不能把其完整union标完成。
不新增ArtifactKind/RuntimeRecordKind；不把support descriptor注册成新artifact。
B的新enum集中在definition_enums.py，不改A的enums.py；A首次交付后显式移交B/C，禁止并发写。

纯接口：
register(exact_ref, definition) → builder；freeze() → immutable registry；
resolve(exact_ref)；validate_payload(ref, raw_value) → A Value；
describe_type(ref, field_path) → ResolvedFieldType；
validate_fact/validate_producers/validate_authority接受完整typed finite context返回ValidationReport。
报告区分实际checks、incomplete、deferred；不以一个bool掩盖未检内容。

## 5. 执行顺序和所有权

各任务自己的契约/输入/Challenge通过后按依赖推进，不再逐文件求批，也不要求G1全局先关闭：
1. A共享支持/ontology + B descriptors（不共享写文件）；主控复核接口。
2. C FactSchema/projection/selectors/tool outputs；A vocabulary/prompts可独立。
3. B Policy/Model/ToolPlan；A capabilities；C producer与runtime支持在接口齐全后。
4. D Registry/cross-validation/integration/schema inventory。
5. 模块targeted tests → 一次全量A+B验收 → Primary直接读全部核心代码/diff/logs/schema/lock →
   独立Sol Challenge → 定向整改/复验 → 最终报告。
目标包含新用户G2–G5所有要求，顺序调整只为依赖，不缩减模块。

每次委派给出worktree、批准PLAN revision、允许文件、输入接口、输出、精确测试命令。
agent completion不等于验收；不得自行扩大scope/修改规范/测试掩盖问题/操作Git出版。
证据目录各Sol使用独立logs子目录，Primary拥有汇总封套。src A原有语义文件只读；
enums与import测试确需增量时单独交接，不重构A来绕过困难。

## 6. 测试与验收

下列为完整验收设计。execution_02已实际执行模块测试，当前结果见TASKS与各日志；
当前已实现A+B完整回归已执行：1455 passed/0 skipped，见execution_02最终日志；
未实施的X1/X2/X3不在该测试通过声明内，A历史324passed不被冒用为B结果。

- Domain：所有根逐字段缺失/额外字段/错误类型/深不可变；exact refs、词表/domain、
  Profile/Facet/Question精确交集、同shape异digest、单primary、无反向循环。
- Policy：16分支完整fixture及所有branch forbidden字段（含null）；显式行为/预算、
  remote/local例外/fallback覆盖、Context source/provenance分离。条件支持随D01裁定。
- Mapping/DAG：source-target所有kind/范围/enum/Record/表示层，optional祖先+block/omit，
  未声明路径、重复/重叠目标、缺required输入、禁止N06源、self/长环/非祖先step。
- FactSchema：七type/Record/enum/list/边界；JCS独立向量与局部digest；父child解耦，
  输入规范化/库能力边界、错误childrefs/非法schema、无远程$ref。
- Producer：Question1:1，ToolStep1:N、仅声明输出、schema/key/id/version/scope一致；
  collision domain、conditional边界、FactSelector路径+producer唯一匹配。
- Authority：whole effective batch/step、required/optional、非known合法Fact、missing不同error、
  Attempt归属/资格、系统emitted/absence、revision完整、FactID+candidate双冲突、noLWW、历史保持。
- Registry：register/freeze/query/version并存、未知ref/错digest/value/producer/scope/authority拒绝；
  缺上下文不得报全验证PASS，旧snapshot不随新注册/新候选变。
- Schema：每根/union生成索引、2020-12离线校验、repeatability、结构+严格域+关联三层。
  保留A外审F01 integer 1.0 / number 1的JSON Schema/Pydantic差异，不放宽A。
- 工程：全部A回归、Ruff/mypy、Domain import无SDK/服务/GPU/循环；
  ImportFrom子模块及重导出边检测，不只检查module文本。无skip/xfail/ignore规避。

使用A已锁CPU环境，先记录实际版本/lock；新canonicalizer须限定并评审再锁，不全栈升级。
未来命令：

```bash
uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q --junitxml=planning/implementation_evidence/WP-01B/pytest_results.xml
uv run --locked --no-sync ruff check .
uv run --locked --no-sync ruff format --check src tests scripts
uv run --locked --no-sync mypy src/jimu_runtime
uv run --locked --no-sync python scripts/generate_schemas.py --check
```

当前生成器已交付；上述最后命令实际结果以整合日志为准。
格式范围明确为维护中的src/tests/scripts。首轮`ruff format --check .`实际失败仅涉及三份
已封存audit_schemas.py；保留原失败与原字节，不加ignore、不重命名或格式化历史证据，
也不宣称全目录format已通过。Ruff lint仍检查整个`.`。模块测试仅相关路径，最终完整验收后只因变更/失败/新风险复验。
日志精确记录UTC起止、cwd、command、exit、stdout/stderr、JUnit counts/skips、源码/lock hashes。
测试数据synthetic；不证明模型质量、真实审批、调用、并发CAS或生产容量。

PASS必须：G1四项闭合；八模块完整；所有必需pytest/Schema/Ruff/format/mypy实际通过；
无C0/C1/C2；无未授权架构变化/越界；Astra实际代码审查及Sol独立Challenge完成；
证据完整，原A/WP00历史未改。Runtime Acceptance=N/A，不伪称Runtime通过。
最终交付FINAL_ACCEPTANCE_REPORT.md与implementation_evidence/WP-01B完整目录，
PASS后更新WORK_PACKAGE_STATUS并停止，推荐C但不开始；Git出版另需用户授权。

## 7. 当前状态、风险和回退

G1-D02–D04的字段契约已完成技术收口和定向Challenge，代码与证据按任务分别验收；
G1-D01已获用户明确方案A授权；共享条件AST数据模型前移B，未批准TypeChecker/Evaluator。
整体完整验收仍需D01闭合；各模块状态以TASKS.md和execution_02实际证据为准，Runtime N/A。
不得生成名为最终验收的PASS报告替代尚未完成的代码和测试。

风险：表达能力暗中缩减、descriptor冒充可信schema、authority只检ID、JCS普通排序伪装、
层次missing与false混淆、复杂后续图检查误报已完成。通过明确边界、独立vectors、
exact refs、完整关联输入、单写入者和实际审查控制。
回退以保留历史的定向修订为主；不reset/restore/删用户文件，不改main。

## 8. Dependency-local execution schedule (current authority)

| Task | Dependencies | D01 dependency | Writer / delivery |
|---|---|---|---|
| S1 shared strict types + ontology/resources | A foundation, Fields§1 | none | Sol-A |
| S2 bounded schema descriptors/mapping/review inputs | S1, Fields§2–4 | none | Sol-B |
| F1 projection/JCS/FactSchema contracts | A foundation + S1, Fields§5 | none | Sol-C |
| O1 Profile/Facet/Question/Prompt shapes | S1, S2 descriptor interfaces, Fields§1/6 | none for data shapes | Sol-A |
| M1 ModelBinding/Tool/ToolPlan data+DAG | S1/S2 + F1 shared output/selector interfaces | none for pinned refs/local DAG | Sol-B |
| P1 non-expression individual Policies | S1/S2 | none; no incomplete PolicyArtifact alias | Sol-B |
| R1 schema register/query/payload/path | F1/S2 | none | Sol-D |
| V1 finite Definition/Vocabulary/Prompt relation checks | O1/M1/P1/F1/S2 | none for available contracts; expression checks deferred | Sol-D |
| A1 Fact/Result/Attempt/Authority data and pure relation checks | S1/F1 + Fields§7 | no AST; complete producer admission remains blocked | Sol-C |
| A2 bounded candidate parse/history/local authority relations | A1/M1/R1, Fields§7.2 | none for finite history; full admission remains deferred | Sol-C |
| E1 initial Schema inventory/export | implemented S1–V1/A2 | historical partial inventory retained in execution_02 | completed; E2 extends it |
| X1 full condition data and16 Policy union | optionA approved, Fields§11 | resolved | Sol-B; local review PASS |
| X2 full FactProducerDeclaration/catalog validation | X1 + M1 + X4 reference helper | resolved | Sol-C; implementing |
| X3 full finite Producer/Authority/Registry integration | X2 + all independent tasks | resolved | Sol-D; implementing |

D01-independent validators report exact performed/deferred checks. Pins to blocked definitions
remain refs, never fake empty objects, unchecked Any or substitute release-ready admission.
Fact/Authority local shape tests may proceed, but cannot assert full producer-authorized acceptance
without X2. Full Goal acceptance remains unchanged; partial work does not become overall PASS.

Order: freeze/Challenge S1 and F1 contracts first, implement S1; independently refine S2/A1.
After S1 handoff, F1 and S2 can run in parallel on disjoint files. Code belongs to Sol;
Primary reads actual outputs and tests before unlocking consumers. No previous read-only
whole-repository audit is repeated; Challenges target only new/revised exact contracts.

No automatic Goal pause because X1/X2 are blocked. Continue every ready independent task.
The historical engine paused state was restored to active by the user on this continuation.
No new Goal, reset, scope reduction or implicit option B was performed.


## 9. Option A continuation — current scheduling authority

用户明确选择G1-D01 A，原回复见execution_03/AUTHORIZATION.md。D01不再是用户范围阻塞。
旧§3/§8及execution_02中“待批/BLOCKED_D01”是该批准前的状态；由本节替代当前调度。
共享条件AST只是已批准节点/操作符的数据契约，不提供RuleDefinition/RuleSetDefinition、
Operator Registry、表达式类型推导/单位兼容检查、求值/门控/执行器；这些继续属于C/后续WP。
结构/深度/数量/循环/输入域限制与精确引用/字段关联校验属于B的契约安全，不冒称TypeChecker。

| Task | Dependency / current state | Ownership |
|---|---|---|
| X0 shared Catalogue reference | independent extraction; local review PASS | Sol-C (released) |
| X1 shared condition data + complete 16 Policy catalogue | X0-independent; frozen §11, code + targeted review PASS | Sol-B (released) |
| X2 ProducerCatalogue + declaration/finite producer validation | X0/X1 models + X4 helper; checker API delivered, final tests in progress | Sol-C |
| X3 complete finite Producer/Authority/Registry integration | X2 actual checker API + independent Registry/history; input models reviewed, composition implementing | Sol-D |
| X4 finite Definition/Policy conditional reference integration | X0 + X1 only; no Producer import cycle; code and remediation review PASS | Sol-A (released) |
| E2 full Schema inventory + regression + independent review | inventory source/test delta may proceed on stable roots; generated output and final regression after all code writers release | Sol-B; Primary verifies |

All existing independent work is retained. The complete eight-module goal is unchanged.
Only dependency waiting remains; no new user approval is required for these granted tasks.
Exact data model/validation/test interfaces are frozen in CONTRACT_FIELDS §11–13
and were challenged by Sol before implementation. Primary owns plan and final acceptance.
Full semantic expression correctness, Bundle authenticity/closure and real provenance remain
separately unimplemented; finite registry admission must name these boundaries explicitly.

X-C01 local selector has explicit user approval; specs/01, specs/02 and ADR amendments are bounded to that authorization. Exact X1–X4 contracts and writer assignments are CONTRACT_FIELDS §11–13.


X0 shared ProducerCatalogueContentRef extraction is independent of X1 and is assigned
to C first (fact_selectors.py + runtime/facts.py import/re-export only, wire unchanged).
X1 and X0 then unlock A-owned condition_inputs/reference helper and C-owned Producer
models. C's full finite validation depends on A's helper handoff; no shared writer.
Frozen exact interface is CONTRACT_FIELDS §13.2. Existing actual definition directories
are reused; no Producer→condition helper→Producer cycle is permitted.

Current continuation DAG: X0 + X1 → X4; X0 + X1 + X4 → X2; X2 + R1/A1/A2 → X3;
X1/X2/X3/X4 → E2 final generation → full regression → Primary/Sol acceptance.
No X4 → X2 → X4 cycle; X4 takes existing finite directories and returns temporary
selectors without importing Producer declarations. All current module states are in TASKS.md.

X3 independent regression implementation: Sol-A owns the single new
`test_b_producer_authority_review_regressions.py` plus its exclusive x3_review fixture/log
directories; D owns the checker and other X3 tests. This disjoint test task exercises
Primary findings X-I06/X-I07 without assigning two writers to one file. Final source
freeze includes both writers before E2 and full regression. No additional scope.
