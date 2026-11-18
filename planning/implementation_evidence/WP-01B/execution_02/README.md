# GOAL-002 — dependency-local implementation evidence

This directory records the authorized strategy revision and execution from
`c25bef668136acc651e7313ba608ec6fdefd39a5` on
`dev/wp-01b-definition-fact-registry`. It is a continuation of GOAL-002, not a new
Goal or replacement objective. Full WP-01B acceptance remains the completion condition.

唯一计划：`planning/active/WP-01B/PLAN.md`，字段附录 `CONTRACT_FIELDS.md`。
任务状态与局部门禁：`TASKS.md`、`FINDINGS.md`。D01 A/B仍未得到用户选择。
D02局部value_schema生成、D03 Fact/Authority及D04 Mapping/Review字段已自主技术收口，
经定向Sol Challenge后实施；没有用不完整Producer或自由字典替代未批准条件AST。

当前独立子图包含S1/S2/F1/O1/M1/P1/R1/A1/A2/V1/E1。X1条件Policy、X2完整Producer、
X3完整整体验收依赖D01；完整Rule/Bundle/Workflow/模型调度/数据库/API均未实施。
P1仅14个真实完整非表达式Policy类型，明确命名NonExpressionPolicyArtifact。
Registry和局部Authority报告保留实际未验证的准入/信任/闭包项为deferred。

## Reading evidence

- `baseline.json`：本轮开始204个既存tracked文件摘要、分支及HEAD。
- `before/`：本轮初始活动PLAN/FINDINGS/REVIEW/SHA256SUMS原字节。
- `AUTHORIZATION.md`：本轮调度修订与权限边界。
- `logs/<task>/`：实际命令、UTC时间、cwd、exit、stdout/stderr、JUnit、环境及阶段摘要。
  最终日志不会删除初次失败；旧阶段hash只证明当时快照，不被后续版本冒用。
- `agent_reviews/*_current.json`：实际Sol最终报告与发往Primary的公开任务消息。
  会话model/effort取实际turn_context，见`agent_execution_metadata_current.json`。
- `PRIMARY_S1_REVIEW.json`、`PRIMARY_F1_REVIEW.json`、`PRIMARY_CORE_REVIEW.json`、
  `PRIMARY_V1_REVIEW.json`：Primary直接源码/测试/日志/摘要审查，不仅采用委派文字结论。

本轮CPU环境复用Python3.12.3、uv0.9.15、Pydantic2.12.5，增加限定范围的
RFC8785 0.1.4，用于已授权F1局部canonical input/child digest；未升级原依赖。
锁摘要 `10d7ad828a27a9ea35326ad95f73becedfdc9237bde8fc746a119833191062eb`。
工程Schema pretty JSON的文件摘要不等于RFC8785 child/Bundle digest。
JSON Schema不单独证明严格int/float表示、UTC日历、构造实例taint或跨目录关系。

## Preserved deviations and limitations

1. Sol-B首次S2命令误在原main worktree创建9个新文件，随后仅移动自己文件到批准worktree。
   Primary核对170保护文件字节一致、无新增遗留，main HEAD未变。
   `logs/s2/cwd_correction.json`与`main_protection_after_s2_correction.json`保留事实；
   没有迁移前hash，不补造，也不宣称本轮从未触碰原worktree。
2. E1首次全src/cache只读快照测试遇到V1另一writer修改definitions.py，真实失败保留。
   Primary要求保持原强断言，用依赖排序等writer停止后复验，没有通过缩小断言掩盖失败。
3. 运行工具的Goal存储状态在本轮开始仍为paused；工具无resume API。
   最新用户指令明确撤销全局暂停，本轮按该授权实际推进；没有新建/重置Goal、伪造完成或再次暂停。
4. 所有fixtures为synthetic；ReviewMetadata是调用者注入的评审关联，未认证报告字节或证明真实质量。
   目录ContentRef身份断言不冒充父制品摘要验证。未运行Runtime、Provider或GPU。

模块测试有交叉，不能相加作为全量数。当前整合结果须以`logs/integration/`实际记录为准；
没有该最终记录时不得从上述阶段通过推断整体测试通过。

本轮无Commit/Push/PR/Merge，不修改原架构历史、WP-00/WP-01A审计或现有需求平台。
`RUNTIME_NOT_RUN`；Runtime Acceptance=N/A（纯Domain/有限静态校验）。

## Final checkpoint (dependency-independent scope only)

Final evidence: `logs/integration/final_recheck/`, directly reviewed in
`PRIMARY_INTEGRATION_REVIEW.json`; independent Sol evidence challenge is recorded in
`agent_reviews/g1_a_ontology_current.json`. Actual result: **1455 passed**, zero
failures/errors/skips. All five commands listed in the Primary report exited 0.
The 240-file code/test/fixture/Schema/dependency snapshot is identical before, after
and at final sealing. `final_protection.json` confirms 197 protected developer-tree
files and 170 original main files, including the original nonignored file set.

The final format gate was `ruff format --check src tests scripts` (89 files).
The earlier `ruff format --check .` failed on three sealed audit source snapshots;
that failure and their original bytes are retained. No ignore rules were added and
this checkpoint does not claim whole-tree formatting passed. The first integration
run also overlapped the R1 annotation correction; final_recheck is the stable-byte
acceptance evidence and the earlier run is retained, not substituted.

- Code: PASS_LOCAL_IMPLEMENTED_D01_INDEPENDENT_SCOPE.
- Tests: TEST_PASSED for the implemented scope; 137 generated Schemas plus index.
- Full WP-01B: INCOMPLETE_PENDING_G1_D01_USER_DECISION.
- Runtime: N/A / RUNTIME_NOT_RUN.
- Goal: original objective retained; all unlocked tasks completed; X1/X2/X3 depend
  on the unanswered D01 A/B scope choice. No default selection was made.

## Final manifest boundaries

`changed_files.json` lists every currently changed/new nonignored path against the
current Git HEAD (including these manifests), with status and category. It contains
no self-referential hashes. `changes.patch` contains tracked changes plus new source,
tests, fixtures, generated Schemas, scripts and active planning documents. This
execution evidence directory is excluded from the patch to avoid recursive log and
patch inclusion; its raw files are covered by `after_sha256.json` instead.

`after_sha256.json` hashes every changed/new nonignored file except itself and
`evidence_seal.sha256`. The seal hashes the manifest. SHA-256 provides byte identity,
not a signature or proof of external trust. `baseline.json` and Git HEAD preserve the
before-state; active planning originals are additionally saved under `before/`.
The active planning `SHA256SUMS` is refreshed for current active files, while old
G1/verification documents remain explicitly historical and unchanged.
