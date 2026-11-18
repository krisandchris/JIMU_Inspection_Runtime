# WP-01B 当前独立审查结论

**PASS**：Primary已直接读取实际核心实现、源码/测试Diff、Schema、依赖锁及原始执行证据；
各项整改逐一复核，不只依赖子智能体总结。Sol完成源码定向审查与最终独立证据Challenge。
最终1907 passed /0fail/error/skip；规定静态/Schema门禁通过，305测试文件稳定。

执行门禁：Code/Test/Independent/Overall Engineering PASS；User Acceptance pending；
Runtime N/A / RUNTIME_NOT_RUN。X-N01额外工具命令失败及历史format限制仍披露。
当前完整结论见FINAL_ACCEPTANCE_REPORT.md与execution_03/PRIMARY_FINAL_REVIEW.json。
以下为保留的审查过程，旧NOT_IMPLEMENTED/CONDITIONAL/等待状态不再是当前结论。

---

# WP-01B — Primary Review history and current execution

Current: PLAN-3-DAG implementation is active; see TASKS.md and execution_02 section below.
The following G1/PLAN-2 passages are retained historical states, not the current test/code state.

基线main=51a10416d882fe85e5a1cfc5440eac6498fc48f4。
当前为G1契约收口审查，不是最终代码验收。
STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN。

## 实际独立核查

Primary重新读取用户Goal原文、AGENTS、实际PLAN/FINDINGS/REVIEW与摘要封套、
相关四规格、A基础代码/接口及CPU配置，直接对照必填condition、projection、producer、
authority与Mapping条款；没有仅接受Sol结论。

初始170个文件（143 tracked+27已知untracked）逐字节与工作前manifest一致，
PLAN-1.1的15个SHA256SUMS条目匹配。main仍为指定HEAD，无tracked/staged改动。
新建已获授权的独立worktree/分支，按manifest复制已知规划材料；原main不写入。
新开发分支HEAD仍为基线，没有Commit/Push。

原生四新Sol只读Challenge全部完成。实际会话turn_context核对：
Primary=gpt-6-astra/xhigh；Sol-A/B/C/D=gpt-6.1-sol/medium。
共享filesystem由实际路径读取确认；worktree隔离由Git worktree创建实际结果确认，
不再把仅接口存在写成创建验证。未改Codex配置/安装框架。

## 首轮Challenge

原文在 implementation_evidence/WP-01B/g1_reviews/。
A=Ontology/Capability，B=Model/Tool/Policy，C=Fact/Producer/Authority，D=tests。
四者均指出D01条件表达支持域不能由“完整16种kind”代替。
Primary直接核对Domain:1021、1167、1168、1174与最新禁止AST条款，接受其为真实分期冲突。
已提交最小二选一，未擅自批准范围例外。

其他意见按新授权技术细化：
- D02局部JCS/child digest范围已明确，不重复要授权。
- D03具体三种result kind、wholebatch/step、最小Fact/Attempt描述、system optional absence；
  D定向复查接受具体闭合EXECUTION_EVENT descriptor，并明确不是通用event兜底。
- D04 strict有限SchemaDescriptor与raw/typed表示、optional祖先、source-target兼容和missing策略。
- D指出旧PLAN把ToolPlan cycle后移与新G3冲突；PLAN-2改为B本地DAG检测。
- 不接受子报告中“SystemProducer仅execution”无充分依据的进一步收窄；
  保持声明scope，execution Fact仅system与N05 object-only原约束。

PLAN-2另交B/C/D定向只读Challenge，原文继续追加保存；
整改和最终摘要写入后再记录最终核查，不以草稿审查冒充最终文件通过。

## 当前验收轴

| 轴 | 状态 |
|---|---|
| G1 Contract Review | CONDITIONAL：D01用户范围裁定未取得 |
| Code Acceptance | NOT_IMPLEMENTED |
| Test Acceptance | TEST_NOT_RUN；324passed仅A历史 |
| Runtime Acceptance | N/A，本包纯Domain；RUNTIME_NOT_RUN |
| Independent Review | 已完成首轮静态Challenge；不是代码审查PASS |
| Overall WP-01B | INCOMPLETE，不能标PASS/Goal complete |
| 实施授权 | 已授权B；具体AST范围例外仍待用户 |

已执行的操作是文件/Git只读核验、原生工程子任务、授权worktree/branch创建、文档复制和修订、
RFC资料查阅及审计哈希。没有运行pytest/Ruff/mypy/项目Schema验证、安装依赖、编写B代码或调用Runtime。
首次审计命令python不存在（exit127），改python3成功；一次apply_patch因同path多操作被拒绝，
未改文件；随后一次会话文本提取未匹配（exit1）也未写文件，修正后写入PLAN。
这些是文档工具操作结果，不是功能测试失败或通过。

待用户给D01答案后，按既有整体实施授权继续冻结相关数据模型及定向Challenge，然后交Sol实现；
不需要用户重复批准每个文件。未答复按Goal的条件暂停指令保存证据并停止。

