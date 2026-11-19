# WP-07 本地运行记录

本次验收从已核验的 WP-06 产品新增 57 个文件，变更依赖声明、锁文件及两处依赖边界测试，移除 0 个文件。与 WP-06 相同的 347 个公开领域 Schema 及索引已核对。工程基线、真实源码差异与已封存的 1,018 项内容均可按文件复核。[产品封存](../../implementation_evidence/WP-07/primary_freeze_2a013b38fd05/snapshot.json)

| 运行 | 本地证据 |
|---|---|
| 新图、两种回放与重新启动 | 受信宿主登记新生成的 RGB 图片，授权 Task 走完六个主节点。Artifact Replay 生成评分，派发数量保持原值。Inference Replay 在运行中的 Attempt 遇到进程终止，恢复同一新 Execution，产生 4 个模型回执和 3 个工具回执，源 Task、Decision、trace 和原件仍可重读。[真实 Compose 运行](../../implementation_evidence/WP-07/compose/compose_replay.log) |
| 指标和面板 | PostgreSQL 含两个范围的任务。真实 Prometheus 抓取 target 为 up，授权抓取只返回 1 个任务。Grafana 通过随机回环端口返回 HTTP 200，用预配置 datasource 成功查询 13 条 dashboard 表达式。[正式监控探针](../../implementation_evidence/WP-07/independent/frozen_run_Y3ovKjUF/monitoring.log) |
| 跨实例恢复 | 实际 pg_dump/pg_restore 连同 Bundle SQLite、资源账本和私有文件恢复到全新目标，验证同一逻辑数据库身份。恢复后能读原 Decision、旧 Release、评分和受 hold 保护的证据。已按策略删除的证据返回 expired。对未确认派发执行有范围的取消和对账，新增模型请求、CPU 工具和 dispatch 均为 0。[正式恢复探针](../../implementation_evidence/WP-07/independent/frozen_run_Y3ovKjUF/combined-recovery.log) |
| 外部接口和未来适配 | 实际候选路由支持受理、状态、结果、分页 trace、精确证据、两类回放及版本管理的已定义操作。候选管理动作和宿主边界已向接入方列明。需求制品示例对 77 项来源逐项核对，需求平台无改动。[API 合同](API_CONTRACT.md) · [上游适配设计](REQUIREMENTS_ADAPTER.md) |

真实图像、模型部署和生产控制面的移交条件见 [真实环境验收交接](FIELD_VALIDATION_HANDOFF.md)。正式静态检查、全量测试计数和最终独立结果归档在 [独立验收记录](../../implementation_evidence/WP-07/independent/FROZEN_REVIEW.md)。
