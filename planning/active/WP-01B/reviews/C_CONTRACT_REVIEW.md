# Sol Medium read-only review C

Source: native task `/root/wp01b_c_policies`.
Captured by Primary from the actual first final response; no claim of code acceptance.

`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`

只读状态确认：`main`，HEAD=`51a10416d882fe85e5a1cfc5440eac6498fc48f4`。`git status --short`仅显示已知 `planning/acceptance_evidence/`、`planning/transition_evidence/` 未跟踪目录；未写文件、未测试、未安装、未修改 Git。当前可见代理元数据只说明 Codex/GPT-6 系列身份，没有可信实际部署 model ID/effort 白名单字段，因此两者 `UNVERIFIED`；未读取环境秘密或隐藏配置。

**C-F01：阻塞——“完整16 Policy”和“禁止 RuleAST/TypeChecker”不能同时按现有契约交付。**

证据：

- `specs/01_Runtime_Domain_Model_V1.0.md:1167`：ApplicabilityPolicy 必有 `condition_ast`。
- 同文件 `:1174`：ToolSkipPolicy 必有 `condition_ast(tool_condition)`或显式 literal true。
- 同文件 `:1021`：FactProducerDeclaration.activation_condition 为 always 或 typed Tool condition。
- 同文件 `:1108`：三个 AST input domain 必须区分；Applicability 禁 Facts，Tool 仅允许 step 前可用 validated Facts，全部禁止 N06 Candidate/Decision。
- 同文件 `:1156`：禁止 opaque object/Any/hooks。
- `planning/WP-01_IMPLEMENTATION_PLAN.md:163` 已规定 AST 为低层依赖，不能反向依赖 Rule。
- 同计划 `:224`附近明确16分支清单；`:344`将完整 mapping/DAG/producer closure 留 WP-02。

可审查选项，均需用户决策，不能由代理批准：

| 选项 | B交付边界 | 兼容/验收影响 |
|---|---|---|
| A：保持禁止 AST | B暂不交付 ApplicabilityPolicy、ToolSkipPolicy、含条件 activation 的 ProducerDeclaration；保留14个其余完整 Policy及可独立定义的选择政策 refs | 无需改变已批准规范形状，但必须修改B“完整16”验收表；C再加入完整union，不得把14分支别名冒充最终PolicyArtifact |
| B：重新划分 AST 数据模型 | 经批准将既有有限 AST 节点/必要 selector 数据模型提前到B，TypeChecker/Registry/RuleDefinition/求值器仍留后续包 | 最终wire可与已批准规范一致；属于扩大B范围，必须单独授权；不能新造“ConditionAST”逃避禁止RuleAST |
| C：先实现 literal-only 子集 | 只接受显式 true/always 的局部 fixture，条件政策标记未交付 | 这是规范支持域缩减，不能验收“完整16”；后续扩展仍需明确兼容分析，不应称无变化 |

不建议用 `str`、`object`、`Any`、未解析 ForwardRef、空 marker model、任意json替代 AST，也不建议删除必填字段后宣称完成。QuestionSelectionPolicy.context_rules 引用 ApplicabilityPolicy；其ref形状可先实现，但不能宣称目标政策语义已验收。

**C-F02：阻塞完整 mapping 验收——ToolStep 的通用输入/输出与 grounding input mapping 尚缺冻结wire形状。**

- Domain `:175`列 steps/dependency/input-output mapping/required-optional/失败和skip policy，但未规定 StepDependency、InputBinding、OutputBinding、ConditionalBranch 全字段及判别符。
- Domain `:744`：两种 grounding binding “may reference typed input mappings”，没有定义字段名、来源分支、路径基准、必填条件。
- Domain `:1039`—`:1055`已充分规定**Fact输出子映射**：ToolFactOutputDeclaration、ToolFactOutputMapping、payload-relative value_path、evidence section路径、每个mapped output一个状态。它不等于通用 Tool input mapping。
- 计划 `:181`列出 InputBinding/OutputBinding/ConditionalBranch 名称，但没有补齐wire。