## PLAN-2 定向Challenge的实际结论

B/C/D均直接读取worktree内PLAN-2，原回复追加保存，未改写此前报告。
Primary已对照具体条款修订：

| 发现 | 处置 | 状态 |
|---|---|---|
| 共享纯类型放validation层可导致反向依赖 | 改schema_types.py低层，validation仅目录校验 | RESOLVED_IN_PLAN，代码未实施 |
| N04自带effective集合可能自证完整 | 加QuestionSelectionDescriptor作为独立pinned锚点 | RESOLVED_IN_PLAN |
| 只用succeeded不能辨别late | 显式arrival_qualification；late拒选、unresolved/missing不通过 | RESOLVED_IN_PLAN，可信输入非实际时序证明 |
| Tool optional absence与nonknown混淆 | emitted/absence descriptor、required/cardinality绑定、五态保持 | RESOLVED_IN_PLAN |
| bytes未绑定模型/重复键覆盖 | 解析前拒重复键、非法数/Unicode；完整typed content绑定 | RESOLVED_IN_PLAN |
| runtime refs构dict覆盖；历史输入漏项 | 所有scoped refs先查唯一内容；明确有限目录信任边界 | RESOLVED_IN_PLAN |
| JCS数值溢出及安全整数≠binary64全集 | 路径化错误与具体边界测试补齐 | RESOLVED_IN_PLAN |
| SchemaDescriptor/source union/Review scope尚无完整wire | 不把要求描述当字段冻结，保留B-F04 C2 | OPEN_TECHNICAL，不需新增业务审批 |
| FactSchema metadata字段/Prompt/metadata细节 | 保留B-F03/04 C2，后续主控技术补齐后再委派代码 | OPEN_TECHNICAL |

D定向承认并撤回system execution-only额外限制，原文保留。
当前最终PLAN并非无条件静态PASS；上述已处置项由Primary直接核对，未声称Sol对最终
新字节又全量复核通过。D01待用户范围裁定；其余C2由Primary负责，不转嫁用户。
尚无B代码、测试、Schema生成、依赖安装、Commit/Push；原A和WP-00历史保持原样。

## PLAN-3 dependency-local execution revision

User explicitly revoked global blocking; D01 A/B remain unchosen. Prior G1 whole-Goal
pause recommendation is superseded, not erased from its committed history.
Primary reviewed exact S1 contract fields and current A interfaces; Sol-D targeted only
new S1/PLAN§8. S1 is D01-independent. A concrete test-basename collision was fixed
by naming negative/test_b_ontology_invalid.py, preserving pytest configuration.
Optional-null rejection must agree between generated Schema/Pydantic; duplicate refs,
SchemaRef dialect, UTC/safe paths, deep immutability and helper inventory are explicit.
S1 field contract is frozen and may be delegated to Sol-A under existing authorization.
This is module plan approval by the authorized orchestrator, not whole WP acceptance.
F1 and S2 field closure continues independently; no AST/partial PolicyArtifact is approved.
Current actual execution evidence is appended under execution_02/, not old G1 files.

F1 targeted Sol-D Challenge accepted D01-independence and identified two normalization
bypass risks. Primary added raw-root normalization before A defaults and independent
GenerationInput revalidation; full root/nested/model-copy cases are mandatory. Exact child
four-part identity and safe-int bounds on both Integer/Number definitions remain explicit.
F1 field contract is frozen; waits only for S1 shared interface/environment handoff, not D01.
RFC8785 0.1.4 bounded dependency is technically approved under existing CPU dependency
authorization, with independent vectors still required. Sol-D owns the corresponding exact
import/dependency allowlist enhancement; no test assertions are removed to hide failures.

## execution_02 current review (supersedes historical NOT_IMPLEMENTED/global pause)

Latest user instruction permits frozen D01-independent implementation without another approval.
Fields §1–9 have been closed by Primary and targeted Sol Challenges. A2/V1 remain active;
D01 A/B is still unanswered, only X1/X2/X3 are blocked. Stored Goal pause cannot be resumed
via available API; no new Goal/reset/false completion was performed.

Primary directly inspected shared strict/UTC implementation and tests, FactSchema/JCS,
Registry, A1 data, ontology/Prompt, M1 source and Policy resolver code. It found and delegated
actual extra-dropping, output-subset, plan identity and missing-vs-invalid issues, rather
than accepting agent prose as acceptance. Per-module conclusions remain evidence scoped.
S1/F1 local reviews are recorded as JSON; other modules await final combined evidence closure.
Independent Sol reviews verified P1 Prompt/review fixes and M1 all-context fixes by direct
code+tests+logs+hashes+read-only probes. A1 independent review found no C0–C2, explicitly
excluded still-changing A2 additions and did not extend old hashes to new bytes.

