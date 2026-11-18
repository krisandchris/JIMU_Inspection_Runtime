# WP-01 Implementation Plan — Domain Model and Contract Foundation

状态：WP-00-R2.1.1 / PROPOSED / PENDING_USER_APPROVAL；本文件不构成实施授权。
修订范围：仅 Applicability 文档；基线 main / `2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`。R2.1 修改前字节归档于 `planning/review_evidence/WP-00-R2.1.1/before/`；R1/R2/R2.1 历史证据禁止修改。
审查日期：2026-11-17（Asia/Shanghai）。工作包：WP-00 Bootstrap Review。  
证据等级：`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。

## 1. 目标、范围与审批边界

本次仅授权 WP-00-R2.1.1 的 R3-01 执行归属、R3-02 层级门控语义闭合及审计证据；只改 specs/01、specs/03、ADR、本计划、TEST_STRATEGY 五个文档。不重新规划系统、不新增架构层或决策编号。禁止代码实现、pyproject、安装依赖、运行测试/服务/模型/GPU/外部 API、改分支、初始化 Git、commit/push；不修改需求平台、AGENTS、原始分发清单或 R1/R2/R2.1 证据；不 git reset/restore/checkout 覆盖文件。

WP-01 的建议目标：建立独立 Runtime 的 Python 包、严格 Pydantic v2 契约、版本与摘要原语、Fact Schema Registry、类型化 Rule AST 及纯类型校验接口、可重复生成的 JSON Schema 和对应测试。WP-01 只建设 Domain Model、内存 Fact Registry、Rule AST 静态类型检查、JSON Schema 生成和 CPU-only 测试。所有建议均为 PROPOSED，用户审批状态保持 PENDING_USER_APPROVAL。

非目标：Bundle 编译/打包/验签/完整引用闭包解析（WP-02）、数据库或 ORM、API/OpenAPI 服务、持久调度和节点执行器（WP-03）、Provider/Tool 调用（WP-04）、规则运行时求值和真实巡检（WP-05）、回放执行（WP-06）、Compose 部署与 UI。已有 JIMU 需求平台及 V4 不进入依赖图。WP-01 可以定义后续工作包使用的数据结构，不能据此实现相应服务。

审批循环：Plan → Challenge → 用户 Approval → Execute → Verify → Review → Decide。先由用户批准第 10/13/14/15 节 D01–D15 当前候选和 WP-01 范围，再明确测试/依赖准备授权，才可实施。本次保留修改前原文；不能把候选契约视为已批准规范。当前结果仅供 GPT-6 独立复审及用户审批。

## 2. 启动包与仓库证据（R2.1.1 当前基线及早期历史）

R2.1.1 开始前核对：main / `2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`，工作区 clean，61 个已跟踪文件。R2.1 after 清单的 7 个修订文件及 43 个保护文件与当前字节一致，before 清单的原字节副本一致；没有 pull/reset/restore/init/分支操作。下文 R2.1/R2/初轮结果均为历史，不是本轮执行的测试。

R2.1 开始前实际核对：main / `40af8a17855b27063da94b3c9891272f3e3fc4e3`，工作区 clean，50 个已跟踪文件。`git status --short --branch` 与 `git rev-parse --show-toplevel HEAD` 均 exit 0。无 pull/reset/restore/init/分支操作；以下 R2 和初轮信息仅为历史。

R2 当时核对：分支 main；HEAD=`46388ce1d8f03e0b122b3038a5930eb0aa3ceb01`，初始工作区 clean，origin 指向指定 GitHub 仓库。`git status --short --branch`、`git rev-parse --show-toplevel HEAD`、`git remote -v` 均 exit 0；36 个已跟踪文件。无需 Git 初始化/fetch/pull/reset，本轮均未执行。以下无 Git/环境探测是初轮历史，不是当前状态；R2 未重跑依赖/驱动探测。

已完整依次阅读：`AGENTS.md`、`README.md`、`specs/01` 至 `04` 四份规格书、`planning/ARCHITECTURE_DECISIONS.md`、`DEVELOPMENT_ROADMAP.md`、`DEPENDENCIES.md`、`TEST_STRATEGY.md`，随后阅读非机动车违停目录全部四个文件，再查询 Git 和目录。另核对 `PACKAGE_MANIFEST.json`。

初始目录共 15 个文件：

```text
AGENTS.md
README.md
PACKAGE_MANIFEST.json
specs/
  01_Runtime_Domain_Model_V1.0.md
  02_Runtime_Bundle_Specification_V1.0.md
  03_Six_Node_Execution_Contract_V1.0.md
  04_Runtime_Technical_Architecture_V1.0.md
planning/
  ARCHITECTURE_DECISIONS.md
  DEVELOPMENT_ROADMAP.md
  DEPENDENCIES.md
  TEST_STRATEGY.md
examples/nonmotor_vehicle_illegal_parking/
  README.md
  bundle-fragment.yaml
  task-submission.json
  decision-result.json
```

初轮检查时，清单列出的 14 个文件均存在，字节数及 SHA-256 全部吻合；清单本身为第 15 个文件。本次原文在修改前归档，原 PACKAGE_MANIFEST 不重写；修订后规格/ADR 的哈希变化由独立 before/after 证据记录，不再宣称当前文件仍符合原分发清单。没有 Python 实现、测试目录、`pyproject.toml` 或锁文件。

| 只读命令 | 退出码 | 实际结果 |
|---|---:|---|
| `git status --short --branch` | 128 | `fatal: not a git repository (or any parent up to mount point /mnt)` |
| `git rev-parse --show-toplevel --abbrev-ref HEAD HEAD` | 128 | 同上；Git 在文件系统边界停止查找 |
| `rg --files --hidden -g '!.git/**'` | 0 | 列出上述 15 个文件 |
| `ls -la` | 0 | 当前目录没有 `.git` |

初轮当时分支、HEAD、dirty/clean 均为不可获取，不能报告为 clean，也不能把父目录的其他工程当作本项目仓库。Git 初始化需要后续明确授权，不能由本轮自动执行。

只读环境探测使用 `python3` 标准库 `sys`、`platform`、`importlib.metadata`：Python 3.12.3，GCC 13.3.0，Linux 6.8.0-149-generic x86_64，glibc 2.39；当前解释器未安装 Pydantic、pytest、jsonschema、Ruff、mypy。该信息不是目标 GPU 主机兼容性结论。未探测驱动/CUDA、未安装依赖、未运行服务。

## 3. 四份规格的契约一致性审查

### 3.1 一致且应保留的架构基础

四份规格共同支持：定义与执行分离，Release/Bundle 固定，六节点确定性控制流，原图坐标与变换留痕，严格数据验证，工具计划预绑定，模型候选仅作建议，PostgreSQL 为持久状态权威。没有发现必须推翻这些边界的直接冲突。技术规格中的 API、ORM、部署建议不反向成为领域依赖。

Domain Model 已具备实现基础：核心概念、主要字段、基数、状态域和不可变性已有说明；尚未达到“可无歧义冻结为模型”的程度。缺少的字段类型、联合分支、摘要身份、操作符语义和策略结构必须先冻结，不能以 `dict[str, Any]` 或任意字符串占位。

### 3.2 C01–C11 逐项处理状态（R1 历史摘要；R2 的细化以 §13 为准）

以下“候选已对齐”只表示文档歧义已处理，**不表示用户已批准、代码已实现或测试通过**。C01–C11 均为 `RESOLVED_IN_CANDIDATE / PENDING_USER_APPROVAL`；后续 WP 的行为验证保持未实施。

| 编号 | 原问题 | R1 候选解决方案及落点 | WP-01 / 后续边界 |
|---|---|---|---|
| C01 | root_digest、manifest/checksums 自引用 | 02 §4 排除自身和 manifest 的 payload 清单；M 排除 root_digest/signatures；JCS + domain/长度封帧的 root preimage | WP-01 定义字段/引用；算法实现、固定测试向量/验签 WP-02 |
| C02 | Profile↔Question/ToolPlan digest 循环 | 01 §7 ContentRef / IdentitySelector；inventory 在外部 pin selector targets；前向 content graph 必须无环 | WP-01 引用类型和有限目录类型校验；完整闭包/摘要 WP-02 |
| C03 | Question 多输出与单 FactResult 不一致 | 01 §3.5、03 §6：一 Question 一个主 Fact，复合数据为受限 Record；取消多输出槽 | WP-01 模型、Registry、Record 字段路径类型检查 |
| C04 | Execution 创建时无法知道 actual deployment | 01 §4.2、03 §2：先 pin configured bindings/constraints；实际 Invocation 事后追加，Context digest N01 后 write-once | WP-01 数据不变量；真实调用/持久绑定后续 WP |
| C05 | blocked/skipped 必须 running 后进入的线性图 | 03 §9 两张合法转移表；未运行 pending/ready 可 blocked/skipped/cancelled，无虚假 Attempt；retry 保持 NodeRun running | WP-01 纯状态合法性；lease/fencing/recovery WP-03 |
| C06 | Release 被误列为定义实体 | 01 §2 分开 Bundle 配置聚合与 runtime Release bridge | WP-01 领域模块职责明确，release 控制器不实现 |
| C07 | Fact 值与 Schema dialect 未冻结 | 01 §4.4、计划 §6：typed scalar/list/closed Record，2020-12 离线生成子集；非 known 不带 value | WP-01 Registry/Schema 基础 |
| C08 | 真值/mandatory/多命中无明确策略 | 03 §8.2–8.3：strong Kleene T/F/U + 独立 required completeness guard；mandatory 全扫；collect_all | WP-01 AST/静态检查和策略数据；求值/全扫执行 WP-05 |
| C09 | 自包含缺 prompt/context/tool IO 等资源 | 02 §2/5：完整资源目录；ContentRef 和 selector 语义目标均闭包；外部能力明示 | WP-01 引用契约；完整 compiler/admission WP-02 |
| C10 | 空 Objects/未命中/失败被误判无违规 | 01 §4.8、03 §8.4、D09：独立 Coverage，保留 grounding/discovery 原因和子集结果，不能空集推出 no_violation | WP-01 coverage 数据与一致性 fixtures；运行聚合 WP-05 |
| C11 | 节点/外部持久记录缺 schema_version | 01 §8、03 §2 所有独立持久根显式 schema_version，嵌套值由根版本覆盖 | WP-01 全根生成 Schema 和版本负例 |

### 3.3 示例审查

示例 README 已声明 illustrative，因此不将示例简写当作正式契约：

- Bundle 片段省略必填名称、策略、摘要、上下文、模型绑定、工具输入输出和证据策略；没有实际 Tool 的 output Schema，无法证明 `wheel_boundary_relation` 有合法 producer。
- `enum[...,unknown]` 与 Fact.status=unknown 双重表示缺失，可能把不确定误当已知。建议业务枚举不含状态含义的 unknown；未来 fixture 用 status=unknown 且不携带 value。真实业务枚举如需同名词必须另定义语义。
- `condition_ast: all/fact/operator` 是简写，不能直接作为正式 discriminated union 的合法输入；后续单独构造规范 fixture，不静默宽松兼容。
- task 示例缺 schema_version、租户/项目幂等范围、完整时钟来源，SHA-256 是占位文本；请求 release_id 的选择权限/解析结果需与 accepted Task 分离。
- result 示例以 candidate_rule_ids 表示候选，而 03 §8.1 描述候选违规类别；建议候选统一 category 标识，规则解析由确定性层承担。规则引用缺版本/digest，结果缺完整 Decision/评估 trace，规则引擎版本是占位值。
- 示例没有真实 N02–N05 输出和 Fact/evidence 实体，不能证明完整调用次数、几何准确性或模型能力。

WP-01 原示例保持不动；它们是“应拒绝为规范输入”的回归来源，新的完整正例放 tests/fixtures。业务阈值、SAM3 能力和真实违停判据均 `UNVERIFIED`。

## 4. 模块结构、依赖与职责

建议采用 src layout，名称待审批：

```text
src/jimu_runtime/
  __init__.py
  domain/
    __init__.py
    base.py                 # 严格/不可变配置，持久根契约基类
    enums.py                # ArtifactKind、PolicyKind、分域状态/producer/node 枚举
    primitives.py           # ID、版本、SHA-256、时间、约束数值
    references.py           # ContentRef / IdentitySelector / SchemaRef，运行记录引用
    values.py               # 有判别字段的值与类型代数
    policies.py             # 01 §14 全部 typed policies；无默认行为或任意字典
    geometry.py             # 原图 XYXY、尺寸、变换 provenance
    definitions/
      ontology.py           # Entity / Region
      capabilities.py       # Profile / Facet / Question / typed grounding bindings
      prompts.py            # PromptTemplate / PromptVariable
      resources.py          # Vocabulary / GeometryResource / StaticAsset descriptors
      tools.py              # Tool / ToolPlan / Step / Mapping
      models.py             # ModelBinding / provider policy
      rules.py              # Rule / RuleSet / applicability
    release.py              # Bundle/Release/Activation 数据，无管理服务
    runtime/
      task.py
      execution.py
      perception.py
      facts.py              # candidate Facts / AuthoritativeResultSelection
      question_selection.py # N02 immutable selection record
      tools.py
      evidence.py
      invocations.py
      decisions.py          # Applicability 六类记录/RuleEvaluation 数据，无 Gate Evaluator
      coverage.py           # 执行覆盖，与 lifecycle/result 独立
      replay.py
    nodes/
      envelopes.py
      context.py
      discovery.py
      grounding.py
      observation.py
      tool_execution.py
      decision.py
    fact_schemas/
      models.py             # FactSchemaDefinition / Schema catalogue
      registry.py           # 纯内存只读 registry 接口及校验
    rule_ast/
      nodes.py              # 有限的带判别符 AST
      operators.py          # typed signature descriptors + registry 接口
      typecheck.py          # 纯类型校验，无规则求值
    validation/
      lifecycle.py          # 纯转移表/转移校验，无调度
      issues.py             # 有路径、错误码的结构化诊断
