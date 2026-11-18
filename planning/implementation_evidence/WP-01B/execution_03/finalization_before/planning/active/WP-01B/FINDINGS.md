# WP-01B G1 Findings — Current Register

Revision G1-4-A，执行基线c25bef668136acc651e7313ba608ec6fdefd39a5。
最新调度修订：D01仅局部阻塞；独立技术收口/实现继续。
最新实施授权已生效；旧“未获得任何实施授权”结论仅在before_planning/保留。
历史G1尚未代码实施/安装/测试；本轮结果追加execution_02，不重写历史状态。
G1-D01–04不是历史架构D01–D15编号；后者已批准，不重复审批。

| Finding | 当前状态 | 处置/剩余门禁 |
|---|---|---|
| B-F01 / G1-D01 | USER_SCOPE_RESOLVED_OPTION_A / TECHNICAL_FREEZE_IN_PROGRESS | 用户明确授权共享条件AST数据模型前移B；RuleDefinition/TypeChecker/Evaluator仍禁止；X1–X3按DAG推进 |
| B-F02 / G1-D02 | CLOSED_TECHNICAL / LOCAL_IMPLEMENTATION_REVIEW_PASS | §5冻结；独立JCS向量/父子解耦/limits测试与真实代码审查完成，F1定向127 passed（含20 selector），不等于整包PASS |
| B-F03 / G1-D03 | CLOSED_TECHNICAL / LOCAL_IMPLEMENTATION_REVIEW_PASS | A1/A2已实现验证；完整Producer准入进入已授权X2/X3 |
| B-F04 / G1-D04 | CLOSED_TECHNICAL / LOCAL_IMPLEMENTATION_REVIEW_PASS | S2/M1/P1/V1已实现验证；条件分支引用关联进入X4 |
| B-F05 | IMPLEMENTED_LOCAL_CHECKS / FULL_CLOSURE_DEFERRED | ToolPlan本地DAG和类型映射已测试；RuleSet内容、完整Bundle闭包不属于本子图，不冒称PASS |
| B-F06 | IMPLEMENTED / VERIFIED | import图解析actual submodule/reexport、原纯度限制已实际通过；包含于最终1455 passed |

## B-F01 最小解锁决策

原文证据：
- Domain §14 / line1167：ApplicabilityPolicy.condition_ast必填。
- line1168：QuestionSelectionPolicy.context_rules指向ApplicabilityPolicy。
- line1174：ToolSkipPolicy为typed tool_condition或explicit literal true。
- §13.1 / line1021：Producer.activation_condition为always或typed Tool condition。
- §13.3 / lines1108–1117规定表达式输入域，但没有另一个AST-free简单条件语言。
- A源码只有基础值/引用，无现成AST；最新Goal明确禁止B实现Rule AST。

不存在可同时满足“全部既有条件能力”和“完全无AST”的封闭模型。
仅列16个kind、14个模型、未解析ForwardRef、任意dict/Any、opaque string、把AST改名、
删除必填condition或默认为true，均不关闭此问题。

已通过异步用户问题提交两个具体选项：
1. 推荐：明确授权既有共享条件AST的数据模型前移B，不包含RuleDefinition/TypeChecker/Evaluator。
   这会有限改变本轮禁AST范围，不能由Primary自行批准；批准后补模型/文件/测试并定向Challenge。
2. 保持完全无AST：明确批准B条件分支支持域及验收范围缩减，后移C。
   不能把缩减交付写成原完整目标PASS；不另发明简单表达式语言。

用户尚未答复，不把推荐或等待时间当批准。其他实施授权保持有效，无需逐文件再批。
最新用户明确修订为依赖驱动的局部阻塞。D01的A/B两方案继续待用户决策，
仅PLAN X1/X2/X3阻塞；S1/S2/F1/O1/M1/P1/R1/A1按自身前置完成后继续。
D03/D04技术字段由Primary自主收口，不等待D01，不新建/重置Goal，不把局部交付当整体完成。

## 技术Challenge处置原则