One workdir deviation is retained: Sol-B's first S2 write placed9 new files in original main
worktree then moved only its own files to approved worktree. Primary verified170 protected
files identical and no additions afterward. This is a corrected deviation, not a never-written
claim. Exact initial wrong-workdir/failure and correction logs remain execution_02.

Latest P1 tests140 and M1 tests43 passed; sharedUTC300, O1132, R153, A1135 have actual raw
records. Counts overlap and are not summed. Full regression/Schema inventory remains pending.
Runtime Acceptance=N/A; RUNTIME_NOT_RUN. Overall WP-01B remains INCOMPLETE, not PASS.

## Final implemented-subgraph review

All ready D01-independent tasks completed. Primary directly reviewed current code and
new Registry shared IssuePath fix; independent Sol code/probe review closed V1 response
Schema omission, M1/P1 identity classifications and A2/E1 cases. Final T1 raw commands,
JUnit and hashes were directly inspected:1455 passed,0 failures/errors/skips; Ruff lint.,
mypy40files, maintained format89files and Schema check exit0. All240 tested artifacts
have stable before/after/current hashes. See PRIMARY_INTEGRATION_REVIEW.json.

Whole-tree format first-run failure on3 immutable audit scripts remains disclosed; no
ignore/config bypass or historical rewriting. First-run integration snapshot changed
while an authorized Registry annotation remediation ran; that envelope is not used as
final-byte proof. A new complete stable final run supplies current evidence.

Code/Test PASS applies only to implemented independent subgraph. Full Goal/whole WP
remains INCOMPLETE/CONDITIONAL pending D01; X1/X2/X3 are unimplemented. This is not
implicit optionB approval. No further ready independent code work remains. Runtime=N/A,
RUNTIME_NOT_RUN. No Goal reset/new Goal/false complete or Git publication occurred.


## execution_03 — authorized option A continuation / X1 gate

User restored GOAL-002 active and explicitly approved G1-D01 option A. Primary read
actual Goal source, current PLAN/FINDINGS/tasks, repository HEAD and source interfaces;
verified execution_02's748 hashed files/current changed set and protected main unchanged.
No prior partial PASS was promoted to final acceptance.

Sol-B/C/D performed narrow new-interface Challenges. Sol-B and independent Sol-A
confirmed the same-ToolPlan policy digest cycle. User explicitly approved the bounded
LocalToolFactSelector amendment; Domain/Bundle/ADR updates preserve original exact refs.
Actual user replies and before bytes are in execution_03. No inferred approval.

Primary adopted §11 strict recursive data and full16 Policy shapes; Sol-B's targeted
Challenge found no implementation blocker, with ancestor-set cycle detection and
preorder occurrence-preserving traversal accepted. Primary reviewed the actual approved
AST catalogue, literal/Reference boundaries and TypeChecker non-goals. X1 is now assigned
to Sol-B with exclusive file scope and targeted tests. This is the design gate, not a code
or whole-WP PASS. §12/X2 and §13/X3/X4 proceed only after exact interface handoffs.


X2/X3 targeted Challenge accepted after explicit technical closure: CatalogueContext
wrapper/owner, System output-slot uniqueness, exact activation matching table, actual
version via result descriptors, cross-directory merge without precedence, pins for
empty output groups, and eligibility distinct from historical validity. Primary read
runtime Fact source/System result fields and existing A2 association flow before
accepting these notes (§12.1/13.1). No new business decision or evaluation was inferred.


X4 Challenge accepted: finite ConditionReferenceContext/result + helper signature is
frozen in §13.2. It reuses current actual registries and never imports ProducerCatalogue;
C owns unique declaration matching. No selector kind substitutes for source/schema/path
proof. Rule operands/truth and external runtime provenance remain separate. Primary
split independent X0 ref extraction to C while B implements X1; only after real interface
handoffs do dependent A/C validators run. All remaining user scope gates are resolved.


## execution_03 completed component review (before final full regression)

Primary directly read X0 ref extraction, X1 complete conditions/Policy source, X2 full
Producer/catalogue/Fact checker, X4 reference helpers/Definition deltas, X3 integrated
Authority checker and independently authored regressions, E2 exporter/test deltas and
representative generated Schema. Raw logs, finalJUnit and exact product hashes were
checked separately from Sol summaries. PRIMARY_*_REVIEW.json records concrete scopes.
Independent Sol reviews cover X1/X2/X3/X4/E2; X-I01 throughX-I07 implementation findings
are closed. All histories remain separate from current final acceptance.

Component results: X1 269, X2 117, X4 138, X3 78, independentX3 63, E2 41 tests; counts
overlap and are not summed. Full final integration uses one frozen product snapshot.
Supplemental script-only mypy7errors are retained; the unchanged approved gate is
src/jimu_runtime. Historical whole-tree format3audit failures are also preserved.
No evaluator, real runtime authority, Release/Bundle admission or external trust was
certified by these CPU checks. Final whole-WP result remains pending actual regression.