scripts/generate_schemas.py
schemas/generated/
tests/{unit,contract,negative,fixtures}/
```

依赖方向：primitives/enums → values/references/geometry → definition/runtime records → node envelopes。AST 基础类型不导入 Rule；Rule 导入 AST。Fact Schema 模型只依赖 values/references；typecheck 通过只读解析接口读取 Schema，不导入 Release 管理器或 Bundle compiler。`__init__.py` 不用全包 re-export 制造循环。

未来 application 依赖 domain 和其定义的 ports；infrastructure 实现 ports；API 调 application。Domain 仅依赖标准库和 Pydantic v2，不依赖 FastAPI、SQLAlchemy、Redis、HTTP/Provider SDK、对象存储或 GPU 库。解析与校验不得产生网络、磁盘写、模型调用或动态导入。

依赖方案建议：ADR-018 选 uv，Python 3.12 为 CPU 契约层候选；Pydantic 限定 v2 主版本。pytest、jsonschema、Ruff、一个类型检查器（建议 mypy）为开发依赖，Hypothesis 可选。实际有界范围与精确 patch 版本在批准后结合兼容性记录确定，写 pyproject 和 uv.lock；未验证前不填“已测试”版本，不安装 latest，不安装 GPU/服务依赖。

`DEPENDENCIES.md` 已在 R2 明确两阶段门禁候选：WP-01 仅 CPU Python/Pydantic/pytest/jsonschema/Ruff/mypy 与独立 lock；WP-04 才冻结 GPU/driver/CUDA/PyTorch/vLLM/Transformers/SAM3/Grounding 及共驻矩阵。D01 仍待审批；本轮未安装/验证任何版本。

## 5. 待冻结 Pydantic Schema 清单

以下为 WP-01 模型清单，不表示已有实现。Python models 是唯一源；生成的 JSON Schema 不手改。凡独立外部保存/交换的根记录均显式包含 schema_version，嵌套值由所属根的版本覆盖。基类不代替各根的明确版本字面量。

| 模型组 | 待冻结模型 | 核心不变量/边界 |
|---|---|---|
| 基础 | ContractModel、PersistedContract、ArtifactIdentity、ContentRef、IdentitySelector、SchemaRef、RuntimeRecordRef、MediaRef、EvidenceRef、ClockProvenance、TraceContext、ValidationIssue | exact version/digest；错误路径；ContentRef/IdentitySelector 不混用，不把定义引用与运行记录 ID 混用 |
| 枚举 | ArtifactKind、PolicyKind、TaskStatus、ExecutionStatus、NodeRunStatus、AttemptStatus、FactStatus、DecisionResult、RuleTruth、CoverageStatus、DiscoveryOutcome、GroundingOutcome、ProducerKind、ScopeKind、NodeId、ReplayKind、ApplicabilityLevel/EvaluationStatus/Disposition/ApplicabilityEvaluationOwner、ProviderRoutingMode/RouteKind、SelectionInvocationMode、ToolInvocationOriginKind、ProviderVersionResolution/ImplementationVersionResolution，以及显式 release/tool/validation 状态 | 原规格分域枚举保持独立；新增枚举值需审批，不复用字符串的含义 |
| 结构化值 | BoolValue、IntegerValue、NumberValue、StringValue、EnumValue、ListValue、RecordValue 及对应 TypeDefinition | kind 判别；有限类型与约束；无 Any、NaN/Infinity |
| Ontology | EntityDefinition、RegionDefinition、AssetRef | 不是 Object；Region 不等于 polygon |
| Capability | ProfileDefinition、FacetDefinition、QuestionDefinition、PrimaryFactOutput、ModelGroundingBinding、ToolGroundingBinding、ContextFieldSelector、QuestionSelectionPolicy | Release 固定 catalogue/policy；N02 记录确定集合；Facet 精确交集和 Profile context/scope 校验 |
| Tool | ToolDefinition、ToolPlanDefinition、ToolStep、StepDependency、InputBinding、OutputBinding、ConditionalBranch、ResourceRequirements、ToolExecutionPolicy | 已注册 tool；有限 DAG 数据；条件只读合法上下文/Fact；无 N06 依赖 |
| Model | ModelBinding、ProviderPolicy、DeploymentRequirement、VersionConstraint、FallbackPolicy、LocalPrimaryException | 配置身份与实际身份区分；不含 secret/请求任意配置 |
| Rule | RuleDefinition、RuleSetDefinition、RulePolicy、ConflictPolicy、FactSelector | 完整类型 AST；必需/可选适用性明确；无隐式顺序优先级 |
| Bundle/Release | BundleManifest、ArtifactInventoryEntry、SourceProvenance、CompatibilityRequirement、IntegrityEnvelope、ChecksumEntry、AdmissionReceipt、CapabilityReport、ReleaseRecord、ApprovalReceipt、ActivationRecord、ActivationScope | 仅数据契约；根摘要采用 D07 候选字段；技术准入、业务批准、激活分别记录；无编译/验签/激活实现 |
| Intake | TaskSubmission、AcceptedTask、MediaAsset、TaskContext、ContextSnapshot、ContextValueProvenance、ExecutionCoverage、ProfileCoverage、MandatoryRuleCoverage、MandatoryRuleObligation | 一个 RGB 图片；可信元数据来源；UTC/时钟；接收请求与已解析 Release 分离 |
| Execution | ExecutionRecord、NodeRun、Attempt、ExecutionEvent、ExecutionPins、ConfiguredModelPin、ActualDeploymentIdentity | immutable 输入/config 引用；Attempt 单调编号；不存在的 actual identity 不预填；无持久引擎 |
| Perception | ObjectRecord、BBoxXYXY、ImageDimensions、CoordinateSystem、TransformRecord、GroundingRecord、ProfileGroundingResult | 原图坐标、正面积/范围；no_target/failed/partial 分开；Object 仅 execution 内身份 |
| Fact | FactRecord、KnownFact、UnknownFact、ErrorFact、NotApplicableFact、NotEvaluatedFact、FactProducer、FactScope、FactResult、AuthoritativeResultSelection、ToolFactOutputDeclaration、ToolFactOutputMapping、FactSelector | status 判别；scope 判别；类型/来源/证据；invalid value 不伪装成 known |
| Tool runtime | ToolExecution、GroundingBindingOrigin、ToolPlanStepOrigin、ToolResult、ToolOutputState、ToolAttemptResult、ToolError、ToolResourceUsage | 配置 ref 与实际实现版本；输入输出 digest；failure 非负证据 |
| Evidence | EvidenceAsset、EvidenceScope、StorageReference、RetentionPolicyRef、AssetStage | 元数据而非大字节；原图/变换关系；只定义 staged/finalized 状态契约，无上传流程 |
| Model audit | ModelInvocation、InvocationIdentity、ConfiguredProviderIdentity、ActualProviderIdentity、ProviderRouteTrace、InvocationOutcome、InvocationError、UsageRecord | configured binding + 实际 provider/deployment/model；provider request ID；不在记录中写密钥 |
| Applicability runtime | ProfileEligibilityEvaluation、RuleSetApplicabilityEvaluation、RuleApplicabilityEvaluation、ApplicabilityTrace、ApplicabilityLevelTrace、UnresolvedObligation | Policy 定义、执行记录、解释 trace、Coverage 聚合四者分开；Pre-N02/N06 归属、父级阻断和无 truth 的 not_evaluated；见 01 §3.8.1 |
| Decision | CandidatePrediction、CandidateCategory、DecisionRecord、RuleEvaluation、RuleEvaluationOutcome、ConflictResolutionTrace、ExplanationTrace | 全量 applicable/evaluated/skipped/blocked/not_evaluated/matched 引用；独立 complete/blocked Required Evidence Guard 检查 Facts/Evidence/Producer Authority；不含 evaluator |
| Replay | ArtifactReplayRequest、InferenceReplayRequest、ReplayRun | kind 判别；source/target pin；只建数据，无模型/工具执行 |
| Node common | NodeInputEnvelope、NodeOutputEnvelope、NodeError、ArtifactOutputRef | task/execution/release/node/attempt/trace pin；错误、时间、输出有界 |
| N01 | ContextBuildInput、ContextBuildOutput、ContextValidationReport | 不补造缺失上下文；required failure 明确 |
| N02 | DiscoveryInput、DiscoveryOutput、ProfileHit、DiscoveryProfileDiagnostic、QuestionSelectionRecord、AllDeclaredSelectionRecord、ContextRuleSelectionRecord、ModelAdvisorySelectionRecord、FallbackSelectionRecord、FallbackSelectionEvidence、SelectionCallPolicy、SharedN02SelectionCallPolicy、AdditionalSelectionCallPolicy | 只接收 eligible_profiles/evaluation ref；DiscoveryProfileDiagnostic 替换含混 FilteredProfile，不生产 Profile eligibility 排除；Questions/selection 不变 |
| N03 | GroundingInput、GroundingOutput、ModelGroundingProvenance、ToolGroundingProvenance | 独立 grounding；原样继承 selection；模型/工具定位来源判别 |
| N04 | ObservationInput、ObservationBatch、QuestionResult、QuestionValidationResult | 一 Object/整批 Attempt；一 Question 一个主 Fact（可为 Record）；显式 parse/关联可信度；批次诊断与合法 Fact 分开 |
| N05 | ToolNodeInput、ToolNodeOutput、ToolPlanRunSummary | 仅绑定计划，引用 N04 validated Facts；无候选驱动选择字段 |
| N06 | DecisionInput、DecisionOutput、MandatoryRuleCoverage | 候选与规则评估分离；完整性报告，缺失候选不改变 mandatory 集合 |
| Registry/AST | FactSchemaDefinition、ValueSchemaGenerationSpec、GlobalTypeLimits、TypeDefinitionProjection、ValueSchemaGenerationInput、FactSchemaCatalogue、ProducerCatalogue、FactProducerDeclaration、QuestionProducerSelector、ToolProducerSelector、SystemProducerSelector、OperatorDescriptor、TypeCheckResult 及第 7 节 AST 节点 | 纯合同和校验；完整 producer closure/DAG admission 属 WP-02 |

R2 新增清单（详见 01 §9–14，每个持久根均需生成 Schema 和正负 fixture）：

| 模型组 | 新增/明确模型 | 契约依据 |
|---|---|---|
| Artifact/静态资源 | ArtifactKind、PromptTemplateDefinition、PromptVariable、VocabularyDefinition、GeometryResourceDefinition、StaticAssetDescriptor、ReviewReportRef、ExternalCapabilityRequirement、VersionConstraint、DeploymentRequirement | 01 §9/10；不引入独立 Discovery/Grounding Artifact 或 Registry 服务 |
| Question selection | QuestionSelectionPolicy、QuestionSelectionRecord、ContextSelectionRule、ExcludedQuestionReason | 01 §11/14；mandatory/optional 分区、回退、配置/Invocation pin |
| Policy Catalog | PolicyArtifact/PolicyKind、ApplicabilityPolicy、UnknownPolicy、QuestionValidationPolicy、EvidencePolicy、ToolExecutionPolicy、ToolFailurePolicy、ToolSkipPolicy、ResourceRequirements、ProviderPolicy、FallbackPolicy、RulePolicy、ConflictPolicy、CoveragePolicy、ContextProvenancePolicy、ExecutionPolicy | 01 §14 加上 QuestionSelectionPolicy，共 16 个封闭 policy 形状；无 Any/object 占位 |
| Producer/authority | ProducerCatalogue、FactProducerDeclaration、QuestionProducer、ToolStepProducer、SystemProducer、ToolFactOutputDeclaration/Mapping、ProducerSelector union、AuthoritativeResultSelection、ActorRef | 01 §13；Question 1:1，Tool step 1:N，scope/key 冲突、Attempt 候选和显式 authority |
| Schema/路径 | FactSchemaDefinition.semantic_key/type_definition/payload_schema_ref、RecordFieldRefExpr、ContextFieldSelector、InvocationContractSnapshot | fact_schema 定义与 value_schema 生成产物分离；同一 field_path 约定 |

R2.1 字段覆盖补充（与以上完整清单合并，不另建新运行服务）：

| F 项 | 明确的 Schema / 字段 | 唯一字段来源 |
|---|---|---|
| F01 | ApplicabilityTrace/ApplicabilityLevelTrace：三级 target/policy/input refs、evaluation_status/truth、decisive_level/disposition、ancestor reason；Coverage/Decision 的 applicability_trace_refs | Domain §3.8.1 |
| F02 | ProviderPolicy.routing_mode/primary_deployment_id/selection、LocalPrimaryException、ConfiguredProviderIdentity/ActualProviderIdentity、ProviderRouteTrace；DeploymentRequirement.provider_id | Domain §3.9.1/§14 |
| F03 | QuestionSelectionRecord 的 method 联合分支；discovery_model_invocation_ref、条件必填 model_invocation_ref/selection_invocation_mode、FallbackSelectionEvidence；ModelInvocation.purposes/source_discovery_invocation_ref；SelectionCallPolicy shared/explicit_additional（含 binding/resource/budget refs） | Domain §11.1 |
| F04 | FactSchemaDefinition.value_schema_generation；ValueSchemaGenerationSpec/GlobalTypeLimits、TypeDefinitionProjection、ValueSchemaGenerationInput；payload_schema_ref 仍指独立 value_schema | Domain §12.1 |
| F05 | ToolExecution 公共 refs/actual-version resolution；invocation_origin=GroundingBindingOrigin/ToolPlanStepOrigin；N03 不能成为 N05 producer | Domain §4.5 |

不新增 ArtifactKind/PolicyKind：仍是 25 种制品、16 个 Policy 子类型。新增 trace/union/
生成输入是既有领域契约的精化；外部持久根显式 schema_version，嵌套分支由根版本治理。
本节是 WP-01 完整 Schema 清单：基础表、R2 资源/Policy/producer 表和 R2.1 字段表共同
构成生成器 root index 的清单；R2.1.1 的六类 Applicability 记录已合并到基础表。
字段以 01 §3.8.1/4.7/4.8、03 §3.1/4/8 为准：Gate aggregate 的三分区及 trace refs；
RuleSet/Rule 记录的 parent refs/真实 N06 refs 或 ancestor projection；Trace 的 owner/
source/decisive ancestor；UnresolvedObligation 与原 ledger 共用 obligation_id；Coverage
和 Decision 引用选定 revision。ApplicabilityLevelTrace 是嵌套 trace，独立存储才有根版本；
UnresolvedObligation 是现有 obligation 的类型分支，不建立重复账本。不增加 ArtifactKind/
PolicyKind/NodeId；枚举集中定义 evaluation owner、reason code 和 N02 skip reason。

冻结时应为每个模型列必填/可选、默认值、长度/数量边界、错误码、样例及 JSON Schema 根名称。当前规格未给出的实体类型、保留策略等业务枚举不虚构为 CONFIRMED；以受约束标识和被引用的版本化词表治理，或在审批中列出有限初始值。

### 类型与版本策略

1. `extra="forbid"` 覆盖所有外部对象，严格标量校验，不能把 `"false"` 转 bool 或把 bool 当 integer。明确 JSON 文本解析与 Python 输入的允许范围，写双入口测试。
2. schema_version、artifact version、Bundle version、runtime/workflow/operator implementation version 分开。建议 SemVer 精确引用，V1 候选使用稳定的 major.minor.patch（非负整数、无前导零），禁止 `latest`、范围和隐式默认版本；兼容范围只出现在明确的 compatibility 对象。
3. SHA-256 建议小写 64 位十六进制，摘要算法单独标识；错误长度、占位符或 mismatch 拒绝。JSON Schema `$id` 与引用身份稳定且版本化，离线解析。
4. 根 schema major 变化或值域/required 收紧视作不兼容；新增字段在 extra=forbid 的旧消费者中也可能不兼容，不能自动称为 minor 安全。未知 required feature/version fail closed。迁移说明必须包含旧记录读取/转换策略，不覆写历史证据。
5. 内部 UTC aware 时间；接受边界保留 capture 原时间、offset/timezone 与时钟来源；拒绝 naive 时间。置信度只作来源元数据，取值范围需能力声明，不宣称校准概率。
6. 发布定义与 Snapshot 使用不可变模型及不可变嵌套集合；Pydantic frozen 不能单独证明深层不可变。运行状态更新生成新值，历史版本不就地改写。
7. scope 为 discriminated union：object 必有 object_id，execution 不得夹带 object_id；Rule 的 FactSelector 同时限定 fact_key、fact_schema_ref、scope/producer_selector/field_path，避免同名 Facts 歧义。
8. V1 不启用通用 extensions。未来需要时单独冻结命名空间、有限值类型、大小上限、兼容策略；不能用开放 JSON 绕过额外字段校验。

## 6. Fact Schema Registry 设计要求

### 6.1 身份与注册内容

建议 immutable key 为 `(schema_id, version, digest)`；相同 ID/version 不同 digest 必须拒绝，不能覆盖。`fact_key` 是用途标识，不替代 Schema 身份。Registry 按 Release 构造不可变快照，不采用进程全局可变单例。

FactSchemaDefinition 应显式规定：schema_version、schema_id/version、semantic_key、type_definition、payload_schema_ref（value_schema）、scope、known/non-known 策略、单位/坐标系（适用时）、allowed_producer_kinds、证据与置信度元数据策略、有限集合/字段约束。Facet.fact_schema_refs 是该精确 Schema 的语义允许集合；Question 主 Schema 必须属于全部关联 Facet 的交集。F04 的 projection/generation spec 先生成独立 value_schema，child digest 获得后才构造 parent ref/digest（Domain §12.1）。父身份、摘要、Bundle 摘要不进入生成输入或 child Schema；枚举身份由 Registry/Fact envelope 校验，不在 child 中固化父 digest。Schema digest 不能把自己纳入被摘要内容；生成文件的字节摘要与逻辑 Bundle 摘要是不同概念。

建议仅支持 JSON Schema 2020-12 的受限生成子集，由 Pydantic/受限 TypeDefinition 生成；禁止导入任意外部 Schema、remote `$ref`、可执行 validator 或插件代码。输出 `$schema`/`$id`、离线 `$defs`，记录生成器版本；不承诺 Pydantic 自定义跨字段校验能全自动变成 JSON Schema，必须用分支结构表达并测试，无法表达的语义由显式二阶段校验承担。

### 6.2 值与状态

- 建议类型代数：bool、integer、finite number（默认 IEEE-754，有单位可选）、bounded string、closed string enum、bounded homogeneous list、closed record；嵌套深度/数量有限，不支持任意递归、union 自由拼装或 Any。
- 显式 `kind` 判别；枚举的 schema 身份由 Fact envelope/Registry 保留；number/integer 不静默互转，不能把 bool 作为 number。几何通过特定 record Schema 表达并附 coordinate system，不把未校验字典作为 payload。
- `known` 必须有合法 value；known false、0、空字符串等按 Schema 合法性保留，不以 truthiness 判断缺失。
- 所有非 known 分支均不允许 value 字段（含 null），并要求结构化 reason；error 附错误分类/validation issue，not_applicable 必有适用性依据，not_evaluated 记录跳过原因。01 §4.4 已同步为此候选，审批仍待用户确认。
- 传输失败/整批 parse 失败只产生 Attempt/batch 错误，不凭空制造每项 Fact。单项非法在关联可信时可产生 status=error 的结果；原始非法响应作为受控诊断证据，不能写入 typed value。
- 多次 Attempt 的 Fact 全部保留，用显式选择的 authoritative attempt/result 引用消费；重复 producer、同 Question 主 Fact 多条无权威选择不能“最后一条胜出”。

### 6.3 接口与验证层次

01 §13 是 Producer/authority 的唯一字段源：Question→primary 一条、ToolStep→多个命名输出，V1 N05 outputs 仅 object scope、execution Facts 为 system producer；value_path 静态解析 Tool IO；物理 Fact 候选不覆写，whole-batch/step authority 显式选择并 pin revision。同一 object-scope Profile/fact_key 或 execution-scope fact_key 的多 producer 冲突拒绝。Rule FactSelector 用同一 producer/schema/key/field_path，不能引用 derived catalogue 产生摘要环。

接口建议为 `register(definition)`（仅构建阶段）、`freeze()`、`resolve(exact_ref)`、`validate_payload(ref, typed_value)`、`validate_fact(fact)`、`describe_type(ref)`、`compare_compatibility(old_ref, new_ref)`。返回有路径/错误码的诊断，不能直接泄漏 Provider 异常形状。

WP-01 校验 Schema 自身、重复身份、值/状态、scope、引用匹配、producer kind 与类型描述；用注入的有限目录做 AST Fact 解析测试。WP-02 才检验整个 Bundle producer/consumer closure、必需工具可达性、DAG 及真实 registry capability。兼容性比较保守 fail closed：无法证明兼容即不允许替换；运行时必须继续解析旧 ref，不能自动换到新版。

## 7. Rule AST 类型系统和校验

### 7.1 AST 结构与允许操作符（建议首版）

AST 是有限数据，按 `kind`/`operator` 判别，不允许 Python 表达式、eval/exec、动态导入、正则代码或任意函数名。

| 节点/操作符 | 输入类型 | 输出/约束 |
|---|---|---|
| Literal | typed scalar/list 常量 | 自带明确类型；闭合 enum 必须是合法成员 |
| FactRefExpr | exact FactSelector | Registry 解析 value 类型；不存在是静态错误 |
| RecordFieldRefExpr | FactSelector + 声明的 field_path 元组 | 逐层验证 closed Record 字段与类型；禁止任意 JSONPath；可选字段缺失在未来求值中为 U |
| ContextRefExpr | 已声明 allowlist selector + SchemaRef | 仅确定性适用性/Tool 条件所需上下文；任意路径拒绝 |
| eq / ne | 同类型可比较值 | Boolean；enum schema identity 必须一致，单位必须兼容 |
| lt / le / gt / ge | 同类型 numeric、同单位 | Boolean；不允许字符串/enum 排序或隐式单位换算 |
| in | scalar T + 非空 bounded list[T] | Boolean；不允许字符串子串猜测语义 |
| all / any | 非空 bounded boolean expressions | Boolean；空集合拒绝以避免 vacuous truth |
| not | 一个 boolean expression | Boolean；非 known 不转成 false 再取反 |

几何、日期算术、聚合量词、自由脚本、动态 operator 插件不在首版。OperatorDescriptor 包含固定 operator ID/version、arity、输入约束、输出类型、非 known 传播策略；registry 由代码白名单构造，Bundle 只声明所需 operator capability，不能提供实现。

### 7.2 静态校验与求值语义边界

WP-01 实现 AST 结构及纯类型检查；Rule 执行引擎留 WP-05。结构校验不等于可执行性证明，类型检查需显式读取 Registry/Context 类型环境：

1. 校验 operator、arity、树深/节点数/集合大小预算（具体值审批后冻结并测试边界），拒绝额外字段与循环输入对象。
2. 解析每个 Fact/context 引用到 exact Schema，检查 scope、producer/主 Fact是否唯一；验证 AST 所引用 Fact 与 required/optional dependency declarations 一致。
3. 递归推导类型，检查常量、enum membership、单位与比较操作，拒绝 incompatible types。可解析缺 producer 的本地类型测试不能宣称完整 Bundle closure 通过。
4. 静态检查必须显式传入 input_domain：rule_applicability 只允许已声明 Context/Region/Profile 元数据（禁止 Object Facts）；rule_condition 可读验证 Fact/Context；tool_condition 可读先前已验证 Fact/Context。所有根 Boolean lifted T/F/U，所有域禁止 Candidate/Decision，不用一个无边界 AST resolver 代替。
5. 策略必须覆盖 missing（无记录）、unknown、error、not_applicable、not_evaluated；missing 不是额外 FactStatus，不用 false 补齐。
6. Predicate truth 使用 Strong Kleene T/F/U，仅在单个 Policy AST / Rule Condition AST 内；全表见 03 §8.3：not(U)=U，F AND U=F，T AND U=U，T OR U=T，F OR U=U。独立 Required Evidence Guard 检查 required Facts、Evidence 和已选 Producer/Attempt Authority，输出 complete/blocked 及原因，不输出 TruthValue。必需项缺失或 authority 歧义时，即使 predicate 可简化为 F/T，Rule outcome 仍 blocked/effective uncertain。WP-01 只定义类型/策略与静态检查，不实现求值器或 guard 执行器。
7. 是否允许显式状态检查操作符应单独审批；首版建议不提供，以避免条件绕过 required Fact 政策。
8. RuleEvaluation 分开保存 execution outcome（evaluated/skipped/blocked）、predicate truth（true/false/unknown）与 matched 标记/引用；matched 不是替代 succeeded 的生命周期状态。
9. R3-01/R3-02 按 Profile→RuleSet→Rule 有序 gate 留 trace，不把三级压成 AND AST。N01 只构建 Context；Workflow Engine 在成功 N01 后执行 Pre-N02 Profile Gate，N02 只收 eligible_profiles；N06 引用固定 Profile 结果并负责 RuleSet/Rule 两级。每层只读声明元数据，FALSE 排除子树，UNKNOWN 产生 unresolved obligation、后代 not_evaluated 且无 truth；只在父 TRUE 时到达子级。全 mandatory 范围用实际评估或祖先投影记录，不要求执行被阻断的子 policy；保存 excluded/unresolved/blocked/not_evaluated 原因，不改变 N05 ToolPlan。
10. RuleSet conflict_policy 候选为 collect_all：保留全部证据有效的匹配 Rules 与 category 集合，绝不提前停止；V1 不支持排他/优先胜出模式。单 Object 有有效匹配即 violation，同时保留其他 blocked/unknown 与 partial coverage；无匹配且有未决 mandatory 工作为 uncertain；所有适用 mandatory 完整且 false 才 no_violation；确定无适用 mandatory 为 not_applicable。Execution 空集与部分覆盖遵守 D09，不能借 Object 级结果掩盖未执行范围。

## 8. 测试计划、边界与执行授权

以下全部是待批准的 WP-01 测试；本轮没有执行 pytest 或 Schema 功能测试。测试使用确定性 CPU fixtures，无网络、真实图像上传、模型、工具执行或外部数据库。本次计划修订明确禁止执行这些测试；未来仍需单独实施/测试授权。

| 测试组/建议文件 | 正向与边界 | 负向/回归 |
|---|---|---|
| unit/test_primitives.py | exact refs、UTC、digest 格式、版本 | 占位 digest、latest/range、naive 时间、ID 混用 |
| unit/test_immutability.py | 定义/Snapshot 嵌套不可变 | 修改列表/映射内容、就地改 ref 被拒绝 |
| unit/test_lifecycle.py | 审批过的 Task/Execution/Node/Attempt 边 | 跨枚举赋值、终态回 running、未运行节点 blocked 的 C05 回归；不测试 DB fencing |
| unit/test_fact_registry.py | 注册/freeze/精确解析、旧版本共存、known false/0 | 相同 ID/version 异 digest、缺 ref、freeze 后注册、同名键多 producer 无选择 |
| negative/test_fact_values.py | 每个状态合法 fixture、scope 判别 | known 缺值、非 known 携值/null、错误类型、bool-as-int、额外字段、非法 enum、NaN/Infinity、超长/超深 |
| unit/test_rule_typecheck.py | 每操作符所有允许签名、嵌套逻辑 | 未注册 op、错误 arity、enum/单位/schema mismatch、非 bool 根、未知 Fact、越界预算、非法上下文/N06 selector |
| contract/test_definitions.py | 每个定义模型完整最小正例 round-trip | ToolPlan 任意 shell/code 字段、ModelBinding 无政策 fallback、缺必需字段 |
| contract/test_runtime.py | Task/Execution/Attempt/Object/Fact/Evidence/Decision 等所有根 | 非法 pin、scope 不匹配、bbox 越界/零面积、错误坐标来源、策略遗漏 |
| contract/test_nodes.py | 每个节点输入输出 fixture | N02 BBox/Object/category；N03 修改 Questions；N04 重复主 Fact/未声明 Question/多输出槽；N05 候选依赖字段；N06 trace 缺必需项 |
| contract/test_coverage.py | JEV no_hits、空 Objects、grounding no_target/failed/partial、子集违规保留 | none/partial 伪装 complete/no_violation、缺 per-Profile 原因、coverage 与 lifecycle 混用 |
| contract/test_object_identity.py | 同目标跨 Profile 两个独立 Object/Fact refs | 隐式 merge、跨 Profile Fact 引用、宣称 unique physical count |
| contract/test_logic_policy.py | T/F/U 类型与 collect_all 策略 fixture、完整 mandatory trace | Boolean 强转 U、first_match、candidate-only 筛选字段/缺失必需诊断；不求值 AST |
| contract/test_reference_shapes.py | ContentRef 与 IdentitySelector、有限 inventory 注入 | selector 用作执行 content ref、回向含 digest、缺/歧义/wrong-kind 目标；不计算 Bundle digest |
| contract/test_artifact_bindings.py | 全部 25 ArtifactKind 的模型/路径/预期 ref kind，direct ModelBinding/Tool binding、Prompt variables、grounding union | 未定义 kind、假 Capability ref、purpose/schema mismatch、工具定位强塞 ModelInvocation |
| contract/test_question_selection.py | A/B、mandatory union、optional partition/fallback/ordered set、selection pin | 未注册 Question、mandatory 漏选、N03 改集合、选择影响 required Tool/Rule、retry 换集合 |
| contract/test_facet_schema.py | 多 Facet 精确交集、跨 Facet/Question 复用 | 仅形状相似不同 digest、semantic_key/unit/scope 不符、Profile context 不兼容 |
| contract/test_policy_catalog.py | 16 个 policy subtype 的显式字段、各合法分支、默认禁止 | 缺字段、Any/额外配置、未定义 business 默认、非法 applicability Facts/Tool Candidate |
| contract/test_producer_authority.py | Question 1:1、Tool step 1:N、路径/producer/schema、batch/step authority | key 冲突、重复 Attempt 异值、last-write-wins、跨 Attempt cherry-pick、Rule selector 无唯一 producer |
| contract/test_final_closure.py | F01–F05 正例/条件分支/显式 refs、投影生成重复性 | TEST_STRATEGY §2.3 的五组负例；有限合成 refs 不能证明真实调用/审批/路由 |
| contract/test_applicability_contract.py | 六类记录、完整层级 trace/分区、引用固定 ancestor、空 eligible skip 与真实 no_hits 分开 | TEST_STRATEGY §2.4 的正负例；拒绝父 U 被子 F 覆盖、not_evaluated 携 truth、越权输入/双重排除、Rule gate 改 N05；只校验预制记录和静态 AST 域 |
| contract/test_json_schema.py | 每根 generated Schema 有效；Pydantic 和 jsonschema 双验样例 | 同组错误 fixture 均拒绝；无法 JSON 表达的语义单独标记，不谎称等价 |
| contract/test_schema_generation.py | 两次生成字节完全一致、root index/version 清晰 | schema 漂移/缺文件失败；非手工更新生成产物 |
| contract/test_import_boundaries.py | domain 只用允许依赖 | 静态 AST/import 检查禁止 infrastructure/SDK/GPU 导入；解析不触发 I/O |
| negative/test_illustrative_examples.py | fixture 标识保持 illustrative | 原 task SHA 占位、缺版本；原 AST 简写/enum unknown 不被静默正规化 |

Unit + contract 的“集成”仅为内存 Registry → Fact validator → AST checker，需实际运行后报告；PostgreSQL/MinIO/Redis/Provider/GPU integration 均不适用 WP-01，不以 mock 冒充。

明确归属后续 WP 的测试：ToolPlan cycle/完整 mapping、缺 producer、Bundle path traversal/签名/root digest/activation（WP-02）；幂等接受、stale worker、cancel/recovery（WP-03）；真实超时/fallback/资源预算（WP-04）；N04 实际一 Object 一请求、部分非法保留、N05 独立运行、N06 全扫及 Rule 真值求值（WP-05）；零调用 artifact replay（WP-06）。WP-01 只能通过形状约束测试这些边界，不能宣称已经验证运行行为。

审批建议包含以下精确命令及 CPU-only 依赖准备；未获安装授权前不执行 uv sync：

```bash
uv sync --locked --group dev
uv run --locked python --version
uv run --locked python -m pytest tests/unit tests/contract tests/negative -q
uv run --locked python scripts/generate_schemas.py --check
uv run --locked ruff check src tests scripts
uv run --locked mypy src/jimu_runtime
```

生成命令的 `--check` 是计划中的接口：在临时目录重复生成并与受控输出逐字节比较，不静默改 checked-in Schema；首次生成另用显式 `--write`。包管理器若未选 uv，上述命令须在审批后整体替换，不能将未安装的工具当作可运行证据。

每次记录开始/结束时间、工作目录、解释器/依赖版本、精确命令、退出码、测试计数、skip 原因、日志位置和代码快照标识。无 Git 时以文件清单 SHA-256 记录快照并明确 HEAD unavailable；不宣称是 Git commit evidence。

## 9. WP-01 交付文件、验收门槛与回滚

### 9.1 文件变更清单

| 阶段 | 文件 | 动作/边界 |
|---|---|---|
| 初轮 WP-00（历史） | planning/WP-01_IMPLEMENTATION_PLAN.md | 当时仅新增计划；初始证据保留于 §11 和 R1 before 副本 |
| 本轮 WP-00-R1 | 本计划、specs/01–04、planning/ARCHITECTURE_DECISIONS.md | 仅六个现有文档修订；新增 review_evidence 文档审计包，无代码 |
| WP-01 待批准 | pyproject.toml、uv.lock（若选 uv）、.gitignore | 有界依赖、可重建 CPU 环境；无模型权重/秘密 |
| WP-01 待批准 | src/jimu_runtime/** | 第 4/5 节的领域模型、纯 Registry 和类型检查器；不得加入 API/ORM/Provider/调度实现 |
| WP-01 待批准 | scripts/generate_schemas.py、schemas/generated/*.schema.json、schemas/generated/index.json | 从 models 生成；每个可持久根可定位；生成器具备 --write/--check |
| WP-01 待批准 | tests/unit/**、tests/contract/**、tests/negative/**、tests/fixtures/** | 第 8 节确定性正例/负例，不改现有 illustrative 示例 |
| WP-01 待批准 | planning/CONTRACT_COMPATIBILITY.md、planning/DEPENDENCY_MATRIX.md、planning/WP-01_VERIFICATION_REPORT.md | 兼容/迁移说明、CPU 验证矩阵、真实命令证据和 gate 结论 |
| WP-01 待批准 | planning/ARCHITECTURE_DECISIONS.md、specs/01–03 对应段落、README.md | 只落实用户批准的决议及契约索引/运行说明，先列差异再修改；不自动批准技术架构变更 |

尚无数据库，WP-01 不生成/执行 migration；首版兼容说明仍需说明：原示例不是持久生产记录、哪些旧形状拒绝、未来 schema major 如何保留历史读能力。锁文件形成是交付，不代表获准 git commit。

### 9.2 可验证验收标准

| Gate | 验收证据 | 通过条件 |
|---|---|---|
| G0 授权 | 用户批准范围、D01–D15 当前候选的冻结/缩减方案及测试授权 | 阻塞项未解决时不得标记 WP-01 PASS |
| G1 清单完整 | 第 5 节每个根模型到文件/生成 Schema/fixture 的映射 | 无 Any 占位、无遗留未定义策略类型；必要缩减先获批准 |
| G2 严格验证 | unit/contract/negative 的实际 pytest 报告 | 所有必需正例接受，负例拒绝；0 failed，必需测试不得 skip |
| G3 Fact/AST | Registry + AST 类型检查报告 | 状态/value/scope/ref 不变量、全部注册签名、错误类型/未知 op/深度边界均覆盖 |
| G4 可重复 Schema | --check 输出和两次字节比较 | 无差异；Schema 自检和双验证通过；语义层限制有独立测试 |
| G5 依赖边界 | import 检查、Ruff、类型检查、依赖/锁文件 | domain 无禁用依赖，无网络/模型/GPU 副作用；CPU 环境可复建 |
| G6 兼容与安全 | compatibility notes + approved 决策记录 | 摘要身份无循环、版本规则明确、secret/任意代码/无限配置不进入契约；无数据迁移 |
| G10 R2 契约闭合 | §13 的 finite-definition 契约检查报告 | 25 ArtifactKind、16 typed policies、能力/Prompt refs、Question/Facet/producer/authority/input_domain 规则一致；未实现完整 compiler/执行器 |
| G11 R2.1 最后闭合 | 五项类型/引用负例及生成重复性报告 | 三级 gate/Remote Primary/选择证据/独立 value_schema/Tool origin 均符合 §14；只做 Domain/Registry/静态类型/Schema，运行执行仍后续 WP |
| G12 R2.1.1 Applicability | §15 / TEST_STRATEGY §2.4 的模型、静态引用/类型与生成 Schema 证据 | 六类记录齐全，Pre-N02/N06 权限正确，三级有序且父 U 不被子 F 覆盖；三类语义分离；Coverage/Decision 保留祖先与未执行义务；无 Gate/Workflow evaluator |
| G7 报告与停止 | WP-01 verification report | code 与 runtime 证据分列；限制明确；完成后停在 gate，不开始 WP-02 |
| G8 Coverage/Identity | CPU-only 契约 fixtures | JEV no_hits、空 Objects、grounding no_target/failed/partial 均不能伪造 complete/no_violation；子集有效命中保留；同目标跨 Profile 独立 Objects/Facts |
| G9 Rule/生命周期策略 | 策略模型、AST 静态检查及纯转移验证 | T/F/U 与 required guard 分离；仅 collect_all/full mandatory scan 策略；非运行 blocked/skipped 无假 Attempt；所有终态无出边；不实现 Rule 求值/worker |

WP-01 PASS 只表示领域/契约基础验收，不表示 Bundle 验签、工作流、真实推理、GPU 容量、业务质量或生产可用。Schema tests 是 code evidence；本轮 `RUNTIME_NOT_RUN`，未来 WP-01 若仅做内存测试仍不能写服务 `RUNTIME_VERIFIED`。

回滚：R1 可按 review_evidence 的 before 副本恢复六个文档，保留证据包；不自动执行回滚或删除。初轮只新增计划的历史事实见 §11。未来 WP-01 在隔离工作目录/获准仓库中实施，保存变更前清单；回退仅撤销本 WP 新增/修改文件，不使用破坏性全目录 reset，不动外部工程、历史证据或已存在用户修改；没有 DB 或 Release 激活需要回滚。

## 10. 风险、挑战与 OPEN 决策

### 10.1 D01–D10 候选延续；D11–D15 及更新见 §13/ADR R2 表

每项状态均为 **PROPOSED / PENDING_USER_APPROVAL**，没有实现或安装/测试授权。此处保留既有候选编号；D05 的反向 membership 解释由 R2 D15 细化，当前全集以 `ARCHITECTURE_DECISIONS.md` R2 表及 R2.1 的 F01–F05 限定澄清为准；状态不变。

| 决策 | 单一明确候选 | 冻结/授权门槛 |
|---|---|---|
| D01 | uv + Python 3.12 候选 + Pydantic v2；独立 CPU-only 有界依赖/lock/matrix，GPU 后续独立 | 用户批准 CPU/GPU 门槛分离；版本未验证，依赖安装仍需授权 |
| D02 | 受限 scalar/list/closed Record；2020-12 离线生成子集；known 有值，非 known 无 value | Registry 与类型契约审批 |
| D03 | 第 7 节 operator catalogue + strong Kleene T/F/U + required completeness guard；仅静态 typechecker | 不实现运行时 Rule evaluator |
| D04 | mandatory 适用性全扫 + collect_all；未知不消除、有效违规不被其他未知擦除 | 全部 trace 与聚合契约审批；运行行为 WP-05 |
| D05 | ContentRef（kind/ID/version/digest）与 IdentitySelector（kind/ID/version）分离；inventory 外部 pin，前向无环 | R2 D15 去掉多余反向 membership；显式 applicability selector 不执行内容，完整闭包 WP-02 |
| D06 | 一 Question 一个主 Fact、Record 复合值；configured 先 pin / actual 追加审计；03 §9 显式状态表 | 无多输出替代方案、无假 Attempt、终态不覆写 |
| D07 | 02 §2/4/5 自包含资源与语义闭包、M/C 排除/封帧根摘要候选 | 算法/验签/打包/closure 实现与 signature trust gate 留 WP-02 |
| D08 | 每 Profile 一个 ContentRef ToolPlan；Question/Facet 复用满足 exact Schema/context/scope；Tool 条件 deterministic | typed policy、allowlist 和资源边界，不开放任意 map |
| D09 | 独立 ExecutionCoverage：complete/partial/none/not_applicable；no_hits/no_target/failure/partial 不推导 no_violation | 01 §4.8、03 §8.4；不把执行覆盖当模型召回率 |
| D10 | Execution 内按 Profile 独立 Object 身份；跨 Profile 同目标不 merge、不共享 Fact、不宣称物理唯一计数 | 后续如关联需新版本独立合同，无 N02 实例对齐 |

基础约束候选：定义 ID 为 `[a-z][a-z0-9_.-]{0,127}`；版本为稳定 major.minor.patch；SHA-256 为小写 64 位 hex；AST 根为深度 1、最大深度 32、节点数 512、逻辑/in 列表最多 64；Fact 容器根为深度 1（Record/list 每嵌套一层加 1），最大深度 8、每 Record 最多 64 字段、list 最多 256 项、string 最多 4096 字符。单个 Schema 可声明更小上限；这些是待审批的安全边界，不是性能测量结论。单位不隐式转换，未识别 required 类型/operator/feature 拒绝。

### 10.1.1 ContentRef / IdentitySelector 与无循环摘要

R2 D15 候选：Profile → Entity/Facet/Question/ToolPlan/RuleSet 为 ContentRef；Question/ToolPlan 不再反向保存 Profile selector，关系仅正向权威、反向索引派生。IdentitySelector 仅用于显式 Profile/Region applicability 约束。Facet 不反嵌 Question 内容引用。Artifact 字节不含自身 digest 或 Bundle root。Manifest inventory 将所有身份（包括 selector 目标）绑定到唯一 digest；消费仅在 pinned inventory 解析。ContentRef 图 DAG，语义闭包可有 selector 回向关系，后者不是递归哈希边。Schema `$id` 是版本化身份，不嵌自己的 hash。WP-01 仅构建引用类型与注入目录的类型检查，不构建完整 compiler/hasher。

### 10.1.2 一 Question 一个主 Fact 与 Record

QuestionDefinition 用 primary_fact_key + primary_fact_schema_ref，QuestionResult 只有一个主 Fact；不再支持 output_fact_schema_refs[] 或 QuestionOutputSlot。Record 必须声明字段、required/optional 与类型，禁止额外字段和任意递归 map；必填字段非法导致整个 Question Fact error，不能伪造字段级独立 FactStatus。若要独立 unknown/error，拆成多个 Questions。AST 的 RecordFieldRefExpr 仅访问已声明路径；路径不存在/类型不匹配静态拒绝。Retry 保留历史 Fact 并显式选择权威 Attempt，不将历史多条解释为单次多输出。

### 10.1.3 NodeRun / Attempt 转移摘要

完整 guard 表见 03 §9；此处列所有合法边，终态无出边：

| 对象 | 合法边 |
|---|---|
| NodeRun | pending→ready；pending/ready→blocked/skipped/cancelled；ready→running；running→succeeded/failed/cancelled |
| Attempt | created→running/cancelled/superseded；running→succeeded/failed/timed_out/cancelled/superseded |

未运行的 blocked/skipped 不创建 invocation Attempt/started_at。retryable Attempt 失败后 NodeRun 保持 running，新 Attempt 编号递增；terminal NodeRun 再执行需新 identity。Late response 追加审计，不改终态。WP-01 只做数据/纯转移合法性，持久 lease/fencing 不在范围内。

### 10.2 可延后且不得阻塞纯契约基础的决策

- ADR-019/020：模型集、量化、GPU 并发/显存；WP-04 目标主机测量。
- ADR-022：Region geometry 由 Runtime 或空间系统管理；WP-01 仅有不透明的带版本/digest geometry_ref，禁止假装已有空间求值。
- ADR-010：真实审批身份和权限、签名算法/信任根/rotation/revocation、activation scope 优先级；WP-02 冻结并测试，WP-01 只定义无行为的数据结构，若字段布局受影响则先解决相应部分。
- ADR-013/014/015/017：备份保留、MinIO/S3、Redis、ORM/迁移器；数据库与部署包再批准。
- ADR-025：callback 及结果传输；领域结果先独立于 transport。

“可延后”以不泄漏进 WP-01 规范字段为条件；一旦影响字段必填性/含义/值域，就提升为阻塞，不使用 Any 占位。

### 10.3 自我挑战

| 隐藏假设/失败方式 | 控制与剩余风险 |
|---|---|
| frozen Pydantic 等于深不可变 | 嵌套 immutable 结构与 mutation 负例；否则不接受 immutable 声明 |
| JSON Schema 表示所有 Python validator | 分支结构 + 双入口/双验证；跨引用/类型检查显式二阶段，不能只验证模型构造成功 |
| 把每个策略留成任意 map 可“先跑通” | 禁止；未冻结策略直接阻塞对应模型，不伪造完整性 |
| 只给 AST 建 union 就有类型系统 | Registry 解析与纯 typechecker 都属 WP-01；执行求值独立留 WP-05 |
| union 支持任意 Schema 就天然可扩展 | 受限代数、离线引用、大小/深度预算；扩类型要版本/兼容评审 |
| C02 可以直接递归 hash | 多对多引用引入循环；先冻结 identity/content ref 设计及无循环样例 |
| no-hit/工具失败可转成 negative | 分开 coverage、failure、absence；没有证据不能给确定 no_violation |
| 建好所有 runtime 数据模型就是实施后续 WP | 仅字段与校验，禁止 manager/worker/API/求值器；按文件审查 gate 防扩范围 |
| 当前 Python 可用等于依赖兼容 | 未装依赖，必须真实锁定/测试；GPU 矩阵独立且未验证 |
| 示例可直接作正向合约 | 保留原文作 illustrative 回归，创建独立规范 fixtures；不宽松迁就占位内容 |

## 11. 初轮 WP-00 验证证据与独立自审（历史保留）

本节为初轮证据，不代表 R1 当前状态；命令未在 R1 重跑，D01–D08 等编号是历史引用。原计划全文和原始启动包字节均保存在 review_evidence 的 before 副本。当前契约及授权以 §1–10、§12 为准。

### 11.1 实际执行的检查

本轮是静态审查，不把读取文件/完整性检查称作领域测试。所有 `cat`（15 个启动文件）、`rg --files`、`ls` 返回 0；两条 Git 命令均为 128，见第 2 节。

环境/字节快照探测通过 `python3 - <<'PY'` 执行标准库代码，exit 0：输出 Python/platform 和五个包的安装状态，并将原始 15 文件 SHA-256 存入 `/tmp/jimu_wp00_baseline.json`。该临时文件在项目目录外，无项目配置或运行时代码变更。

完整性检查的实际命令如下，exit 0，输出 `manifest_entries: 14`、`issues: []`：

```bash
python3 - <<'PY'
import json, hashlib
from pathlib import Path
manifest=json.loads(Path('PACKAGE_MANIFEST.json').read_text())
issues=[]
for item in manifest['files']:
    p=Path(item['path'])
    if not p.is_file(): issues.append((str(p),'MISSING')); continue
    data=p.read_bytes()
    if len(data)!=item['size_bytes']: issues.append((str(p),'SIZE_MISMATCH'))
    if hashlib.sha256(data).hexdigest()!=item['sha256']: issues.append((str(p),'HASH_MISMATCH'))