可先明确规划并审查下列事实：Fact输出映射可按已给字段建模；通用 input mapping 和 grounding mapping 如在B范围，必须先给字段表和正负 fixture，再请用户冻结。来源可以提出有判别的 context/object geometry/media/upstream validated Fact/step output 等候选，但不能直接把该候选作为已批准枚举。跨step可达性、cycle、完整type compatibility归 WP-02；不能靠运行 DAG 或导入后续 TypeChecker补齐B。

**C-F03：阻塞“全部Prompt/Schema语义支持”；仅ref形状不阻塞。**

Domain `:756`给 PromptTemplateDefinition/PromptVariable 充分字段，但有两项须在B计划明确：

1. named placeholder 的精确语法、注册名称/重复变量/未声明placeholder/partial参数作用域未冻结。可先接受模型字段与有限fixture，不得声称 renderer 或完整静态模板验证。
2. response/context/tool IO/schema artifact 有kind与路径（`:690`），但当前A只有 `SchemaRef`和受限 `TypeDefinition`，没有通用生成 Schema 的 authoritative支持模型/受限descriptor。B若要校验“路径存在/类型匹配”必须显式给有限、类型化 schema描述环境；不能下载任意JSON Schema或采用`dict[str, Any]`。若B仅存 exact refs，语义检查留后续包并在验收表标 `UNVERIFIED`。

`InvocationContractSnapshot`（`:779`）是runtime模型；B即使不交付它也不能改 N04设计：ModelBinding.output_schema_ref是common batch envelope，Question prompt是单Question component，selected-set schema由后续版本化compiler派生。B不需要实现prompt渲染或动态schema组合。

**非阻塞：Remote Primary、Fallback、ReviewReport形状已经足够明确；信任/质量验证应留后续。**

必须保持：

- Domain `:366`—`:377`：primary_deployment_id恰好解析一个deployment；remote模式该primary必须remote；其他仅fallback。数组顺序不授予切换权限。
- `:379`：LocalPrimaryException有ID/version、capabilities、三值reason、bounded rationale、ReviewReportRef、UTC有效窗；remote模式禁止例外字段。模型定义不代表批准真实例外。
- `:390`：每个transition需exact IO兼容和独立quality review，覆盖source/target/capability/scope。
- `:715`：ReviewReportRef恰为 report_id/version/sha256/review_registry_id，非 URL、非虚构ArtifactKind。
- `:1133`：VersionConstraint为bounded nonempty exact deployment/model versions；DeploymentRequirement.logical_model_id需与binding一致。
- `:407`：local Tool placement与Model Gateway路由分离；仅Tool明确嵌套gateway调用才受其binding provider policy约束。
- FallbackPolicy目前只有整体 `trigger_errors[] + allowed_transitions[] + evaluation_refs[]`，没有逐transition报告关联wire。有限目录验证可用外部typed report metadata证明“每条都被覆盖”；若没有metadata，只能校验ref存在与形状，不得声称完成独立评测/特定触发transition授权。是否增加association字段须用户决策，不能暗改现有wire。

**本分工完整模型/枚举及依赖清单。**

拟用文件边界来自计划 `:117`—`:125`，此处未创建：

- `policies.py`：PolicyArtifact及16分支：ApplicabilityPolicy、QuestionSelectionPolicy、UnknownPolicy、QuestionValidationPolicy、EvidencePolicy、ToolExecutionPolicy、ToolFailurePolicy、ToolSkipPolicy、ResourceRequirements、ProviderPolicy、FallbackPolicy、RulePolicy、ConflictPolicy、CoveragePolicy、ContextProvenancePolicy、ExecutionPolicy。
- 辅助模型：ContextSelectionRule、SharedN02SelectionCallPolicy、AdditionalSelectionCallPolicy及SelectionCallPolicy union；ContextFieldSelector、ContextFieldRequirement；DeploymentRequirement、VersionConstraint、FallbackTransition、LocalPrimaryException；ReviewReportRef、ExternalCapabilityRequirement、VocabularyMemberRef。
- `definitions/tools.py`：ToolDefinition、ToolPlanDefinition、ToolStep、StepDependency、InputBinding、OutputBinding、ConditionalBranch；ToolFactOutputDeclaration、ToolFactOutputMapping（如owner放producer模块应唯一源）；ModelGroundingBinding、ToolGroundingBinding（可放capabilities.py，避免重复）。
- `definitions/models.py`：ModelBinding；只依赖refs、共享aux模型及policy refs，不引入runtime ModelInvocation。
- `definitions/prompts.py`：PromptTemplateDefinition、PromptVariable；如需要schema路径语义环境，单列typed supporting model，不能隐藏在任意map。
- `definitions/resources.py`/共享ref模块：ReviewReportRef、ExternalCapabilityRequirement、VocabularyMemberRef应有唯一owner，禁止policy/tools/resources互相循环导入。
- AST依赖项如选B提前：Literal、FactRefExpr、RecordFieldRefExpr、ContextRefExpr、eq/ne/lt/le/gt/ge/in/all/any/not节点、FactSelector、Question/Tool/SystemProducerSelector、ContextFieldSelector；这本身就是实际AST范围，不能淡化。

