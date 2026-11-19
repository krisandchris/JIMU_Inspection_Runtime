# WP-06 封存独立验收

验收覆盖旧执行与数据库升级、Artifact Replay、Inference Replay、历史导出与独立评分、证据留存及中断恢复。产品基线是 WP-05 正式封存 `124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad`。活动树 Git HEAD 为 `2552affeb69fbc2594fe70724f895b0db0967d47`。WP-06 封存 961 个文件，其中 91 个场景文件。与 WP-05 相比新增 31 个、修改 11 个、删除 0 个文件。 [产品变更](frozen_run_EJT43OE3/product-delta.log) [WP-05 独立验收](../../WP-05/independent/FROZEN_REVIEW.md)

- WP-06 清单 SHA-256 `bfaf2ccec1408421609f7ce2c9a9dbe15987ba27b57ae75f4bf99ea167a154ed`，ZIP SHA-256 `548fba7da8003e5f6bf7441830f0bcfadbcf30b9c422e20c7eb970ab1e9ee2e9`。 [封存验收前核对](frozen_run_EJT43OE3/verify-before.log)
- Linux x86_64、CPython 3.12.3、uv 0.9.15 和离线锁定环境。由两个封存 ZIP 独立构建、安装非 editable wheel。WP-06 wheel SHA-256 `325a779ff747599ea87ed20d7c9deb3b74e4a0cdb28ba2cdb9067bed355741de`，WP-05 wheel SHA-256 `405da1f31fa2c9efeebd485ee5e6a77ceb0e93e6ffe820712c82a907d91dbb6e`。 [环境](frozen_run_EJT43OE3/versions.log) [WP-06 Wheel](frozen_run_EJT43OE3/wheel-sha.log) [WP-05 Wheel](frozen_run_EJT43OE3/legacy-wheel-sha.log)

```bash
bash planning/implementation_evidence/WP-06/independent/run_acceptance.sh \
  planning/implementation_evidence/WP-06/primary_freeze_bfaf2ccec140/product_sha256.json \
  bfaf2ccec1408421609f7ce2c9a9dbe15987ba27b57ae75f4bf99ea167a154ed --all
```

## 实际运行

| 条件 | 独立证据 |
|---|---|
| WP-05 历史升级 | 旧 wheel 在新建隔离 PostgreSQL 应用 001–008，经实际六节点生成已终态 Task、Decision 和媒体证据。WP-06 追加 009–011，重复执行未应用迁移。旧 Task、Execution、事件、能力、各 Attempt、权威、工具和派发及媒体的 20 类记录行摘要前后一致。 |
| Artifact Replay | 两个独立 key 产生两个分数。同 key 顺序及并发重发都返回同一记录。独立进程 strace 和回环观测到零工具启动和零 HTTP，PG 执行、job 和派发计数保持一致。两条分数均可按 EvidenceRef/digest 重读，错 digest 和错误 Scope 被拒绝。 |
| Inference Replay | 已激活不同当前 Release 后，并发受理仅新建一个 Execution 和初始 job。原 Task、旧 Bundle、旧 Context 仍固定。新增执行实际完成六节点，发生四次回环 POST、三个 CPU 进程，三份具名 sidecar 对账通过。新旧 Attempt 集不相交，源历史保持一致。 |
| 独立评测 | 正式 HistoricalEvaluationExporter 两次导出同一结果，并对账七条历史派发及八个受限响应摘要引用。持久预测、签名 Bundle 配置和预先固定的合成真值给出 IoU 1。三个 Question、几何关系、Rule 与三个工具均核对。已标注违规有一条缺自动放行观测，错误放行率为 null，自动放行风险 0/0 为 null。 |
| 证据处置 | 从原 Release 解析重签的一秒留存测试策略。review hold 留住文件。释放后在真实 unlink 与完成提交之间 SIGKILL，重开时为 pending，并发恢复同一 absent_on_recovery。原 Evidence 元数据可查，过期证据阻挡重新导出，已存在的 Artifact 分数仍可读。 |

以上物理操作只使用回环 Provider 和隔离 PostgreSQL。实际数据、授权和评分边界详见运行日志与生成的合成证据。 [实际运行](frozen_run_EJT43OE3/replay-runtime.log) [历史导出](frozen_run_EJT43OE3/independent-quality.historical-export.json) [独立真值](truth.json) [重算报告](frozen_run_EJT43OE3/independent-quality.score.json) [处置记录](frozen_run_EJT43OE3/evidence-disposition.json)

## 静态与回归

产品 Ruff、455 个文件格式、199 个文件 strict mypy 和 `git diff --check` 均通过。347 份 Schema 与索引重导出一致，并与 WP-04 和 WP-05 逐字节比较无变化。WP-01B Schema 契约的历史测试输入经旧 seal 锚定，复制于临时解包路径。 [Ruff](frozen_run_EJT43OE3/ruff.log) [格式](frozen_run_EJT43OE3/ruff-format.log) [mypy](frozen_run_EJT43OE3/mypy.log) [Schema](frozen_run_EJT43OE3/schema-wp05.log) [历史测试输入](frozen_run_EJT43OE3/history-before.log)

首次正式执行完成静态产品检查和全部运行探针，但独立探针三份脚本触发 Ruff I001。首轮在 pytest 早段结束，实际退出 143，保留日志。使用同一冻结解包环境无缓存修正独立验收器导入排序后，第二轮从原封存重启，独立代码也通过 Ruff。 [首轮诊断](frozen_run_DL2k6qrS/ruff-independent.log) [首轮运行](frozen_run_DL2k6qrS/replay-runtime.log) [第二轮独立 Ruff](frozen_run_EJT43OE3/ruff-independent.log)

WP-06 专项 `python -m pytest` 通过 79 项，0 失败、0 错误、0 跳过，耗时 209.40 秒。 [专项 JUnit](frozen_run_EJT43OE3/wp06.xml) [命令与退出](frozen_run_EJT43OE3/wp06-contracts.log)

完整 `python -m pytest` 通过 3,959 项，包含 24 项可编译场景测试，0 失败、0 错误、0 跳过，耗时 1,488.58 秒。第二轮整个 `--all` 于 2026-11-18 11:18:57 UTC 退出 0。再次核对 961 个产品文件、WP-06 ZIP、WP-05 历史 ZIP、独立输入及 WP-01B Schema 测试清单，摘要一致。 [全量 JUnit](frozen_run_EJT43OE3/pytest.xml) [全量命令与退出](frozen_run_EJT43OE3/pytest.log) [验收后产品校验](frozen_run_EJT43OE3/verify-after.log) [独立输入后验](frozen_run_EJT43OE3/probe-inputs-after.log) [历史 ZIP 核对](frozen_run_EJT43OE3/legacy-archive-after.log) [历史 Schema fixture 后验](frozen_run_EJT43OE3/history-after.log)

## 验收结论

**本地独立验收 PASS，RUNTIME_VERIFIED。** 数据库升级和证据处置在临时本地环境中执行。外部 Provider、生产数据处置、目标 GPU、代表性现场数据、业务评分阈值、部署和用户验收均为 UNVERIFIED。
