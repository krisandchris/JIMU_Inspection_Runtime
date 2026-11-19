# WP-07 正式本地验收

**Code PASS · Test PASS · 本地隔离 Runtime VERIFIED。** 开发工作树以 `2552affeb69fbc2594fe70724f895b0db0967d47` 为 Git 基线。上游为 WP-06 的 961 项产品封存。本次新增 57 项，变更 4 项，移除 0 项。1,018 个文件的最终清单 SHA-256 为 `2a013b38fd05125a54b1b8126932a75f2b20849d8d148efbb0b2b3793ce53d68`，ZIP 为 `a458149665893542f29b71e48e9d4d4fbf6c9025f231a86c557a4bc59c14d1c9`。[产品差异](../../implementation_evidence/WP-07/primary_freeze_2a013b38fd05/snapshot.json) · [封存包](../../implementation_evidence/WP-07/primary_freeze_2a013b38fd05/product.zip)

主控分别复核同一封存上的静态、运行及完整 pytest 证据。最终全量 **3,988 项通过，0 失败、0 错误、0 跳过**，包含 WP-07 的 29 项及 24 项场景示例。JUnit SHA-256 为 `ce20189e77152a1a9cc3a4234d229fee551a8033360a237ffea8431568241d91`。Ruff、497 个文件的格式校验、230 个文件的 strict mypy 及 347 份上游公开 Schema 和索引兼容检查通过。[全量 JUnit](../../implementation_evidence/WP-07/independent/frozen_run_N2X0sklL/pytest.xml) · [独立验收及实际命令](../../implementation_evidence/WP-07/independent/FROZEN_REVIEW.md)

首次全量使用运行器的 umask 077，权限负例中申请的 0755 目录因此成为 0700，3,987 项通过，1 项失败。用同一封存分别在 077 和 022 下对照复现后，只为 pytest 子进程设置 022，重新执行全部 3,988 项。运行器外层仍使用 077。保留两次独立 JUnit 和原始日志。[权限对照](../../implementation_evidence/WP-07/independent/pytest_umask_reproduction.log)

- **Compose 与 API。** 通过有范围的受理、取消、结果、trace、证据、回放和管理权限校验。在真实 Docker Compose 内从受信宿主登记新合成 RGB，执行六个节点，进程中断和重启后仍可读原结果与证据。[运行记录](EXECUTION_STATUS.md) · [API 合同与候选边界](API_CONTRACT.md)
- **备份与恢复。** 在两个隔离 PostgreSQL 实例间实际恢复 Bundle、资源账本、媒体和任务历史。原判定、回放评分和 hold 可重开，合法处置过的证据返回 expired；对未确认派发进行取消对账，新增 HTTP、CPU 和派发数均为 0。[恢复日志](../../implementation_evidence/WP-07/independent/frozen_run_Y3ovKjUF/combined-recovery.log)
- **监控。** 真实 Prometheus 抓取正常，授权范围只返回数据库中两个范围的一个任务。Grafana 回环健康响应 200，13 条仪表盘查询通过。[监控日志](../../implementation_evidence/WP-07/independent/frozen_run_Y3ovKjUF/monitoring.log)
- **交付边界。** 本地使用隔离身份、合成 Provider 与 CPU 工具。未改需求平台；未来适配以受审的上游导出和独立 Runtime 身份交接。真实 Provider、目标 RTX 4090、实拍误放与漏检、生产恢复、上线与用户业务验收分别待相应授权及实测。[上游适配](REQUIREMENTS_ADAPTER.md) · [真实环境交接](FIELD_VALIDATION_HANDOFF.md)

本轮未对 main、Git 发布或生产环境执行变更。本地验证到此封存。