需集中定义的闭合枚举：

- PolicyKind精确16值（Domain `:1129`）。
- ModelCapability五值；ToolType与ToolCapabilityTag各五值（Domain `:735`、`:786`附近）；ToolProviderKind=builtin/local_model/remote_service。
- ProviderKind=remote/local；ProviderRoutingMode=remote_primary/local_primary_exception；LocalExceptionReason三值；fallback trigger=transport/timeout/rate_limit/unavailable。
- ResourceClass=cpu/remote/gpu；Tool retry error另含resource_exhausted，不能误复用fallback错误枚举。
- EvidenceType八值；EvidenceAccessScope=project/task。
- ContextSource五值；PromptPurpose四值；PromptVariableSource五值；PromptOnMissing=block/omit。
- QuestionSelectionMode两值、OptionalSource三值、SelectionFallback两值、SelectionInvocationMode两值。
- ToolRequirement=required/optional、OptionalContinuation两值。
- NodeId=N01..N06（ExecutionPolicy固定顺序，不添加Gate Node）。
- 单值行为可以Literal表达，不需为每个常量虚构独立枚举。所有业务budget/retention/clock实值显式提供，不能从fixture默认生产值。

底层复用A的 `ContractModel`、ArtifactId/RecordId/Version/Digest、ContentRef/SchemaRef/IdentitySelector、TypeDefinition/Value/ValueContract。现有公开API：

- `domain/base.py:8` strict/frozen/extra forbid/revalidate；`:24` stable_json明确不是JCS/摘要。
- `domain/references.py:21` ContentRef；`:27` IdentitySelector仅profile/region；`:47` SchemaRef必须带dialect。
- `domain/values.py:311` validate_typed_value，无Registry/I/O；`:27`—`:30`固定基础大小边界；MappingProxyType用于深层不可变。
- `domain/enums.py`已含ArtifactKind、RuntimeRecordKind及六个状态域；新增枚举不能改变已验收wire或复用跨域enum。
- `__init__.py`无全包re-export；应保持低耦合。
- pyproject当前仅Pydantic，dev已有pytest/jsonschema/Ruff/mypy；本分工不需要新增依赖。

**待授权测试计划，均未运行。**

正向：16分支最小显式配置（待F01选项解决）、模型/工具grounding union、remote primary、有效UTC local exception fixture、disabled/explicit fallback、ReportRef、prompt required/block与optional/omit、Tool多输出及exact target key、Resource cpu/remote/gpu条件分支、固定node顺序、Python/JSON双入口、schema root/index与immutable roundtrip。

负向：extra/secret/endpoint/shell/code字段；缺schema_version或策略必填值；预算bool/0/负值/溢出；错误ref kind/policy subtype/digest；remote模式local primary、重复deployment、未找到primary；无/过期/错capability例外；fallback disabled含trigger/transition、无review/undeclared endpoints/IO不兼容；prompt错purpose/非法source/required+omit；输入mapping未知branch/任意JSONPath/未声明路径；Fact映射alias/重复output/缺state/non-known仍有payload值；Tool条件含N06字段、Applicability含Facts；模型output common-envelope与单Question component混淆。

必须分开：shape/schema测试、有限目录语义测试、真实review authority/quality/routing/runtime验证。后者全部后续WP，不能由合成fixture标PASS。

本分工静态结论：`CONDITIONAL`。C-F01为必须在最终B计划Challenge中显式提交的范围决策；C-F02/C-F03是否阻塞取决于B是否承诺完整mapping/template/schema语义。未批准任何架构或方案，工作到此停止。