PLAN-2定向复查原文保存于g1_reviews/。以下原技术待办现已在CONTRACT_FIELDS §1–9逐字段关闭，保留追溯清单：
1. 列FactSchemaDefinition全部公共字段、单位/坐标、证据/置信metadata约束；不只给模型名。
2. 给SchemaDescriptor精确discriminator、raw/typed分支字段与ToolResult envelope描述；
   TypeDefinition不能单靠raw标签证明DTO shape。
3. 给InputBinding五个来源分支的准确wire、Schema/owner/path基准与target obligation；
   保持optional source + explicit block，不误称mandatory producer完整可用。
4. 给ReviewMetadata/TransitionAssessment准确identity/scope/verdict/例外与fallback分支；
   不能发明可信审批主体或真实评测通过。
5. 给metadata namespace entry/Record根/tag semantic_domain与Prompt partial变量scope等细节。

这些技术缺口由Primary完成技术冻结；实现和验证缺陷另列下表，不能把字段冻结当代码验收。
已补入PLAN：低层共享类型位置；所有scoped ref目录冲突；N04外部selection集合锚点；
可信arrival资格；Tool required/cardinality/output state/emitted与absence关系；raw bytes与模型绑定；
duplicate keys在字典化前拒绝；有限历史检查边界；JCS大整数溢出/独立边界用例。

- JCS只处理生成input/child；A stable_json及payload数值语义保持不变。
- 准入父制品真实性与本地child再生成分别报告；不能用ContentRef格式通过冒充hash校验。
- System输出用已有EXECUTION_EVENT的具体闭合descriptor，非通用event或新kind；空输出
  有明确zero_or_one和absence原因，不能合成Fact或负证据。
- System scope按已声明scope；不从“execution Facts只能来自system”倒推出“system不能object”。
- Optional Question一旦进入effective batch，仍恰一个primary；optional Tool absence不同nonknown Fact。
- Optional source可以映射required target，但on_missing=block必须显式；schema路径不存在仍拒绝。
- 本地DAG检测是新Goal明示B工作，不再援引旧PLAN后移它。
- 原始Challenge不改写。Primary不因Sol完成而验收，后续必须直接读代码/diff/logs与实际测试。

生产分类成员、预算、评测门槛、部署值仍由配置审批，synthetic fixtures不代表生产批准；
它们不构成本轮类型工程的重复审批要求。

## execution_02 实施审查登记（当前，取代上文历史阶段状态）

| 编号 | Finding | 当前处置 |
|---|---|---|
| I01 | OptionalContract重验证丢弃注入extra | S1修复，9回归；133 targeted passed，Primary已直接复核 |
| I02 | S2初次9个新文件误写原main worktree | Sol仅移动自己文件，170保护文件byte-identical无新增；原始失败及纠正证据保留，不能声称从未写入 |
| I03 | descriptor diagnostic prefix上限/Region owner异digest冲突 | S2定向整改与独立复核完成；最新88 targeted passed |
| I04 | JSON UTC嵌套模式/数字字符串被当epoch | 共用UTC lexical+strictdatetime修复，300 targeted passed（重叠S1/A1）；Primary读实际source/tests |
| I05 | ToolPlan required输出被强制全映射、遗漏完整plan_ref | 依批准声明子集语义修复，required只约束mapped输出；禁止伪造步骤 |
| I06 | P1 Prompt重复目录顺序依赖、unknown窄Review遮蔽已知覆盖 | 先18失败后140 passed，独立Sol代码/日志/hash复核关闭 |
| I07 | M1 unused/空Plan context绕过owner/step检查 | 先8失败后43 passed，独立Sol probes/hash复核关闭 |
| I08 | M1缺失FactSchema被一律报告invalid | Primary提出；typed report修复后50 targeted passed，独立复核关闭，保留code/path/message/refs |
| I09 | V1模型Prompt缺Question owner、嵌套catalogue、缺validation_at | 独立Challenge采纳，§9冻结；V1定向测试/审查中，未标整包PASS |
| I10 | P1 Prompt/Model/Vocabulary/Review异hash被误当缺失 | Primary提出，6失败复现；156 targeted passed及独立code/probe/hash复核关闭 |
| I11 | V1独立response pin、source digest先检及诊断overflow保留语义 | Primary提出并整改；V1最终76 targeted passed，已实际读最终代码/回归 |
| I12 | Question/partial/孤立Prompt响应Schema漏解析 | 独立Sol复现；逐Prompt exact descriptor解析修复，6负向回归及独立hash/日志复核关闭 |
| I13 | 全mypy诊断path annotation跨模块不一致 | R1改共享IssuePath，51 targeted及全量40源码mypy通过；最终1455 passed，旧R1seal保留 |
| I14 | 全目录format命中3份封存audit脚本 | 原失败保留，历史字节不改；维护代码格式范围src/tests/scripts，Ruff lint仍全目录，不能写全目录format PASS |
| I15 | E1/首轮整合测试期间其他writer变更导致快照不稳定 | 保留并发污染与首轮source-change记录；串行静止窗口E1最终35 passed，整合最终1455 passed且240文件无差异，不将旧日志充当最终字节证明 |

