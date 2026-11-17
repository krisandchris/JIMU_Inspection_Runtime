# WP-01 Implementation Plan — Domain Model and Contract Foundation

状态：WP-00-R1 / PROPOSED / PENDING_USER_APPROVAL；本文件不构成实施授权。
修订范围：仅文档；原计划及原始启动包逐字节归档于 `planning/review_evidence/WP-00-R1/before/`。  
审查日期：2026-11-17（Asia/Shanghai）。工作包：WP-00 Bootstrap Review。  
证据等级：`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。

## 1. 目标、范围与审批边界

初轮获授权的工作是阅读、初始化审查和编写本计划，唯一项目内变更为本文件。本次 R1 获授权有限修订本计划、四份受影响规格及 ADR，并保留文档证据；本次不是代码实施。没有 WP-01 代码、依赖安装、数据库、模型调用、Git 初始化、分支操作、提交、推送或远端资源操作。

WP-01 的建议目标：建立独立 Runtime 的 Python 包、严格 Pydantic v2 契约、版本与摘要原语、Fact Schema Registry、类型化 Rule AST 及纯类型校验接口、可重复生成的 JSON Schema 和对应测试。WP-01 只建设 Domain Model、内存 Fact Registry、Rule AST 静态类型检查、JSON Schema 生成和 CPU-only 测试。所有建议均为 PROPOSED，用户审批状态保持 PENDING_USER_APPROVAL。

非目标：Bundle 编译/打包/验签/完整引用闭包解析（WP-02）、数据库或 ORM、API/OpenAPI 服务、持久调度和节点执行器（WP-03）、Provider/Tool 调用（WP-04）、规则运行时求值和真实巡检（WP-05）、回放执行（WP-06）、Compose 部署与 UI。已有 JIMU 需求平台及 V4 不进入依赖图。WP-01 可以定义后续工作包使用的数据结构，不能据此实现相应服务。

审批循环：Plan → Challenge → 用户 Approval → Execute → Verify → Review → Decide。先由用户批准第 10 节 D01–D10 决策候选和 WP-01 范围，再明确测试/依赖准备授权，才可实施。本次依据用户授权将冲突修订为一致的候选契约，并保留修改前原文；不能把候选契约视为已批准规范。WP-00-R1 到此只提交可审查文档。

## 2. 启动包、环境与仓库证据

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

因此分支、HEAD、dirty/clean 均为不可获取，不能报告为 clean，也不能把父目录的其他工程当作本项目仓库。Git 初始化需要后续明确授权，不能由本轮自动执行。

只读环境探测使用 `python3` 标准库 `sys`、`platform`、`importlib.metadata`：Python 3.12.3，GCC 13.3.0，Linux 6.8.0-149-generic x86_64，glibc 2.39；当前解释器未安装 Pydantic、pytest、jsonschema、Ruff、mypy。该信息不是目标 GPU 主机兼容性结论。未探测驱动/CUDA、未安装依赖、未运行服务。

## 3. 四份规格的契约一致性审查

### 3.1 一致且应保留的架构基础

四份规格共同支持：定义与执行分离，Release/Bundle 固定，六节点确定性控制流，原图坐标与变换留痕，严格数据验证，工具计划预绑定，模型候选仅作建议，PostgreSQL 为持久状态权威。没有发现必须推翻这些边界的直接冲突。技术规格中的 API、ORM、部署建议不反向成为领域依赖。

Domain Model 已具备实现基础：核心概念、主要字段、基数、状态域和不可变性已有说明；尚未达到“可无歧义冻结为模型”的程度。缺少的字段类型、联合分支、摘要身份、操作符语义和策略结构必须先冻结，不能以 `dict[str, Any]` 或任意字符串占位。

### 3.2 C01–C11 逐项处理状态（R1）

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
    enums.py                # 分域枚举、schema/producer/node kind
    primitives.py           # ID、版本、SHA-256、时间、约束数值
    references.py           # ContentRef / IdentitySelector / SchemaRef，运行记录引用
    values.py               # 有判别字段的值与类型代数
    policies.py             # typed policies；禁止任意配置字典
    geometry.py             # 原图 XYXY、尺寸、变换 provenance
    definitions/
      ontology.py           # Entity / Region
      capabilities.py       # Profile / Facet / Question
      tools.py              # Tool / ToolPlan / Step / Mapping
      models.py             # ModelBinding / provider policy
      rules.py              # Rule / RuleSet / applicability
    release.py              # Bundle/Release/Activation 数据，无管理服务
    runtime/
      task.py
      execution.py
      perception.py
      facts.py
      tools.py
      evidence.py
      invocations.py
      decisions.py
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

`DEPENDENCIES.md` 的完整 GPU pinning 门槛与 WP-01 CPU-only lock 需明确适用范围：建议先验证 CPU 契约环境并记录 GPU 项 N/A/deferred，GPU worker 单独环境到 WP-04 再冻结；如果要求所有 GPU 项先过门槛，则 WP-01 锁文件也被阻塞，不能自行忽略该规定。当前 Python 能启动不代表未来 Pydantic 依赖矩阵已验收。

## 5. 待冻结 Pydantic Schema 清单

以下为 WP-01 模型清单，不表示已有实现。Python models 是唯一源；生成的 JSON Schema 不手改。凡独立外部保存/交换的根记录均显式包含 schema_version，嵌套值由所属根的版本覆盖。基类不代替各根的明确版本字面量。

| 模型组 | 待冻结模型 | 核心不变量/边界 |
|---|---|---|
| 基础 | ContractModel、PersistedContract、ArtifactIdentity、ContentRef、IdentitySelector、SchemaRef、RuntimeRecordRef、MediaRef、EvidenceRef、ClockProvenance、TraceContext、ValidationIssue | exact version/digest；错误路径；ContentRef/IdentitySelector 不混用，不把定义引用与运行记录 ID 混用 |
| 枚举 | TaskStatus、ExecutionStatus、NodeRunStatus、AttemptStatus、FactStatus、DecisionResult、RuleTruth、CoverageStatus、DiscoveryOutcome、GroundingOutcome、ProducerKind、ScopeKind、NodeId、ReplayKind，以及显式 release/tool/validation 状态 | 原规格分域枚举保持独立；新增枚举值需审批，不复用字符串的含义 |
| 结构化值 | BoolValue、IntegerValue、NumberValue、StringValue、EnumValue、ListValue、RecordValue 及对应 TypeDefinition | kind 判别；有限类型与约束；无 Any、NaN/Infinity |
| Ontology | EntityDefinition、RegionDefinition、AssetRef | 不是 Object；Region 不等于 polygon |
| Capability | ProfileDefinition、FacetDefinition、QuestionDefinition、PrimaryFactOutput、Applicability、ContextFieldSelector | effective Questions 只由已声明集合冻结；Facet/Question 复用受类型/上下文约束 |
| Tool | ToolDefinition、ToolPlanDefinition、ToolStep、StepDependency、InputBinding、OutputBinding、ConditionalBranch、ResourceRequirements、ToolExecutionPolicy | 已注册 tool；有限 DAG 数据；条件只读合法上下文/Fact；无 N06 依赖 |
| Model | ModelBinding、ProviderPolicy、DeploymentConstraint、FallbackPolicy、InvocationBudget、ContractRequirement | 配置身份与实际身份区分；不含 secret/请求任意配置 |
| Rule | RuleDefinition、RuleSetDefinition、RulePolicy、EvidenceRequirement、ResultMapping、ConflictPolicy、FactSelector | 完整类型 AST；必需/可选适用性明确；无隐式顺序优先级 |
| Bundle/Release | BundleManifest、ArtifactInventoryEntry、SourceProvenance、CompatibilityRequirement、IntegrityEnvelope、ChecksumEntry、AdmissionReceipt、CapabilityReport、ReleaseRecord、ApprovalReceipt、ActivationRecord、ActivationScope | 仅数据契约；根摘要采用 D07 候选字段；技术准入、业务批准、激活分别记录；无编译/验签/激活实现 |
| Intake | TaskSubmission、AcceptedTask、MediaAsset、TaskContext、ContextSnapshot、ContextValueProvenance、ExecutionCoverage、ProfileCoverage、MandatoryRuleCoverage、MandatoryRuleObligation | 一个 RGB 图片；可信元数据来源；UTC/时钟；接收请求与已解析 Release 分离 |
| Execution | ExecutionRecord、NodeRun、Attempt、ExecutionEvent、ExecutionPins、ConfiguredModelPin、ActualDeploymentIdentity | immutable 输入/config 引用；Attempt 单调编号；不存在的 actual identity 不预填；无持久引擎 |
| Perception | ObjectRecord、BBoxXYXY、ImageDimensions、CoordinateSystem、TransformRecord、GroundingRecord、ProfileGroundingResult | 原图坐标、正面积/范围；no_target/failed/partial 分开；Object 仅 execution 内身份 |
| Fact | FactRecord、KnownFact、UnknownFact、ErrorFact、NotApplicableFact、NotEvaluatedFact、FactProducer、FactScope、FactResult | status 判别；scope 判别；类型/来源/证据；invalid value 不伪装成 known |
| Tool runtime | ToolExecution、ToolAttemptResult、ToolError、ToolResourceUsage | 配置 ref 与实际实现版本；输入输出 digest；failure 非负证据 |
| Evidence | EvidenceAsset、EvidenceScope、StorageReference、RetentionPolicyRef、AssetStage | 元数据而非大字节；原图/变换关系；只定义 staged/finalized 状态契约，无上传流程 |
| Model audit | ModelInvocation、InvocationIdentity、InvocationOutcome、InvocationError、UsageRecord | configured binding + 实际 provider/deployment/model；provider request ID；不在记录中写密钥 |
| Decision | CandidatePrediction、CandidateCategory、DecisionRecord、RuleEvaluation、RuleEvaluationOutcome、ConflictResolutionTrace、ExplanationTrace | 全量 applicable/evaluated/skipped/blocked/matched 引用；候选仅 advisory；coverage 不全不能静默 no_violation |
| Replay | ArtifactReplayRequest、InferenceReplayRequest、ReplayRun | kind 判别；source/target pin；只建数据，无模型/工具执行 |
| Node common | NodeInputEnvelope、NodeOutputEnvelope、NodeError、ArtifactOutputRef | task/execution/release/node/attempt/trace pin；错误、时间、输出有界 |
| N01 | ContextBuildInput、ContextBuildOutput、ContextValidationReport | 不补造缺失上下文；required failure 明确 |
| N02 | DiscoveryInput、DiscoveryOutput、ProfileHit、FilteredProfile | 注册 Profile + 固定 Questions + Invocation；额外 BBox/object_id/category 字段拒绝 |
| N03 | GroundingInput、GroundingOutput | 独立按 Profile grounding；输出不允许修改 Questions |
| N04 | ObservationInput、ObservationBatch、QuestionResult、QuestionValidationResult | 一 Object/整批 Attempt；一 Question 一个主 Fact（可为 Record）；显式 parse/关联可信度；批次诊断与合法 Fact 分开 |
| N05 | ToolNodeInput、ToolNodeOutput、ToolPlanRunSummary | 仅绑定计划，引用 N04 validated Facts；无候选驱动选择字段 |
| N06 | DecisionInput、DecisionOutput、MandatoryRuleCoverage | 候选与规则评估分离；完整性报告，缺失候选不改变 mandatory 集合 |
| Registry/AST | FactSchemaDefinition、FactSchemaCatalogue、FactProducerDeclaration、OperatorDescriptor、TypeCheckResult 及第 7 节 AST 节点 | 纯合同和校验；完整 producer closure/DAG admission 属 WP-02 |

冻结时应为每个模型列必填/可选、默认值、长度/数量边界、错误码、样例及 JSON Schema 根名称。当前规格未给出的实体类型、保留策略等业务枚举不虚构为 CONFIRMED；以受约束标识和被引用的版本化词表治理，或在审批中列出有限初始值。

### 类型与版本策略

1. `extra="forbid"` 覆盖所有外部对象，严格标量校验，不能把 `"false"` 转 bool 或把 bool 当 integer。明确 JSON 文本解析与 Python 输入的允许范围，写双入口测试。
2. schema_version、artifact version、Bundle version、runtime/workflow/operator implementation version 分开。建议 SemVer 精确引用，V1 候选使用稳定的 major.minor.patch（非负整数、无前导零），禁止 `latest`、范围和隐式默认版本；兼容范围只出现在明确的 compatibility 对象。
3. SHA-256 建议小写 64 位十六进制，摘要算法单独标识；错误长度、占位符或 mismatch 拒绝。JSON Schema `$id` 与引用身份稳定且版本化，离线解析。
4. 根 schema major 变化或值域/required 收紧视作不兼容；新增字段在 extra=forbid 的旧消费者中也可能不兼容，不能自动称为 minor 安全。未知 required feature/version fail closed。迁移说明必须包含旧记录读取/转换策略，不覆写历史证据。
5. 内部 UTC aware 时间；接受边界保留 capture 原时间、offset/timezone 与时钟来源；拒绝 naive 时间。置信度只作来源元数据，取值范围需能力声明，不宣称校准概率。
6. 发布定义与 Snapshot 使用不可变模型及不可变嵌套集合；Pydantic frozen 不能单独证明深层不可变。运行状态更新生成新值，历史版本不就地改写。
7. scope 为 discriminated union：object 必有 object_id，execution 不得夹带 object_id；Rule 的 FactSelector 同时限定 fact_key、schema ref、scope/producer/主 Fact 及声明的 Record field_path，避免同名 Facts 歧义。
8. V1 不启用通用 extensions。未来需要时单独冻结命名空间、有限值类型、大小上限、兼容策略；不能用开放 JSON 绕过额外字段校验。

## 6. Fact Schema Registry 设计要求

### 6.1 身份与注册内容

建议 immutable key 为 `(schema_id, version, digest)`；相同 ID/version 不同 digest 必须拒绝，不能覆盖。`fact_key` 是用途标识，不替代 Schema 身份。Registry 按 Release 构造不可变快照，不采用进程全局可变单例。

FactSchemaDefinition 应显式规定：schema_version、schema_id/version、payload type definition、scope、known/non-known 策略、单位/坐标系（适用时）、合法 producer kinds、证据与置信度元数据策略、有限集合/字段约束。Schema digest 不能把自己纳入被摘要内容；生成文件的字节摘要与逻辑 Bundle 摘要是不同概念。

建议仅支持 JSON Schema 2020-12 的受限生成子集，由 Pydantic/受限 TypeDefinition 生成；禁止导入任意外部 Schema、remote `$ref`、可执行 validator 或插件代码。输出 `$schema`/`$id`、离线 `$defs`，记录生成器版本；不承诺 Pydantic 自定义跨字段校验能全自动变成 JSON Schema，必须用分支结构表达并测试，无法表达的语义由显式二阶段校验承担。

### 6.2 值与状态

- 建议类型代数：bool、integer、finite number（默认 IEEE-754，有单位可选）、bounded string、closed string enum、bounded homogeneous list、closed record；嵌套深度/数量有限，不支持任意递归、union 自由拼装或 Any。
- 显式 `kind` 判别；枚举包含其 schema 身份；number/integer 不静默互转，不能把 bool 作为 number。几何通过特定 record Schema 表达并附 coordinate system，不把未校验字典作为 payload。
- `known` 必须有合法 value；known false、0、空字符串等按 Schema 合法性保留，不以 truthiness 判断缺失。
- 所有非 known 分支均不允许 value 字段（含 null），并要求结构化 reason；error 附错误分类/validation issue，not_applicable 必有适用性依据，not_evaluated 记录跳过原因。01 §4.4 已同步为此候选，审批仍待用户确认。
- 传输失败/整批 parse 失败只产生 Attempt/batch 错误，不凭空制造每项 Fact。单项非法在关联可信时可产生 status=error 的结果；原始非法响应作为受控诊断证据，不能写入 typed value。
- 多次 Attempt 的 Fact 全部保留，用显式选择的 authoritative attempt/result 引用消费；重复 producer、同 Question 主 Fact 多条无权威选择不能“最后一条胜出”。

### 6.3 接口与验证层次

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
4. Rule condition 和 applicability 根必须 Boolean。区分 Rule 命中条件、适用性及 Tool 条件允许访问的输入，明确禁止 Tool 分支访问 candidate/Decision。
5. 策略必须覆盖 missing（无记录）、unknown、error、not_applicable、not_evaluated；missing 不是额外 FactStatus，不用 false 补齐。
6. Predicate truth 使用 strong Kleene T/F/U；全表见 03 §8.3：not(U)=U，F AND U=F，T AND U=U，T OR U=T，F OR U=U。保留全部输入诊断。另有独立 safety guard：required Fact/证据缺失或无效时，即使 predicate 可简化为 F/T，Rule outcome 仍 blocked、effective result=uncertain；不能称此 guard 为另一张真值表。WP-01 仅定义该语义的数据类型/策略及静态类型检查，不实现真值求值器。
7. 是否允许显式状态检查操作符应单独审批；首版建议不提供，以避免条件绕过 required Fact 政策。
8. RuleEvaluation 分开保存 execution outcome（evaluated/skipped/blocked）、predicate truth（true/false/unknown）与 matched 标记/引用；matched 不是替代 succeeded 的生命周期状态。
9. N06 V1 对所有适用 mandatory Rules 做完整性扫描，候选为空/候选调用失败也不能抑制该集合。保存 applicable/evaluated/skipped/blocked/matched，以及 required Facts/evidence 和 engine version。
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
| G0 授权 | 用户批准范围、D01–D10 的冻结/缩减方案及测试授权 | 阻塞项未解决时不得标记 WP-01 PASS |
| G1 清单完整 | 第 5 节每个根模型到文件/生成 Schema/fixture 的映射 | 无 Any 占位、无遗留未定义策略类型；必要缩减先获批准 |
| G2 严格验证 | unit/contract/negative 的实际 pytest 报告 | 所有必需正例接受，负例拒绝；0 failed，必需测试不得 skip |
| G3 Fact/AST | Registry + AST 类型检查报告 | 状态/value/scope/ref 不变量、全部注册签名、错误类型/未知 op/深度边界均覆盖 |
| G4 可重复 Schema | --check 输出和两次字节比较 | 无差异；Schema 自检和双验证通过；语义层限制有独立测试 |
| G5 依赖边界 | import 检查、Ruff、类型检查、依赖/锁文件 | domain 无禁用依赖，无网络/模型/GPU 副作用；CPU 环境可复建 |
| G6 兼容与安全 | compatibility notes + approved 决策记录 | 摘要身份无循环、版本规则明确、secret/任意代码/无限配置不进入契约；无数据迁移 |
| G7 报告与停止 | WP-01 verification report | code 与 runtime 证据分列；限制明确；完成后停在 gate，不开始 WP-02 |
| G8 Coverage/Identity | CPU-only 契约 fixtures | JEV no_hits、空 Objects、grounding no_target/failed/partial 均不能伪造 complete/no_violation；子集有效命中保留；同目标跨 Profile 独立 Objects/Facts |
| G9 Rule/生命周期策略 | 策略模型、AST 静态检查及纯转移验证 | T/F/U 与 required guard 分离；仅 collect_all/full mandatory scan 策略；非运行 blocked/skipped 无假 Attempt；所有终态无出边；不实现 Rule 求值/worker |

WP-01 PASS 只表示领域/契约基础验收，不表示 Bundle 验签、工作流、真实推理、GPU 容量、业务质量或生产可用。Schema tests 是 code evidence；本轮 `RUNTIME_NOT_RUN`，未来 WP-01 若仅做内存测试仍不能写服务 `RUNTIME_VERIFIED`。

回滚：R1 可按 review_evidence 的 before 副本恢复六个文档，保留证据包；不自动执行回滚或删除。初轮只新增计划的历史事实见 §11。未来 WP-01 在隔离工作目录/获准仓库中实施，保存变更前清单；回退仅撤销本 WP 新增/修改文件，不使用破坏性全目录 reset，不动外部工程、历史证据或已存在用户修改；没有 DB 或 Release 激活需要回滚。

## 10. 风险、挑战与 OPEN 决策

### 10.1 D01–D10 明确决策候选

每项状态均为 **PROPOSED / PENDING_USER_APPROVAL**，没有实现或安装/测试授权。候选与 `ARCHITECTURE_DECISIONS.md` 的 R1 表一致。

| 决策 | 单一明确候选 | 冻结/授权门槛 |
|---|---|---|
| D01 | uv + Python 3.12 候选 + Pydantic v2；独立 CPU-only 有界依赖/lock/matrix，GPU 后续独立 | 用户批准 CPU/GPU 门槛分离；版本未验证，依赖安装仍需授权 |
| D02 | 受限 scalar/list/closed Record；2020-12 离线生成子集；known 有值，非 known 无 value | Registry 与类型契约审批 |
| D03 | 第 7 节 operator catalogue + strong Kleene T/F/U + required completeness guard；仅静态 typechecker | 不实现运行时 Rule evaluator |
| D04 | mandatory 适用性全扫 + collect_all；未知不消除、有效违规不被其他未知擦除 | 全部 trace 与聚合契约审批；运行行为 WP-05 |
| D05 | ContentRef（kind/ID/version/digest）与 IdentitySelector（kind/ID/version）分离；inventory 外部 pin，前向无环 | 回向 selector 不作可执行内容引用，完整闭包 WP-02 |
| D06 | 一 Question 一个主 Fact、Record 复合值；configured 先 pin / actual 追加审计；03 §9 显式状态表 | 无多输出替代方案、无假 Attempt、终态不覆写 |
| D07 | 02 §2/4/5 自包含资源与语义闭包、M/C 排除/封帧根摘要候选 | 算法/验签/打包/closure 实现与 signature trust gate 留 WP-02 |
| D08 | 每 Profile 一个 ContentRef ToolPlan；Question/Facet 复用满足 exact Schema/context/scope；Tool 条件 deterministic | typed policy、allowlist 和资源边界，不开放任意 map |
| D09 | 独立 ExecutionCoverage：complete/partial/none/not_applicable；no_hits/no_target/failure/partial 不推导 no_violation | 01 §4.8、03 §8.4；不把执行覆盖当模型召回率 |
| D10 | Execution 内按 Profile 独立 Object 身份；跨 Profile 同目标不 merge、不共享 Fact、不宣称物理唯一计数 | 后续如关联需新版本独立合同，无 N02 实例对齐 |

基础约束候选：定义 ID 为 `[a-z][a-z0-9_.-]{0,127}`；版本为稳定 major.minor.patch；SHA-256 为小写 64 位 hex；AST 根为深度 1、最大深度 32、节点数 512、逻辑/in 列表最多 64；Fact 容器根为深度 1（Record/list 每嵌套一层加 1），最大深度 8、每 Record 最多 64 字段、list 最多 256 项、string 最多 4096 字符。单个 Schema 可声明更小上限；这些是待审批的安全边界，不是性能测量结论。单位不隐式转换，未识别 required 类型/operator/feature 拒绝。

### 10.1.1 ContentRef / IdentitySelector 与无循环摘要

Profile → Entity/Facet/Question/ToolPlan 为 ContentRef；Question 的 applicable_profile_selectors 与 ToolPlan 的 applicable_profile_selector 为 IdentitySelector。Facet 不反嵌 Question 内容引用。Artifact 字节不含自身 digest 或 Bundle root。Manifest inventory 将所有身份（包括 selector 目标）绑定到唯一 digest；消费仅在 pinned inventory 解析。ContentRef 图 DAG，语义闭包可有 selector 回向关系，后者不是递归哈希边。Schema `$id` 是版本化身份，不嵌自己的 hash。WP-01 仅构建引用类型与注入目录的类型检查，不构建完整 compiler/hasher。

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

## 12. WP-00-R1 修订交付与复审入口（当前）

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
