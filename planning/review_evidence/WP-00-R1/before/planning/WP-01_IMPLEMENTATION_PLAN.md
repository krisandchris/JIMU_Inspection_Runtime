# WP-01 Implementation Plan — Domain Model and Contract Foundation

状态：PROPOSED，等待用户审批；本文件不构成实施授权。  
审查日期：2026-11-17（Asia/Shanghai）。工作包：WP-00 Bootstrap Review。  
证据等级：`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。

## 1. 目标、范围与审批边界

本轮获授权的工作是阅读、初始化审查和编写本计划。唯一项目内变更为本文件。没有 WP-01 代码、依赖安装、数据库、模型调用、Git 初始化、分支操作、提交、推送或远端资源操作。

WP-01 的建议目标：建立独立 Runtime 的 Python 包、严格 Pydantic v2 契约、版本与摘要原语、Fact Schema Registry、类型化 Rule AST 及纯类型校验接口、可重复生成的 JSON Schema 和对应测试。所有建议均为 PROPOSED，不能把本计划中的建议视为已经解决 OPEN 决策。

非目标：Bundle 编译/打包/验签/完整引用闭包解析（WP-02）、数据库或 ORM、API/OpenAPI 服务、持久调度和节点执行器（WP-03）、Provider/Tool 调用（WP-04）、规则运行时求值和真实巡检（WP-05）、回放执行（WP-06）、Compose 部署与 UI。已有 JIMU 需求平台及 V4 不进入依赖图。WP-01 可以定义后续工作包使用的数据结构，不能据此实现相应服务。

审批循环：Plan → Challenge → 用户 Approval → Execute → Verify → Review → Decide。先解决第 10 节的 WP-01 阻塞项并明确测试/依赖安装授权，再开始实施。存在冲突的规格保持原样并在本文件报告；不为绕过冲突自行选定规范。WP-00 到此只提交可审查方案。

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

清单列出的 14 个文件均存在，字节数及 SHA-256 全部吻合；清单本身为第 15 个文件。没有 Python 实现、测试目录、`pyproject.toml` 或锁文件。

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

### 3.2 冲突、歧义及缺口登记

以下引用均指项目内文件章节；“建议”不修改原规格的规范地位。

| 编号 | 来源与问题 | 分类/影响 | 建议与冻结门槛 |
|---|---|---|---|
| C01 | 02 §3–4：根摘要计算包括 manifest payload，仅明确排除 signature，但 manifest 中仍有 `integrity.root_digest`；checksums 是否包含 manifest/自身也未定义 | 算法自引用风险；阻塞 Bundle 身份契约 | 明确 payload 文件集合；建议 checksums 排除自身和 manifest，根输入排除 root_digest 与签名；定义带域标识的确定性输入格式及固定测试向量。未批准前不实现摘要/签名算法 |
| C02 | 01 §3.3/3.7/7：Profile 精确 pin ToolPlan，ToolPlan 又要求 profile_ref；Question 的 applicable_profile_refs 也可能回指 Profile；精确引用包含摘要 | 相互内容寻址可能循环，无法直接计算摘要 | 冻结“所有权引用”和“适用性身份选择器”的区别；建议仅前向依赖含完整 digest，回向适用性用版本化身份，Bundle inventory 为这些身份绑定 digest。必须显式批准此对引用规则的解释，测试无循环生成；不能默默删 digest |
| C03 | 01 §3.5 Question 有多个 output_fact_schema_refs；03 §6 QuestionResult 使用单个 fact_key/schema_ref | 结果基数不明确，阻塞 N04/Fact 模型 | 建议一 QuestionResult 含多个按输出槽标识的 FactResult；按 Question 独立验证，明确槽内失败是否影响其他槽；替代方案为 V1 强制每 Question 仅一个输出。二选一审批 |
| C04 | 01 §4.2 要 Execution pin 实际模型身份；03 §6 实际身份只有调用后才知道 | 时间约束歧义，可能要求创建时写入不存在的值 | 配置绑定/部署约束创建时 pin；实际身份由不可变 Invocation 关联，Execution 保留审计关系；禁止预填或覆写成“最新模型” |
| C05 | 01 §6、03 §9 的 NodeRun 线性图将 blocked/skipped 放在 running 后；03 §3/7 要依赖失败直接阻塞后继 | 非完整转移表，容易伪造运行/Attempt | 冻结显式合法边，允许尚未运行的 pending/ready → blocked/skipped；不生成虚假运行记录。持久化并发防护留 WP-03 |
| C06 | 01 §2 将 Release 列于定义上下文；AGENTS 将其列于运行时 | 分类差异，非字段硬冲突 | Release 是发布桥梁：不可变引用定义闭包，执行时固定它；单独模块，不允许 Fact 反写定义 |
| C07 | 01 §4.4 允许 typed union 或按 Fact JSON Schema 校验；02 §9 要声明 dialect；ADR-021 尚为 PROPOSED | Fact 数据类型与扩展方式未冻结 | 采用第 6 节受限类型代数生成 Schema，拒绝任意动态 Schema/网络 `$ref`；审批 D02 |
| C08 | 01 §3.8、03 §8 要 conflict/result policy，但未定义多命中、mandatory、未知与错误完整语义 | Rule/Decision 模型不完整 | 冻结结构化 Policy、每 Rule 结果及聚合关系；默认全扫 mandatory，未知不强制为 false；见 D03/D04 |
| C09 | 02 §2 列出 facts/objects/decisions Schema，无 context、工具输入输出、prompt template 的明确承载位置；01/03 要引用它们 | “self-contained”闭包缺口 | WP-01 定义可区分的引用种类和契约；WP-02 冻结完整目录及 prompt/context/tool schema 资源位置，不允许运行时查需求平台补齐 |
| C10 | 03 §4 no-hit 非最终 no_violation，但 §8 主要讨论 Object；空 Object/部分 grounding 的执行级结果未定义 | 空集 vacuous truth 可造成误报无违规 | 定义 coverage 与失败原因，不用空匹配直接生成 no_violation；WP-01 保留显式 coverage/result 结构，聚合政策需审批 |
| C11 | 01/02 要严格、版本化持久契约，03 通用节点信封未列 schema_version | 字段列表非完整模式，不能直接照抄 | 依 AGENTS 所有外部持久根记录加 schema_version，嵌套纯值由根版本治理；不把模型默认配置当成版本字段 |

这些问题已在规划阶段报告；停止相应实现决策，不擅自修订四份规格。解决方案需要用户在 WP-01 审批中确认，涉及 WP-02 的部分在后续工作包再次冻结。

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
    references.py           # ArtifactRef、SchemaRef、资源/运行记录引用
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
| 基础 | ContractModel、PersistedContract、ArtifactIdentity、ArtifactRef、SchemaRef、RuntimeRecordRef、MediaRef、EvidenceRef、ClockProvenance、TraceContext、ValidationIssue | exact version/digest；错误路径；不能把定义引用与运行记录 ID 混用 |
| 枚举 | TaskStatus、ExecutionStatus、NodeRunStatus、AttemptStatus、FactStatus、DecisionResult、ProducerKind、ScopeKind、NodeId、ReplayKind，以及显式 release/tool/validation 状态 | 原规格分域枚举保持独立；新增枚举值需审批，不复用字符串的含义 |
| 结构化值 | BoolValue、IntegerValue、NumberValue、StringValue、EnumValue、ListValue、RecordValue 及对应 TypeDefinition | kind 判别；有限类型与约束；无 Any、NaN/Infinity |
| Ontology | EntityDefinition、RegionDefinition、AssetRef | 不是 Object；Region 不等于 polygon |
| Capability | ProfileDefinition、FacetDefinition、QuestionDefinition、QuestionOutputSlot、Applicability、ContextFieldSelector | effective Questions 只由已声明集合冻结；Facet/Question 复用受类型/上下文约束 |
| Tool | ToolDefinition、ToolPlanDefinition、ToolStep、StepDependency、InputBinding、OutputBinding、ConditionalBranch、ResourceRequirements、ToolExecutionPolicy | 已注册 tool；有限 DAG 数据；条件只读合法上下文/Fact；无 N06 依赖 |
| Model | ModelBinding、ProviderPolicy、DeploymentConstraint、FallbackPolicy、InvocationBudget、ContractRequirement | 配置身份与实际身份区分；不含 secret/请求任意配置 |
| Rule | RuleDefinition、RuleSetDefinition、RulePolicy、EvidenceRequirement、ResultMapping、ConflictPolicy、FactSelector | 完整类型 AST；必需/可选适用性明确；无隐式顺序优先级 |
| Bundle/Release | BundleManifest、ArtifactInventoryEntry、SourceProvenance、CompatibilityRequirement、IntegrityEnvelope、ChecksumEntry、AdmissionReceipt、CapabilityReport、ReleaseRecord、ApprovalReceipt、ActivationRecord、ActivationScope | 仅契约；根摘要 C01 未决；技术准入、业务批准、激活分别记录；无编译/验签/激活实现 |
| Intake | TaskSubmission、AcceptedTask、MediaAsset、TaskContext、ContextSnapshot、ContextValueProvenance、CoverageReport | 一个 RGB 图片；可信元数据来源；UTC/时钟；接收请求与已解析 Release 分离 |
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
| N04 | ObservationInput、ObservationBatch、QuestionResult、QuestionValidationResult | 一 Object/整批 Attempt；多输出 C03 待决；显式 parse/关联可信度；批次诊断与合法 Fact 分开 |
| N05 | ToolNodeInput、ToolNodeOutput、ToolPlanRunSummary | 仅绑定计划，引用 N04 validated Facts；无候选驱动选择字段 |
| N06 | DecisionInput、DecisionOutput、MandatoryRuleCoverage | 候选与规则评估分离；完整性报告，缺失候选不改变 mandatory 集合 |
| Registry/AST | FactSchemaDefinition、FactSchemaCatalogue、FactProducerDeclaration、OperatorDescriptor、TypeCheckResult 及第 7 节 AST 节点 | 纯合同和校验；完整 producer closure/DAG admission 属 WP-02 |

冻结时应为每个模型列必填/可选、默认值、长度/数量边界、错误码、样例及 JSON Schema 根名称。当前规格未给出的实体类型、保留策略等业务枚举不虚构为 CONFIRMED；以受约束标识和被引用的版本化词表治理，或在审批中列出有限初始值。

### 类型与版本策略

1. `extra="forbid"` 覆盖所有外部对象，严格标量校验，不能把 `"false"` 转 bool 或把 bool 当 integer。明确 JSON 文本解析与 Python 输入的允许范围，写双入口测试。
2. schema_version、artifact version、Bundle version、runtime/workflow/operator implementation version 分开。建议 SemVer 精确引用，禁止 `latest`、范围和隐式默认版本；兼容范围只出现在明确的 compatibility 对象。
3. SHA-256 建议小写 64 位十六进制，摘要算法单独标识；错误长度、占位符或 mismatch 拒绝。JSON Schema `$id` 与引用身份稳定且版本化，离线解析。
4. 根 schema major 变化或值域/required 收紧视作不兼容；新增字段在 extra=forbid 的旧消费者中也可能不兼容，不能自动称为 minor 安全。未知 required feature/version fail closed。迁移说明必须包含旧记录读取/转换策略，不覆写历史证据。
5. 内部 UTC aware 时间；接受边界保留 capture 原时间、offset/timezone 与时钟来源；拒绝 naive 时间。置信度只作来源元数据，取值范围需能力声明，不宣称校准概率。
6. 发布定义与 Snapshot 使用不可变模型及不可变嵌套集合；Pydantic frozen 不能单独证明深层不可变。运行状态更新生成新值，历史版本不就地改写。
7. scope 为 discriminated union：object 必有 object_id，execution 不得夹带 object_id；Rule 的 FactSelector 同时限定 fact_key、schema ref、scope/producer/输出槽，避免同名 Facts 歧义。
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
- 建议所有非 known 分支均不允许 value 字段，并要求结构化 reason；error 附错误分类/validation issue，not_applicable 必有适用性依据，not_evaluated 记录跳过原因。与 01 §4.4 的可选 null/value policy 相比，这是需批准的 V1 收窄。
- 传输失败/整批 parse 失败只产生 Attempt/batch 错误，不凭空制造每项 Fact。单项非法在关联可信时可产生 status=error 的结果；原始非法响应作为受控诊断证据，不能写入 typed value。
- 多次 Attempt 的 Fact 全部保留，用显式选择的 authoritative attempt/result 引用消费；重复 producer、同槽多条无权威选择不能“最后一条胜出”。

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
2. 解析每个 Fact/context 引用到 exact Schema，检查 scope、producer/输出槽是否唯一；验证 AST 所引用 Fact 与 required/optional dependency declarations 一致。
3. 递归推导类型，检查常量、enum membership、单位与比较操作，拒绝 incompatible types。可解析缺 producer 的本地类型测试不能宣称完整 Bundle closure 通过。
4. Rule condition 和 applicability 根必须 Boolean。区分 Rule 命中条件、适用性及 Tool 条件允许访问的输入，明确禁止 Tool 分支访问 candidate/Decision。
5. 策略必须覆盖 missing（无记录）、unknown、error、not_applicable、not_evaluated；missing 不是额外 FactStatus，不用 false 补齐。
6. 建议首版严格传播：任一声明 required 输入非 known，则 Rule blocked 并映射 uncertain；仅所有必需输入 known 且证据齐全时产生确定 true/false。逻辑 all/any 不以短路吞掉 required 的 unknown/error。可选依赖须单独显式策略；不得映射缺失/error 为确定 no_violation。
7. 是否允许显式状态检查操作符应单独审批；首版建议不提供，以避免条件绕过 required Fact 政策。
8. RuleEvaluation 分开保存 execution outcome（evaluated/skipped/blocked）、predicate result（true/false/indeterminate）与 matched 标记/引用；matched 不是替代 succeeded 的生命周期状态。
9. N06 V1 对所有适用 mandatory Rules 做完整性扫描，候选为空/候选调用失败也不能抑制该集合。保存 applicable/evaluated/skipped/blocked/matched，以及 required Facts/evidence 和 engine version。
10. RuleSet 的同时命中策略、优先级与执行级 coverage 聚合由明确 policy 冻结；不按存储顺序、置信度或 LLM 结果自行决策。缺少必要事实/证据的 Rule 不给出确定 no_violation。

## 8. 测试计划、边界与执行授权

以下全部是待批准的 WP-01 测试；本轮没有执行 pytest 或 Schema 功能测试。测试使用确定性 CPU fixtures，无网络、真实图像上传、模型、工具执行或外部数据库。

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
| contract/test_nodes.py | 每个节点输入输出 fixture | N02 BBox/Object/category；N03 修改 Questions；N04 槽重复/未声明 Question；N05 候选依赖字段；N06 trace 缺必需项 |
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
| 本轮 WP-00 | planning/WP-01_IMPLEMENTATION_PLAN.md | 仅新增本计划；其他原始 15 个文件保持原字节 |
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
| G0 授权 | 用户批准范围、D01–D08 的冻结/缩减方案及测试授权 | 阻塞项未解决时不得标记 WP-01 PASS |
| G1 清单完整 | 第 5 节每个根模型到文件/生成 Schema/fixture 的映射 | 无 Any 占位、无遗留未定义策略类型；必要缩减先获批准 |
| G2 严格验证 | unit/contract/negative 的实际 pytest 报告 | 所有必需正例接受，负例拒绝；0 failed，必需测试不得 skip |
| G3 Fact/AST | Registry + AST 类型检查报告 | 状态/value/scope/ref 不变量、全部注册签名、错误类型/未知 op/深度边界均覆盖 |
| G4 可重复 Schema | --check 输出和两次字节比较 | 无差异；Schema 自检和双验证通过；语义层限制有独立测试 |
| G5 依赖边界 | import 检查、Ruff、类型检查、依赖/锁文件 | domain 无禁用依赖，无网络/模型/GPU 副作用；CPU 环境可复建 |
| G6 兼容与安全 | compatibility notes + approved 决策记录 | 摘要身份无循环、版本规则明确、secret/任意代码/无限配置不进入契约；无数据迁移 |
| G7 报告与停止 | WP-01 verification report | code 与 runtime 证据分列；限制明确；完成后停在 gate，不开始 WP-02 |

WP-01 PASS 只表示领域/契约基础验收，不表示 Bundle 验签、工作流、真实推理、GPU 容量、业务质量或生产可用。Schema tests 是 code evidence；本轮 `RUNTIME_NOT_RUN`，未来 WP-01 若仅做内存测试仍不能写服务 `RUNTIME_VERIFIED`。

回滚：本轮只需删除新增计划即可回到初始字节快照，不自动执行删除。未来 WP-01 在隔离工作目录/获准仓库中实施，保存变更前清单；回退仅撤销本 WP 新增/修改文件，不使用破坏性全目录 reset，不动外部工程、历史证据或已存在用户修改；没有 DB 或 Release 激活需要回滚。

## 10. 风险、挑战与 OPEN 决策

### 10.1 WP-01 审批前必须处理

以下是建议决议，不把既有 OPEN 自动关闭；选择不同方案须同步调整影响文件和测试。

| 决策 | 对应现有 ADR/缺口 | 建议 | 阻塞范围 |
|---|---|---|---|
| D01 | ADR-018；依赖兼容 policy | uv + CPU-only Python 3.12 候选；批准独立 CPU lock 与 GPU 后续矩阵；授权有界依赖准备和相关测试 | 包骨架、锁、验证可执行性 |
| D02 | ADR-021；01 §8 | 受限类型代数 + Registry + typed discriminated payload；2020-12 子集；非 known 无 value | Fact/Question/Tool/Rule 类型及 Schema |
| D03 | ADR-012 | 冻结第 7 节 operator catalogue、strict propagation、AST 预算及诊断 | AST/interface/typechecker 和负例 |
| D04 | 01 §8、ADR-011 | V1 全扫 mandatory；多违规建议保留每 Rule 命中并明确 aggregate policy；空/部分覆盖不默认为 no_violation | RuleSet/Decision/coverage 枚举及策略 |
| D05 | C02 | 冻结 identity selector 与 content dependency ref 的区别及外部 inventory pinning 规则；不可违反精确引用原则 | Profile/Question/ToolPlan/ArtifactRef 摘要可构造性 |
| D06 | C03/C04/C05 | 选择 Question 多输出形状；配置预先 pin/actual 事后审计；明确未运行节点 blocked/skipped 转移 | N04、Execution 和 lifecycle 契约 |
| D07 | C01；ADR-009 | 根摘要非自引用的输入集合/字段排除和 canonicalization 标识；若首版冻结 IntegrityEnvelope，先冻结字段布局 | BundleManifest/IntegrityEnvelope 的冻结；签名算法实现仍 WP-02 |
| D08 | 01 §8、ADR-023/024；策略字段无类型 | V1 每 Profile pin 一个 ToolPlan；Question/Facet 复用需 exact Schema/context 兼容；typed applicability/context schemas/producer selectors；冻结结构化最小 policies | Definition、ContextSnapshot、Rule/Tool inputs 的可验证类型 |

审批还应确认测试预算的具体数值、基础 ID/版本语法及各闭合枚举集合；不能在实现时以无限制默认值填充。Git 仓库创建是交付溯源的独立待授权事项，不影响本轮规划，也不自动授权分支/远端/提交。

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

## 11. 本轮验证证据与独立自审

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