以上都是局部技术整改，不改变D01业务范围选择、不批准任何AST。模块测试计数存在交集，
不得相加冒充全量测试数。最终完整已实现A+B回归1455 passed/0 skipped；137份Schema索引验证通过，240测试文件摘要稳定。
当前未解决范围问题仅G1-D01，X1/X2/X3尚未实施；完整WP验收仍未通过。


## Current superseding decision — option A approved

用户已明确选择：A：授权共享条件 AST 数据模型前移 WP-01B；仍不实现 RuleDefinition、
TypeChecker 或 Evaluator。上述“用户尚未答复”“BLOCKED_D01”只描述旧阶段，不再是当前门禁。
保留A/B历史，不将A授权扩张为运行器或语义TypeChecker授权。X1精确字段冻结和Sol Challenge
完成后实施，X2/X3/X4按DAG继续。execution_02封存证据不改写；新增证据写execution_03。
I09/I11/I12的V1整改已由最终76targeted及1455整合验证覆盖；历史进行中描述由此明确关闭。


### X-C01 — same-ToolPlan conditional Fact digest cycle

Sol-B and independent Sol-A confirmed ToolPlan→skip_policy→exact Tool Fact selector→
same ToolPlan. Existing normative ContentRef/Digest rules offered no wire-preserving
solution. The user explicitly approved the local-selector amendment (execution_03
AUTHORIZATION.md); status USER_SCOPE_RESOLVED / IMPLEMENTATION_PENDING. Domain §13.4,
Bundle note and ADR now record the narrow change; CONTRACT_FIELDS §11.2 freezes fields.
No Question-only restriction, IdentitySelector substitution or hash-field omission.
Acceptance: local selector has no reversePlan pin, each use binds exact current Plan/
strict ancestor/mapped output/Scope, original exactselector wire unchanged; full tests
and independent review required before closing implementation.


### execution_03 implementation review log

| Finding | Actual issue | Current disposition |
|---|---|---|
| X-I01 | AST all/any prewalk copied/expanded an already over64 argument array before bounded validation | Before2 failures retained; length rejected before expansion; final269 targeted passed, Primary+independentSol read same source hash; CLOSED |
| X-I02 | Producer activation use context allowed Tool/Question execution scope despite known object-only rule | A added strict model+Schema constraint and targeted tests; finalX4 review pending |
| X-I03 | Successful finite condition binding report lacked an explicit completed reference check alongside future-type/truth deferred | A now emits condition.references with actual result and separately deferred boundary; finalX4 review pending |

X0 source/diff/test/hash review is PRIMARY_X0_REVIEW.json. X1 final269/0skip and own
mypy/Ruff/format rawlogs+12hashes were independently reviewed by Primary and Sol;
PRIMARY_X1_REVIEW.json records local PASS, without promoting full-src or wholeWP acceptance.


### X-I04 — Context registration owner uniqueness (OPEN_REMEDIATION)

Independent Sol reproduced a C2 in X4: distinct Schema registrations with the same
Context source/exact owner escaped full-body duplicate detection, even unused by a
constant condition. Primary confirmed CONTRACT_FIELDS §2.2 requires source/owner
uniqueness; no new business decision is needed. A owns a narrow fix and local/constant
negative regressions. Failed bindings must expose no selector projection. Final same-byte
tests and independent re-review are required to close; other DAG tasks continue.