print('manifest_entries:',len(manifest['files']))
print('issues:',issues)
raise SystemExit(bool(issues))
PY
```

领域单元测试、Schema tests、negative tests：`TEST_NOT_RUN`，本轮未实施代码且当前解释器没有测试依赖。所有服务、Provider、GPU、DB、回放：`RUNTIME_NOT_RUN`。模型质量、容量和生产可用性：`UNVERIFIED`。没有 code implementation evidence。

写入后的变更边界检查实际执行以下命令，exit 0：`changed_original_files: []`，`added_files: ['planning/WP-01_IMPLEMENTATION_PLAN.md']`，四项 checks 全部 true。检查时计划为 430 行；补充本证据段后行数会增加。该检查证明文件变更边界与基本文档结构，不是 Schema/业务功能测试：

```bash
python3 - <<'PY'
import hashlib, json
from pathlib import Path
root=Path('.')
baseline=json.loads(Path('/tmp/jimu_wp00_baseline.json').read_text())
current={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
changed=[p for p,h in baseline.items() if current.get(p)!=h]
added=sorted(set(current)-set(baseline))
plan='planning/WP-01_IMPLEMENTATION_PLAN.md'
body=Path(plan).read_text()
checks={
 'original_files_unchanged':not changed,
 'only_requested_plan_added':added==[plan],
 'required_topics_present':all(s in body for s in ('目标','模块结构','Pydantic Schema 清单','类型与版本策略','Fact Schema Registry','Rule AST','测试计划','风险','文件变更清单','可验证验收标准','D01','D08','CONDITIONAL','RUNTIME_NOT_RUN','TEST_NOT_RUN')),
 'balanced_code_fences':sum(line.startswith('```') for line in body.splitlines())%2==0,
}
print('baseline_files:',len(baseline))
print('changed_original_files:',changed)
print('added_files:',added)
print('plan_lines:',len(body.splitlines()))
print('checks:',json.dumps(checks,ensure_ascii=False))
raise SystemExit(not all(checks.values()))
PY
```

### 11.2 独立审查方法与结论

独立自审采用写成后按 AGENTS §13、用户九项任务、roadmap WP-01 gate 逐项逆向核对；不将写作过程的判断当验证结果，也未调用子代理。审查重点：是否偷渡后续 WP、是否把建议变确认、是否虚报测试、是否对歧义做未经批准的规范替换。

| 审查项 | 结论 | 依据/限制 |
|---|---|---|
| WP-00 已授权目标与范围 | PASS | 只新增计划；完整启动包阅读和一致性审查 |
| 契约冲突/领域可实施性 | PASS | C01–C11 给出来源、风险与门槛，不能直接无歧义实现 |
| Schema/Registry/AST/包边界/测试/验收覆盖 | PASS | 第 4–9 节提供可追踪交付和拒绝条件 |
| WP-01 决策是否冻结 | CONDITIONAL | D01–D08 待批准；依赖和算法不伪造为已验证 |
| Git 初始化与可追溯性 | CONDITIONAL | 当前未识别为 Git 仓库；没有 HEAD/branch/dirty 证据 |
| 代码与运行时证据分离 | PASS | TEST_NOT_RUN / RUNTIME_NOT_RUN；静态检查独立列出 |
| 安全与数据迁移影响 | PASS | 无 secret、无模型调用、无网络服务、无 DB/migration、无平台修改 |
| 防越权与停止条件 | PASS | 不开发、不安装、不开始 WP-02、不创建 Git/GitHub 资源、不提交推送 |

**WP-00 总结论：CONDITIONAL。** 规划交付可供审批；启动包内容完整，但仓库未初始化且若干契约存在阻塞歧义。**WP-01 尚未实施、尚未验收**，不得据本次自审标记其 PASS。

推荐下一步仅为用户审阅 D01–D08、确认边界与授权 WP-01。批准后只实施 WP-01；完成后重新报告文件、契约、命令及结果、runtime 证据、安全/迁移、限制和 PASS/CONDITIONAL/FAIL，再停止，不能自动进入 WP-02。

## 12. WP-00-R1 修订交付与复审入口（历史）

本节是 R1 交付记录，不代表 R2 当前候选/工作区；R1 原文及历史证据不改。当前 R2 以 §13、修订规格与 ADR R2 表为准。

### 12.1 本次目标、文件与证据

本次仅按用户十项修订要求更新文档，未进行 WP-01 实施。六个现有文件发生变更：

- `planning/WP-01_IMPLEMENTATION_PLAN.md`
- `planning/ARCHITECTURE_DECISIONS.md`
- `specs/01_Runtime_Domain_Model_V1.0.md`
- `specs/02_Runtime_Bundle_Specification_V1.0.md`
- `specs/03_Six_Node_Execution_Contract_V1.0.md`
- `specs/04_Runtime_Technical_Architecture_V1.0.md`

新增 `planning/review_evidence/WP-00-R1/` 文档审计包：`before/` 保存修改前全部 16 个项目文件（15 个原始启动文件及初版计划），`before_sha256.json` 保存修改前大小/哈希，`after_sha256.json` 保存这 16 个路径的修订后大小/哈希，`changes.patch` 保存六个修订文件的完整 unified diff，`README.md` 说明证据边界。证据包不是 Runtime Bundle，不改变原始 PACKAGE_MANIFEST，不自我重写其哈希，也不包含实现文件。

本轮重新读取现有规格/规划/示例，`git status --short --branch` 和 `git rev-parse --show-toplevel --abbrev-ref HEAD HEAD` 均 exit 128（当前仍未识别为 Git 仓库），`rg --files --hidden -g '!.git/**'` exit 0（修订前 16 文件）。没有初始化 Git。

文件复制、文档编辑、哈希清单及 diff 由 Python 标准库/文档补丁完成；未执行任何测试命令或测试断言。读取/diff 是 `STATIC_REVIEW_ONLY`，不作为功能测试证据。初轮 §11 的测试/哈希命令保持为历史，未在本轮重跑。

### 12.2 新旧契约差异与兼容性

| 项目 | 初始/初版计划 | R1 单一候选 |
|---|---|---|
| Question 输出 | output_fact_schema_refs[]；初版计划建议多槽 | primary_fact_key + primary_fact_schema_ref；一次 Question 一个主 Fact，复合 Record；取消多槽 |
| Record | 受限结构化值概念，未明确 Question 基数关系 | 声明字段、required、类型与边界；无额外字段/字段独立状态；RecordFieldRefExpr 静态路径检查 |
| Fact 状态 | 非 known null/value policy 可选 | 非 known 禁止 value（含 null）；reason 保留 unknown/error/not_applicable/not_evaluated 原语义 |
| AST 真值 | 初版严格传播但无完整真值表 | strong Kleene 九行表 + not(U)=U；required/证据 completeness guard 独立于 predicate truth |
| 规则完整性/冲突 | 默认全扫，但仍列 candidate gate 替代项；多命中待定 | V1 mandatory 全量适用性扫描，未知适用性显式 blocked；collect_all 全部有效命中，无优先胜出/提前终止 |
| 引用 | ArtifactRef 被理解为全含 digest，双向可能循环 | ContentRef 为执行/内容依赖；IdentitySelector 为反向适用性身份；inventory 外部 pin，前向无环 |
| Bundle | 缺 prompt/context/tool IO 资源；root preimage 可能自引用 | 补全目录与传递闭包；payload 排除 manifest/checksums，M 排除 root/signatures，domain + 长度封帧 |
| Execution pins | actual deployment 与 context digest 好像创建时即存在 | 创建时 pin 配置/约束，N01 后 Context digest write-once，调用后追加实际 identity 审计 |
| NodeRun/Attempt | 线性图，pre-start blocked/skipped 不清楚 | 03 §9 完整允许边与 guard；NodeRun 内 Attempt 重试，终态不改，late response 独立追加 |
| Release | 定义聚合与 runtime 分类混淆 | Bundle 是配置聚合，Release/Activation 属 runtime 发布桥梁 |
| Coverage | 空集/失败没有独立执行级语义 | D09 显式 scope/coverage/summary；空/未命中/失败/部分无权证明无违规 |
| Object | Execution 内唯一，但跨 Profile 同目标未说明 | D10 按 Profile 独立观察，不合并、不共享 Fact、不宣称物理唯一计数 |
| 持久版本 | 通用信封未列 schema_version | 所有独立外部持久根显式版本，额外字段拒绝 |

这些形状与旧 illustrative 示例不向后兼容。没有已实现服务或生产数据迁移；不重写示例，不为兼容简写放宽验证。若将来要导入旧形状，必须独立显式转换、标明 source/version 并经验证，不能凭空补 digest、trusted metadata 或未知值。

### 12.3 新增 CPU-only 测试用例及归属

以下为测试计划补充，本次 **TEST_NOT_RUN**：

| 场景 fixture | WP-01 只验证的数据/类型不变量 | 后续运行验证 |
|---|---|---|
| JEV no_hits + Objects=[] | discovery=no_hits、coverage=none、summary=uncertain；no_violation/complete 负例拒绝 | WP-05 真实发现能力，不能由 fixture 证明 |
| JEV failed | 原始 failure 与 uncertain/none 保留，不伪造成功/absence | WP-03/04/05 超时和恢复 |
| Profile hit + grounding no_target | grounding/no_target 留痕，空 Object 不自动 no_violation | WP-05 grounding/absence 质量 |
| Grounding failed 全部无 Object | failed 与 no_target 区分；profile-scoped blocked obligations 无虚假 object_id | WP-03/05 后继阻塞 |
| 一 Profile 成功、一 Profile 失败/未被 discovery 返回 | partial，expected scope 不缩成 hits；子集记录仍可引用 | WP-05 运行覆盖汇总 |
| 部分 coverage + 一个有效命中 | summary violation + coverage partial，其他 blocked/unknown 仍在 trace | WP-05 全量扫描/聚合求值 |
| 部分 coverage + 零有效命中 | summary uncertain，不能假报 no_violation | WP-05 执行级结果 |
| 完整非空 Objects、全部 mandatory 已评估 false | complete/no_violation 数据正例；truth/证据/trace 不完整则拒绝 | WP-05 确定性规则执行 |
| 上下文确定排除全部检查义务 | not_applicable + 显式排除依据；不得用 no_hits 代替依据 | WP-05 适用性执行 |
| 同车辆跨 Profile 两次 grounding | 两个 object_id、独立 Profile/Fact/Decision 引用；混用引用负例 | 未来关联能力需独立新合同 |
| F AND U / T OR U | RuleTruth/policy/诊断 fixture 合法，required guard 字段与 blocked 结果一致；不执行 AST | WP-05 真值表 evaluator 与 guard 行为 |
| pending→blocked / ready→skipped / terminal→running | 前两条有效且无虚假 invocation Attempt；后一条无效；created/running Attempt 边全覆盖 | WP-03 持久状态/fencing |

WP-01 不实现 N06 扫描循环、Rule evaluator、空集运行聚合器、跨 Profile 关联器、Bundle hasher/compiler/admission、Provider/Tool worker；CPU-only domain consistency validators 不代表这些运行能力已实现。

### 12.4 最终验收标准与当前审查结论

WP-01 最终验收以 §9.2 G0–G9 为准，必须同时满足：

1. 用户批准 D01–D10 和限定工作范围，安装/测试动作有明确授权。
2. Domain/Registry/AST static checker/JSON Schema 的契约清单完整、strict、版本化、typed、无 Any 占位；一 Question 一个主 Fact 与 Record/schema/scope 不变量有正负例。
3. ContentRef/IdentitySelector 字段与用途严格区分，注入的有限目录可做类型解析；不把完整闭包/摘要计算偷偷加入 WP-01。
4. T/F/U、required guard、full mandatory scan、collect_all、Coverage 及 Profile-specific identity 有清晰数据模型、静态检查及契约 fixtures；无 runtime evaluator/服务实现。
5. NodeRun/Attempt 全部合法边和非法边有 CPU-only 纯校验；终态无出边、重试/late response 形状不覆写历史。
6. 每个根模型生成 JSON Schema，重复生成字节一致；Pydantic/JSON Schema 双验证通过；无法由 JSON Schema 表达的跨引用语义另作显式域校验。
7. 已授权 CPU-only 单元/Schema/负向测试实际通过（0 failed，必需用例不 skip），质量/导入边界检查通过，精确命令/版本/exit code/结果留证。
8. 无 DB/API/模型调用/GPU/Rule 求值/部署/平台修改/迁移/提交；交付兼容说明与 WP-01 验证报告后停止，不进入 WP-02。

本轮修订的文档自审：**CONDITIONAL**。C01–C11 已在候选文本中逐项对齐；D01–D10 全部 **PROPOSED / PENDING_USER_APPROVAL**；独立复审尚未发生，WP-01 未实施、未验收。没有 API key、模型成本、数据迁移或历史运行证据修改。依赖兼容、摘要/签名算法实现与真实运行行为仍 UNVERIFIED。

`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。下一步仅为独立复审和用户实施授权；本轮完成后停止。

## 13. WP-00-R2 Architecture Contract Closure（历史复审入口；R2.1 见 §14）

本节保留 R2 交付记录，其完整原字节在 R2.1 before 副本；当前 F01–F05 以 §14 和修订规格为准。

### 13.1 基线、范围、风险与历史保护

基线为 main / `46388ce1d8f03e0b122b3038a5930eb0aa3ceb01`，修改前 clean。
本轮仅修改十个已存在文档并新增 R2 审计目录。无 Python 实现/pyproject，
不安装、不测试、不调用模型/GPU/外部 API、不建分支、不初始化 Git、不 commit/push。
AGENTS、原始 PACKAGE_MANIFEST 和 WP-00-R1 历史目录均不改。§11/12 是历史，
不得把其中旧 Git 状态、反向 selectors 或旧验收范围当作当前契约。

风险是引用/策略字段改名导致 R1 候选不兼容，以及“有类型”被误认为已实现执行器。
控制是 before 原字节、精确 diff、当前字段统一于 Domain §9–14，并分开有限类型
检查与完整图编译/实际调用。回滚仅可在另行选择后恢复 R2 before 中十个文档；
保留 R1/R2 证据，不自动 reset/restore、删除或改写历史。

### 13.2 R2-01 至 R2-08 整改矩阵

以下“文档闭合”均为 `RESOLVED_IN_CANDIDATE / PENDING_USER_APPROVAL`，不是实现或
功能测试结果。DM=specs/01，B=specs/02，N=specs/03，TA=specs/04；另有本计划与 ADR。

| 项目 | R1 实际缺口 | R2 候选方案 | 实际修改文档 | 用户决策 |
|---|---|---|---|---|
| R2-01 | 两个 capability refs 无对应实体/kind，prompt 与 grounding provenance 不完整 | 25 个 ArtifactKind，复用 ModelBinding/ToolDefinition，PromptTemplate/grounding union/外部能力分离；不增独立 Capability Artifact | DM §3/9/10、B §2/5、N §4–6、TA、计划、ADR、README、测试策略 | D11 待批准 |
| R2-02 | “configured Questions”未说明全量/子集、mandatory 与依据 | 比较 A/B，模式显式；建议初始 all_declared；B 仅显式选择 optional，mandatory 永远加入，fallback 与 selection record 明确 | DM §11/14、N §4–6、B 完整性、计划、ADR、README、roadmap、测试策略 | D12 模式/回退待批准 |
| R2-03 | Facet Schema 与 Question 主 Schema 关系缺静态规则 | 保留 Facet allowlist；Question 主 Schema 在全部关联 Facet 精确交集中；semantic_key/unit/scope/context 复用检查 | DM §3.4/12、B §5、计划、ADR、测试策略 | D13 待批准 |
| R2-04 | 抽象 policy 字段无完整类型和输入域 | 16 个封闭 PolicyArtifact 分支，必填/可选/枚举/无默认/范围/校验/效果表；applicability 无 Object Facts，Tool 无 N06 | DM §14、B、N、计划、ADR、roadmap、测试策略 | 认可 policy 形状；业务值保留 OPEN |
| R2-05 | producer、Tool 多输出、唯一性、Attempt 权威未闭合 | ProducerCatalogue/Declaration、Tool value_path 映射、一对一/一对多、冲突域、whole-batch/step authority revision、FactSelector | DM §13、B §5、N §7/8、TA、计划、ADR、测试策略 | D06 的细化待批准 |
| R2-06 | 保守 no_hits 可能高复核负担，但无替代评估 | 比较 A/B/C；保留 D09、推荐先 A 后离线评估 C；明确风险/复核/成本指标与置信区间，不编数据 | N §8.5、DM CoveragePolicy、TA、计划、ADR、测试策略 | D14；阈值/评测/自动放行另批 |
| R2-07 | Q/ToolPlan 反向 selectors 造成双权威/版本耦合 | 比较 A/B/C，推荐 B，Profile 正向 ContentRef 唯一关系，反向派生；IdentitySelector 仅显式适用性 | DM §3/7/9、B §5、计划、ADR、测试策略 | D15 待批准；不重引 digest 环 |
| R2-08 | DEPENDENCIES 仍要求 CPU 锁之前全 GPU 验证 | CPU Gate WP-01 与 GPU Gate WP-04 明确分开，禁止混依赖/伪报兼容 | DEPENDENCIES、TA、计划、ADR、roadmap | D01 仍 PROPOSED / PENDING_USER_APPROVAL |

### 13.3 D01–D15 当前审批状态

当前完整决策表在 `planning/ARCHITECTURE_DECISIONS.md` 的 R2 小节，旧 ADR/R1 表
保持历史。D01–D15 **全部 PROPOSED / PENDING_USER_APPROVAL**；没有新 CONFIRMED。

| 决策 | 当前主题 | 文档层状态 |
|---|---|---|
| D01 | CPU/GPU 两阶段依赖 | PROPOSED / PENDING_USER_APPROVAL |
| D02 | typed Fact + exact Registry + value_schema | PROPOSED / PENDING_USER_APPROVAL |
| D03 | typed AST / T-F-U / input_domain | PROPOSED / PENDING_USER_APPROVAL |
| D04 | mandatory 全扫 + collect_all | PROPOSED / PENDING_USER_APPROVAL |
| D05 | ContentRef/IdentitySelector、D15 细化 | PROPOSED / PENDING_USER_APPROVAL |
| D06 | 单主 Fact/多 Tool Facts、authority、状态历史 | PROPOSED / PENDING_USER_APPROVAL |
| D07 | Bundle 自包含/closure/摘要边界 | PROPOSED / PENDING_USER_APPROVAL |
| D08 | ToolPlan/Question 复用与 typed guard | PROPOSED / PENDING_USER_APPROVAL |
| D09 | Coverage 空集/partial 安全语义（保留） | PROPOSED / PENDING_USER_APPROVAL |
| D10 | Profile-specific Object 身份（保留） | PROPOSED / PENDING_USER_APPROVAL |
| D11 | ArtifactKind/直接能力绑定/Prompt/provenance | PROPOSED / PENDING_USER_APPROVAL |
| D12 | QuestionSelectionPolicy/Record | PROPOSED / PENDING_USER_APPROVAL |
| D13 | Facet–Schema 精确交集/复用 | PROPOSED / PENDING_USER_APPROVAL |
| D14 | no_hits 策略与评测，保守 A 推荐 | PROPOSED / PENDING_USER_APPROVAL |
| D15 | 正向 membership 唯一权威 | PROPOSED / PENDING_USER_APPROVAL |

OPEN 是业务/部署待填值的状态，不是未经批准的默认：证据/retention、预算、clock
tolerance、deployment IDs/versions、评测数据/样本数/风险和复核阈值。签名算法/信任、
激活优先级/空间系统归属为后续 WP OPEN。它们不能用 Any 占位，也不能从 fixtures
推成生产参数；不阻塞纯类型建模，但会阻塞使用该配置的 Bundle 准入。

### 13.4 新旧契约、模型与兼容性

- 不新增独立 DiscoveryCapability/GroundingCapability 实体或 Registry 服务。
  Profile.discovery_capability_ref → discovery_binding_ref[model_binding]；
  grounding_capability_ref → grounding_binding(model/tool discriminated union)。
  新增 observation_binding_ref、question_selection_policy_ref、rule_set_refs。
- 新增版本化 PromptTemplateDefinition/PromptVariable、25-kind ArtifactKind 表、
  静态资源描述、ReviewReportRef/ExternalCapabilityRequirement 等封闭契约。
  GroundingRecord.producer 改为模型/工具联合分支，Object 保存 selection ref/set。
- 删除 Question.applicable_profile_selectors、ToolPlan.applicable_profile_selector
  的候选字段，改由 Profile 正向关系与编译派生；剩余 applicability selectors
  仍由 pinned inventory 解析。R1 历史字节不删。
- Question 主 Fact 不改为多输出；Record fields 无独立 FactStatus。新增 semantic_key、
  payload_schema_ref（value_schema），Facet allowlist 取精确交集。
- 抽象 policy 字段统一为有类型 `_policy_ref` / `_requirements_ref`，目标为
  PolicyArtifact 指定 policy_kind。Business 值必须明确提供，不能注入任意 map。
- 新增 ProducerCatalogue、FactProducerDeclaration、ToolFactOutputDeclaration/
  ToolFactOutputMapping、ProducerSelector、AuthoritativeResultSelection 等 Schema；
  FactSelector 统一 scope/fact_key/fact_schema_ref/producer_selector/field_path。
  每个候选 Fact 保留 source Attempt；消费选择明确 authority revision。

R2 与 R1 是不向后兼容的**候选**形状变更；尚无已发布机器契约版本、服务或生产数据。
文档 revision R2 不自动改成已批准 schema_version，也不宣称 SemVer 兼容。
批准首次冻结前须确定 initial schema version；已发布制品如以后变更，需新 major/
显式转换和读旧策略。原 examples 仍 illustrative，不自动补哈希或迁移数据。

### 13.5 Domain → Bundle → Node → Fact → Rule → Decision 完整引用链

1. 定义：Entity/Region Context Schema → Profile（Facet/Question/ToolPlan/RuleSet，
   discovery_binding_ref、grounding_binding、observation_binding_ref）；Question →
   Facet + FactSchemaDefinition → generated value_schema，及 PromptTemplate →
   response_schema + declared variables/partials；所有内容目标在 ArtifactKind 表。
2. BundleManifest inventory 固定这些 ContentRefs、全部 PolicyArtifact/资源、
   ModelBinding IO、Tool IO 与 ProducerCatalogue。Profile 正向 membership、剩余
   applicability selectors 语义闭包明确；无编译器反写定义，不含自己的 digest。
   外部 model/tool implementation/review approval 属显式 trusted capability，不冒充
   Bundle ArtifactKind。完整 compiler/admission 是 WP-02。
3. Release pin Bundle digest + ExecutionPolicy/workflow version；Task acceptance pin
   Release；Execution pin 同一输入/配置。N01 产生不可变 ContextSnapshot/来源记录，
   N01 后 write-once context digest，不伪造创建时尚不存在的值。
4. N02 以 discovery ModelBinding 和 pinned Profile catalogue/policy 产生 ProfileHit +
   QuestionSelectionRecord/effective_question_refs。Mandatory producer completeness
   由编译/准入独立检查；模型只能提出 optional 子集、无权减少 required 集合。
5. N03 按 Profile 独立定位，模型/工具来源有不同 provenance；Object 继承同一 selection
   ref/set，保留原图坐标/变换，跨 Profile 不合并。N04 对每个 Object 的有效 Questions
   一次联合请求；整批 Attempt/source 响应与每 Question 一个主 Fact 独立留痕。
6. N05 不看 N06，执行 Profile 固定 ToolPlan。每 step 的 tool_output_schema →
   declared value_path → 每条 FactSchema 校验，一 step 可多 Fact，错误非负证据。
7. FactProducerDeclaration 将 Question primary / ToolStep named output 固定到
   schema/key/scope；Fact 候选和 Attempt 全留；AuthoritativeResultSelection 显式
   选择 whole-batch/step revision。Rule FactSelector 匹配同一声明及 Record field_path。
8. N06 的 Rule applicability 只读声明 Context/Region/Profile；condition_ast 可读验证
   Facts。所有 mandatory Rules 全扫，候选只建议；T/F/U 与 required guard 分离，
   collect_all 所有有效命中。Decision 固定 Rule/Fact/authority/evidence/engine refs。
9. ExecutionCoverage 聚合执行义务/empty/partial，与状态和 Decision.result 分开；
   no_hits/no_target/failure 不构成确定否定。D14 不启用其他自动放行协议。

### 13.6 十三项架构一致性独立静态自审

本表是写成后的第二遍人工契约追踪，不是 pytest、Pydantic 或 Schema 执行结果。

| 项目 | 静态结论 | 证据落点 |
|---|---|---|
| Domain / Pydantic inventory | PASS（候选） | §5 新增清单覆盖 01 §9–14 模型 |
| 每个 ContentRef kind 有定义 | PASS（候选） | 01 §9，运行记录/外部要求不冒充 ArtifactKind |
| Bundle 配置资源闭包 | PASS（候选） | 02 §2/5，含 producer catalog/policies/Prompt/schema（runtime selected-set schema 有 invocation digest/evidence） |
| N02 Question 集合确定 | PASS（候选） | 明确 policy/partition/ordered set/selection record |
| N03 完全继承 | PASS（候选） | 03 §5，不因 grounding 修改 Questions |
| N04 一 Object 一联合请求 | PASS（候选） | 03 §6，同一 selection 下整批 retry |
| N05 独立于 N06 | PASS（候选） | 01 §13/14、03 §7，禁止 Candidate/Decision selectors |
| Producer / Rule selector | PASS（候选） | 同 schema/key/scope/producer/field_path，显式 authority |
| applicability / condition 输入域 | PASS（候选） | 01 §13.3/14、03 §8：前者禁止 Object Facts |
| N06 mandatory 全扫 | PASS（候选） | 03 §8.2/8.3，mandatory Questions 依赖完整性 |
| Coverage 空集/部分执行 | PASS（候选） | D09 不变；03 §8.4/8.5 与 typed CoveragePolicy |
| Release / Execution pin 不可变 | PASS（候选） | 01 §4/7、02 §8，actual invocation 追加审计 |
| WP-01 依赖/范围一致 | PASS（候选） | DEPENDENCIES CPU/GPU 两门禁、TA、roadmap、§4/9 |

### 13.7 WP-01 最终门槛与阻塞项

以 §9 G0–G10 为当前验收表：新增 G10 不扩大到实际 compiler/evaluator。
必须获得用户对 D01–D15、有限交付/依赖/测试动作的明确审批；批准前不实施。
纯域验收需：完整模型/生成 Schema 映射、strict extra/版本/引用类型、Registry 与
Facet/producer/authority/input_domain 正负例、selection/grounding provenance/policy
branches、无模型/基础设施依赖、重复生成一致、已授权 CPU-only 测试真实通过并留证。
不得用静态 PASS 冒充这些未来功能 gate 已通过。

当前需要拍板：D11 直接绑定与种类清单；D12 初始 all_declared 或显式 B 模式和回退；
D13 Facet 交集约束；D15 去除反向 membership；D14 是否接受 A 作为 V1 保守行为
（B/C 可延后）；D01/既有 D02–D10 候选整体冻结。场景参数与评测目标仍 OPEN，
不要求本轮凭空填值。批准合同类型与批准真实 Bundle/自动放行部署是不同 gate。

### 13.8 本次交付、证据和停止结论

修改十个现有文档：README、specs/01–04、planning 的 WP-01_IMPLEMENTATION_PLAN、
ARCHITECTURE_DECISIONS、DEPENDENCIES、TEST_STRATEGY、DEVELOPMENT_ROADMAP。
新增 review_evidence/WP-00-R2：README、before_sha256、after_sha256、changes.patch
以及只含这十份修改前字节的 before/。清单记录基线 SHA、每文件大小/SHA-256；
protected_files 另记录 R1 证据/AGENTS/PACKAGE_MANIFEST 哈希，避免重写历史。
审计文件本身不纳入 after 自身摘要，不形成自引用；diff 是文档 diff 不是代码提交。

实际动作仅本地读取、编辑、复制、标准库字节哈希/difflib 和 Git 只读状态查询；
这些属于 STATIC_REVIEW_ONLY，不是功能测试。未安装/运行依赖、pytest、Schema、
lint/typechecker、服务、Provider、GPU；没有 DB 迁移/secret/外部平台修改。
R2 HEAD 保持基线、修改留工作区，未创建分支、commit/push 或调用外部 API。

**R2 文档自审：PASS（仅静态完整性/候选一致性）。整体审批状态：CONDITIONAL。**
WP-01 已具备候选方案的审批材料，尚未批准实施、尚未完成任何功能验收。仍需用户及
GPT Pro 独立复核上述决策/业务参数；真实依赖兼容、模型/发现质量、完整负例协议和
GPU 能力均 UNVERIFIED。`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。
本轮到此停止，不实施 WP-01、不安装、不测试、不提交、不推送。

## 14. WP-00-R2.1 Final Contract Closure（历史；Applicability 以 §15 为准）

### 14.1 有限范围与基线

本轮只处理 F01–F05，不重做 R2 规划。开始前 main/HEAD/工作区符合预期：
`40af8a17855b27063da94b3c9891272f3e3fc4e3`、clean、50 个 tracked files。
仅修改本计划、ADR、TEST_STRATEGY、specs/01–04 七份文档。AGENTS、README、
DEPENDENCIES、roadmap、原始 PACKAGE_MANIFEST、examples、R1/R2 审计全部不改。
本轮不建分支、不 reset/restore、不 commit/push；不实施/安装/测试/模型/GPU。

### 14.2 F01–F05 逐项交付

DM=specs/01，B=specs/02，N=specs/03，TA=specs/04；P=本计划，T=测试策略。
每项状态均 **RESOLVED_IN_CANDIDATE / PENDING_USER_APPROVAL**，不是功能通过。

| 项目 | 原问题 | 修订方案 | 实际文件 | 审批要求 |
|---|---|---|---|---|
| F01 | Profile/RuleSet/Rule 三层未定义组合/优先/trace | 三层 AND、parent-first gate、active U 阻塞；false 只排除对应子树；not_evaluated 无 truth；metadata-only；ApplicabilityTrace 连接 Coverage/Decision | DM §3.8.1/14，N §8.2，P §5/7/9/14，T §2.3 | D03/D04/D09 细化，待用户批准；不以 child false 擦掉 parent unknown |
| F02 | ordered_first_eligible 不能保证云优先 | remote_primary + primary_deployment_id；其他 deployments 仅显式 fallback；LocalPrimaryException 明确 scope/approval/有效期；configured/actual provider 与路由 trace；Tool Worker 独立 | DM §3.9.1/14，B §5，TA §4，ADR R2.1，P | 已确认 remote-first 的约束落实；具体字段及任何 local exception/真实 fallback 仍须审批/兼容/独立评测 |
| F03 | 可选 model_invocation_ref 无方法分支约束 | all/context 无选择调用；model 必有实际 Invocation；shared 同 ID 双 purpose；额外调用必须显式 policy/budget/binding/新 ID；fallback 保留触发 Attempt/失败原因/有条件派发证据 | DM §11.1/14，N §4，P §5/8/14，T §2.3 | D12 A/B 不变；仅细化证据合同，额外调用未获运行授权 |
| F04 | TypeDefinition→value_schema 的独立输入/摘要顺序未冻结 | 规范化 type 投影 + 固定 generation spec；child 先生成/独立 digest，parent 后引用/摘要；生成器不读 parent ID/digest；同输入重复生成同 canonical 内容 | DM §12.1，B §2/5，P §5/6/8/14，T §2.3 | D02/D07 细化；WP-01 Schema 内容/重复性，WP-02 完整 Bundle integrity，均尚未实现 |
| F05 | N03 Tool 与 N05 Step 的 ToolExecution 来源不可判别 | common refs/实际版本 + origin union；N03 grounding_binding 无 Object/Fact，N05 tool_plan_step 带 plan/step/declaration/Object；同 Tool 不同 audit ID/Attempt | DM §4.5，N §5/7，P §5/8/14，T §2.3 | D06/D11 细化，待批；N03 不自动成为 N05 producer |

### 14.3 D01–D15 与不变项

D01–D15 **逐项均保持 PROPOSED / PENDING_USER_APPROVAL**（完整主题表仍见
§13.3 与 ADR R2 表，R2.1 附记仅澄清五项，无 D16/新决策编号）。没有将任何候选
提升为 CONFIRMED；ADR-004 remote-first 原已确认，其强制约束不能被“候选”字样削弱。
具体 LocalPrimaryException 必须独立获批；本轮不批准任何例外。

D09 no_hits 保守、D12 A/B、D13 精确 Facet 交集、D15 正向权威、一个 Question 一个
主 Fact、Step 多 Tool Facts、mandatory 全扫、collect_all、两类引用、CPU WP-01 /
GPU WP-04 两阶段门禁全部保留。F01 的 RuleSet/Rule gate 不改变 N05 的预绑定计划。

### 14.4 完整 Schema 清单、负向计划与最终验收

**完整 WP-01 Schema 清单以 §5 的全部表为准**：基础/枚举/typed Value；Entity/Region；
Profile/Facet/Question 与 grounding bindings；Tool/ToolPlan/step mappings；ModelBinding/
Provider/Fallback；Rule/RuleSet/AST；Bundle/Release 数据；Task/Context/Execution/NodeRun/
Attempt；Object/Grounding；Fact/producer/authority；ToolExecution；Evidence/ModelInvocation；
Decision/Coverage/Replay；六节点 I/O；Prompt/static resources；16 typed Policies；Registry/
projection/generation。F01–F05 新增的 trace、条件联合与 generation input 已纳入该清单，
不是另一套手工 JSON Schema。每个持久根显式版本，生成器 index 必须逐根映射。

新增负向用例以 TEST_STRATEGY §2.3 为唯一详细列表，均 **TEST_NOT_RUN**：
- F01：U→F、未知父被 child false 排除、not_evaluated 仍带 truth、缺祖先依据、非法 Facts/candidates。
- F02：remote 模式 local primary、无效/过期/错 scope 例外、未授权 timeout 切换、无兼容/评测、假 actual version、混用 Tool scheduling。
- F03：model 无实际/错 purpose/错 execution ref、shared 不同 ID、无预算隐形 extra call、fallback 缺触发/原因或伪造未派发调用、发现失败伪造命中。
- F04：parent digest/ID 进入 input/child、Schema 与 projection 不符、重复/递归/非有限类型、环境可变参数、手工漂移或对不同生成版本误称同输出。
- F05：缺公共 refs、origin 字段混合、错 N03/N05 parent、grounding 产生 Facts/带 Object、Step 无 plan/producer/Object、两个 origin 共用一个 invocation 身份。

最终验收为既有 G0–G10 **加 G11**，范围不扩大。G11 要求类型/条件 refs/记录不变量
和独立生成内容的 CPU-only 正负例及重复性真实通过；实际 routing/gate evaluator/
Tool scheduler/调用预算执行、信任/评测验证、完整 Bundle hashing/admission 仍属后续 WP。
用户授权之前不能执行任何这些测试，更不能从文档断言 gate 已通过。

### 14.5 兼容性、风险与十项静态一致性复核

R2.1 收紧候选输入：旧 ordered_first_eligible 替换为显式 Primary 路由；selection 的
全局 optional model ref 替换为 method 分支条件；FactSchemaDefinition 增加 generation
spec；ToolExecution 必须有 invocation_origin；适用性增加层级 trace。旧 R2 候选对象
不能靠填默认值静默升级。首次正式 schema version 待批准冻结；已有制品如后续要迁移，
必须显式转换/兼容说明，保留原文，不修改原始 Release/Execution 证据。本轮无 DB/数据迁移。

独立自审在写成后按下表逐条人工追踪，不调用子代理、不作运行验证：

| 检查 | 静态结论 / 落点 |
|---|---|
| 三级 applicability | PASS（候选）；DM 七行 gate 表/trace 与 N §8.2 一致，U 不当 F |
| Remote Primary | PASS（候选）；DM/TA/Bundle 强制 primary 与显式例外/transition，Tool placement 独立 |
| Selection 调用证据 | PASS（候选）；四 method 分支、shared/extra/fallback refs 与实际 ID/预算约束 |
| parent/child 摘要 | PASS（候选）；DM §12.1 独立 projection input，B 保持 child-before-parent |
| Tool 调用来源 | PASS（候选）；origin discriminated union，N03/N05/producer 关联区别明确 |
| Bundle 引用闭包 | PASS（候选）；无新 ArtifactKind；inline generation/call spec 与既有 Policy/Binding/Review refs 完整 |
| WP-01 Schema 清单 | PASS（候选）；§5 覆盖 trace/provider/selection/generation/Tool origin 新字段 |
| 五组负向计划 | PASS（文档覆盖）；T §2.3，未执行 |
| AGENTS / WP 范围 | PASS（静态）；AGENTS 不改、Domain/Registry/静态 AST/Schema/CPU-only 边界不变 |
| R1/R2 历史保护 | 以 R2.1 before/after 哈希审计为证；禁止修改，非功能测试 |

风险控制只针对这五项：不让 gate 优化隐藏 unknown、不以列表顺序冒充 fallback 许可、
不伪造模型调用、不让 generator metadata 形成 parent digest 环、不把 N03 输出晋升为
N05 producer。预算/质量/部署参数仍 OPEN；不凭空填写数值，不追加新业务设计。

### 14.6 证据与停止结论

`planning/review_evidence/WP-00-R2.1/` 含 README、before_sha256.json、after_sha256.json、
changes.patch 和仅七个修改文件的 before 原字节；记录 R2 基线 SHA 和所有前后 SHA-256。
另对其余 43 个 tracked files 做保护哈希（含 R1/R2 全部 34 个历史证据文件）。审计
输出不把自身纳入摘要，避免自引用。字节/差异检查是 STATIC_REVIEW_ONLY，不是 Schema 测试。

**R2.1 文档自审 PASS（仅候选契约完整性/静态一致性）；整体审批 CONDITIONAL。**
F01–F05 已具备独立复审材料；D01–D15 和 WP-01 仍未批准。没有功能测试、依赖兼容、
模型质量或运行时通过的声明。`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。
完成即停止，等待 GPT-6 独立复审及用户审批；不实施、不安装、不测试、不 commit/push。

## 15. WP-00-R2.1.1 Applicability Contract Closure（当前）

### 15.1 目标、范围与逐项整改

基线 main / `2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`；初始 clean，61 个 tracked files。
仅修改 specs/01、specs/03、ADR、本计划、TEST_STRATEGY 五份文档并新增 R2.1.1 审计包。
AGENTS、Bundle/技术架构规格、依赖门禁、README、roadmap、examples、原始清单和
R1/R2/R2.1 历史证据受保护。§14 保留 R2.1 历史；其中三级 AND 简写按本节解释。

| 问题 | 解决方案 / 落点 | 状态 / 审批 |
|---|---|---|
| R3-01 执行归属 | N01 仅 Context；Workflow Pre-N02 Profile Gate 生成三分区/trace；N02 仅 eligible，不重做排除；N06 继承 Profile 结果，负责 RuleSet/Rule；N05 独立。Domain §3.8.1/4.8、Node §3.1/4/8、本计划 §5/7/9、测试 §2.4、ADR R2.1.1 | RESOLVED_IN_CANDIDATE / PENDING_USER_APPROVAL |
| R3-02 层级与三值边界 | 有序 gate 的 F 证据排除、U unresolved/后代 NE、T 才推进；Strong Kleene 仅单 AST 内；Required Evidence Guard 独立。Domain §3.8.1/4.7/13.3/14、Node §8.2–8.4、同上计划/测试/ADR | RESOLVED_IN_CANDIDATE / PENDING_USER_APPROVAL |

三种机制不可合并为通用 bool。Profile U + 子 RuleSet F、Profile F + 子 Rule F、
RuleSet U + 子 Rule F 均不得执行子 policy；子 trace 只保留 decisive ancestor 和
not_evaluated，无 TruthValue。全 T 才可进入 condition；condition U 仍 uncertain，
完整 Evidence Guard 不能将其变为否定。WP-01 不实现任何 Gate/Workflow/Rule 求值器。

### 15.2 Applicability Schema 和完整链路

完整 Schema 清单以 §5 全部表为准，本轮六类记录为：

| Schema | 职责 / 不变量 |
|---|---|
| ProfileEligibilityEvaluation | Workflow 所有；配置全集、三分区、applicability_traces，固定 Execution/Bundle/Snapshot，无 aggregate truth/假 NodeRun |
| RuleSetApplicabilityEvaluation | N06 RuleSet 包装记录；固定父 Profile evaluation/目标 trace，实际评估才有真实 NodeRun/Attempt |
| RuleApplicabilityEvaluation | 额外固定 Rule/父 RuleSet evaluation，不另写与 trace 冲突的 truth；未到达为 ancestor projection |
| ApplicabilityTrace | 有序路径/decisive level；继承祖先 effective_truth 不意味着子级有 truth |
| ApplicabilityLevelTrace | evaluated 才有 T/F/U、owner/source；NE 禁 truth，必有祖先；F 有依据、U 有诊断 |
| UnresolvedObligation | 原义务账本 unresolved 类型分支，共用 obligation_id，不重复计数、不伪造 Object |

Policy 定义仍在 policies.py；六类记录/trace 放 runtime/decisions.py；Coverage 仍在
runtime/coverage.py。DiscoveryProfileDiagnostic 替换原含混 FilteredProfile，仅审计
发现输出异常；Guard authority 字段归 RuleEvaluation。没有新 ArtifactKind/PolicyKind/
NodeId/Registry 服务/架构层；RuntimeRecordRef 不成为 Bundle 配置依赖。

Pinned Release/Bundle → Profile + ApplicabilityPolicy + 声明元数据 Schema
→ N01 ContextSnapshot → Workflow Pre-N02 ProfileEligibilityEvaluation/Trace
→ eligible_profiles → N02 ProfileHit + 固定 QuestionSelectionRecord/effective Questions
→ N03 Object（原样继承 Questions）→ N04 一 Object 一次联合请求、独立主 Facts
→ N05 Profile 预绑定 ToolPlan、声明 Tool Facts/authority
→ N06 固定 Profile 前缀、RuleSet/Rule gates → Condition AST + 独立 Required Evidence
Guard → collect_all Decision → ExecutionCoverage / 同一义务与 trace refs。

Gate excluded/unresolved 分支直接进入 Coverage，不进入 N02 模型候选；eligible 为空
则 N02 skipped/no_eligible_profiles、discovery not_evaluated，无调用/no_hits。全部证明
排除为 not_applicable，有 unresolved 则 none/uncertain；真实 N02 no_hits 仍按 D09。
混合分支保留有效违规和 partial coverage。空 Object 不造 Decision；Coverage 独立保存
excluded/blocked/unresolved/NE 与祖先依据。mandatory 全扫要求完整记账，不执行被祖先
停止的子 policy。RuleSet/Rule 不修改或取消 N05 ToolPlan。
N01 失败未到 Gate 时保留上游 blocker，不要求虚构 evaluation ref；无适用性祖先阻断、
仅因 no_hits/grounding 失败而未到 N06 时保留执行依赖原因，不伪造 applicability UNKNOWN。

### 15.3 测试、验收与审批阻塞

TEST_STRATEGY §2.4 新增 H01–H07、A01–A13 正负向 fixture，覆盖六条特殊路径、F/U
依据、NE 无 truth、祖先/分区/输入域、N02 双重排除、空 eligible/no_hits、N05 独立、
authority 和共享义务 ID。均为未来 CPU-only 模型/Schema/静态引用检查，TEST_NOT_RUN。

WP-01 最终验收为 §9.2 **G0–G12 全部**：用户先批准范围、当前候选及依赖/测试；§5 全表
具备严格模型源/显式根版本/生成 index/正负 fixture；Registry、AST 类型检查、值/引用/
生命周期约束与 CPU 依赖边界经授权验证；Schema 重复生成一致；G12 六类模型、owner、
ancestor、三机制分离和 Coverage/Decision 关联负例拒绝；兼容/安全与真实证据报告完整。
不把实际 Gate/Workflow/Rule 执行或完整 Bundle compiler 纳入 WP-01，不开始 WP-02。

D01–D15 逐项状态仍见 §13.3 / ADR R2 表：全部 **PROPOSED / PENDING_USER_APPROVAL**，
无 CONFIRMED 提升、无 D16。D09 保守 no_hits、D12 A/B、D13 精确交集、D15 正向权威、
mandatory 全扫/collect_all 和 CPU WP-01/GPU WP-04 门禁不变。阻塞仍为用户对候选契约
和工作包的审批；不新增业务选择，已有部署/质量阈值 OPEN 值不得由默认值代填。

### 15.4 兼容性、自审与停止

相对 R2.1 新增三类 evaluation 包装及 UnresolvedObligation，精化两类 trace；增加
eligibility/parent/owner/source/ancestor/authority refs；替换 N02 FilteredProfile 及其
含混的排除职责。旧候选记录缺这些字段不能声称符合新契约；未来正式 schema version
和显式转换须随 WP-01 审批冻结。无历史数据/DB 迁移、凭据或需求平台变更。

写成后以输出反查 owner、输入域、父链和义务的第二遍独立静态自审：执行归属、三层
顺序/NE、三机制分离、Coverage/Decision 空目标、六 Schema 覆盖、N03/N04/N05/N06
原职责及配置闭包均 PASS（仅文档）。主要失败风险为祖先 U 被抹掉、伪造未执行记录、
N02/N06 双写资格、混用 bool、空集当无违规；对应负例均已规划，实际运行效果未验证。
AGENTS 与历史保护以摘要核对为证。回滚仅参考本轮 before/patch，不自动 restore/reset。

证据：`planning/review_evidence/WP-00-R2.1.1/` 的五份 before 原字节、before/after SHA-256、
完整 changes.patch 和 56 个保护文件哈希（含 45 个 R1/R2/R2.1 历史证据文件）。
**文档自审 PASS；整体审批 CONDITIONAL / PENDING_USER_APPROVAL**。已具备独立复审和
用户审批的材料条件，不等于 WP-01 已授权或验收。立即停止，等待 GPT-6 独立复审及用户。
`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。无代码、安装、测试、模型、
服务、Commit 或 Push。