X4 final disposition: X-I02/X-I03/X-I04 CLOSED after Primary actual code/delta/tests/logs/hash
review and independent Sol remediation re-probes. Final module snapshot:138 passed, zero
failures/errors/skips; owned static checks passed. Whole-source mypy at that point had
two C-owned in-progress errors, retained; no full-project static PASS claimed.
See execution_03/PRIMARY_X4_REVIEW.json and logs/x4/review_remediation_01/.


### X-I05 — Schema exporter preparation call arity

Primary direct diff review caught three accidental extra module arguments in new
`_group` inventory calls before generation. Sol preserved the faulty preparation
snapshot/diff, corrected only those argument lists, and statically inspected all55
calls. Status FIXED_PENDING_E2_TESTS; syntax/AST inspection is not a functional PASS.

X2 complete data/checker local review: PASS,117 targeted/0skip and owned static checks
verified against ten product hashes by Primary and independent Sol. Full WP remains
pending X3/E2/final regression; no dynamic activation or consumer-ready authority claim.


### X-I06 — Integrated Result links and optional-source completeness (IN_REVIEW)

Primary reviewed the first X3 body before its test handoff. Unselected zero-Fact
N04/N05 Results must retain their valid failed/late history while still proving static
Attempt/NodeRun/Object/Profile and Tool step identities; selected-only A2 checks cannot
cover these associations. Also optional_source=all must not permit an omitted optional
Question merely because mode=mandatory_plus_optional. D is checking these existing
contracts and adding positive/negative regressions; no evaluator or status coercion is
authorized. This is a development-review observation, not a claim of an executed failure.


### X-I07 — Reciprocal emitted-output associations (IN_REVIEW)

Primary direct review found candidate→Result checks alone could not prove present
emitted refs in every unselected Result. N04/N05/System Results must also resolve each
provided Fact and match Execution/Object/Profile/Attempt/source Result/output/status.
Missing candidates are incomplete; contradictory existing candidates invalid. Failed or
late unselected histories may remain partial; only selected results require full output
sets and authority eligibility. D implemented the reciprocal checks; separate Sol-A
review regression files are assigned, with final same-byte tests/review still required.


X-I06/X-I07 CLOSED: final X3 owned78/0skip plus independent63/0skip, Primary full
checker/test/delta/log review and independent Sol finalhash/probes agree. The independent
63-case run verified60 dependency hashes unchanged. Missing/borrowed refs and zero-Fact
static owners reject correctly without discarding valid unselected failed/late partials.
X3 final code hash4179223f05cc4a226936a6c83ff832919a119b5754495bc3715c34856c4349af.
The intermediate77passed/1failed XML was retained by rename as
logs/x3/composition_prerelease_failure.xml; final XML78 is separate.


### X-N01 — Supplemental exporter-only mypy (C3 / NON_BLOCKING_TOOLING_LIMITATION)

An extra command `uv run --locked --no-sync mypy scripts/generate_schemas.py`
actually failed with7 diagnostics: missing jsonschema stubs, installed package imports
without a py.typed marker, and the existing bootstrap return annotation. Raw output is
execution_03/logs/e2/initial_mypy.json. It is NOT a passed check. The approved PLAN and
pyproject mypy gate targets `src/jimu_runtime`; that scope is unchanged and must pass.
The exporter is covered by direct code review, Ruff/format and41 actual CLI/Schema tests.
No new ignore/stub/dependency, whole-stack upgrade or reduced Domain gate was added.
Full typing of this additional engineering-script invocation remains unverified; this is
not an unresolved Domain/Producer/Authority C0/C1/C2 and does not authorize later WP work.


X-I05 CLOSED after final41 exporter/Schema tests, owned Ruff/format, approved-source
mypy, actual deterministic generation and read-only --check; Primary and independent
Sol verified166 product hashes and162 indexed Schema documents. The preparation
arity failure remains as original snapshot/diff. X-N01 remains explicitly nonblocking
and unverified for that supplemental script-only typing command.
