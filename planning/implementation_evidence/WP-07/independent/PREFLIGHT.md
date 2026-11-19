# WP-07 独立验收预检

当前证据对应集成候选的本地副本，包含 1018 个产品文件。候选清单 SHA-256 为 `d0614f207b9ec736fb9ee9e806159c8e3eaee282b90d76d5753ae81670e5f7d9`，ZIP SHA-256 为 `c362b667ec999e80eafee91343ef49e06965a0f77a25a950da104aa5b1ba0f96`。正式封存的验收结果单独记录。

- 分离解包逐文件摘要核对通过，可信历史 fixture 单独复制并复核了 SEAL、原件和副本。uv 0.9.15 使用 CPython 3.12.3，按锁离线安装 39 个含开发依赖的包。Docker Engine 29.9.3、Compose 5.7.0 和固定的 Python、PostgreSQL、Prometheus、Grafana 镜像均可本地访问。证据见 [静态运行日志](frozen_run_kwDZiLPB) 和 [运行环境日志](frozen_run_Cw9Hzb9z)。
- 候选 Ruff lint 通过，唯一格式问题位于 `src/jimu_runtime/api/boundary.py`，已通知集成方。调正 runner 的命名空间设置后，`mypy --explicit-package-bases --strict` 核查 230 个源文件通过。Schema 生成检查和两次重导出通过，对照可信 WP-06 清单的 347 份公共 Schema 及索引没有新增、删除或字节差异。证据见 [格式检查](frozen_run_kwDZiLPB/ruff-format.log) 和 [Schema 对照](frozen_run_kwDZiLPB/schema-347.log)。
- 真实本地 Compose 入口执行 `JIMU_RUN_COMPOSE_TEST=1 python -m pytest -q -ra --tb=short tests/contract/test_wp07_compose_local.py`，2 passed in 101.57s，退出 0。覆盖真实签名场景的 API、持久 trace 和 Evidence，使用专属临时项目。证据见 [Compose 结果](preflight_compose_Ezb7QNAj/compose.log) 和 [JUnit](preflight_compose_Ezb7QNAj/compose.xml)。
- 监控首轮发现 runner 的解包权限让 Prometheus 容器读不到绑定配置。明确规范临时私有目录内产品文件和目录权限后，同一候选的独立监控退出 0。PostgreSQL 16.14 以只读账号读取授权范围，匿名 metrics 和 readiness 都返回 401。宿主随机回环 Grafana health 返回 200，Prometheus 目标为 up，只得到授权 Task 1 条，库中共有 2 条。Grafana 执行 provisioned dashboard 的 13 个表达式，第一面板为 1，探针确认容器、卷与网络均已清理。证据见 [监控 smoke](frozen_run_Cw9Hzb9z/monitoring.log)。
- 组合恢复的当前候选以安装 wheel 的独立进程运行，记录旧 Release 的 artifact/inference replay、物理调用计数和未确认派发时 SIGKILL。现有探针在校验伪造媒体时仍调用 3 参数版本的 `verify_media`，候选版本要求第四个 `bundle_db` 参数。运行退出 1，组合恢复验收为 `UNVERIFIED`。已报告给 Recovery 签字方。证据见 [组合探针日志](frozen_run_Cw9Hzb9z/combined-recovery.log)。
- 隔离 PostgreSQL/API 和原生备份恢复契约，对 `tests/contract/test_wp07_api_*.py tests/contract/test_wp07_recovery.py` 执行定向 pytest 得到 10 passed in 52.97s，退出 0。证据见 [实际命令与结果](preflight_api_pg_UWp9m7LL/api_pg.log) 和 [JUnit](preflight_api_pg_UWp9m7LL/api_pg.xml)。
- 运行器实际拒绝摘要错误的 manifest、被替换内容的成员、额外成员和 ZIP symlink。正常组解包成功且核对了 0644/0755 权限。证据见 [运行器输入验证](harness-invariants.log)。
- Recovery 负责人按新签名传入实际备份的 Bundle SQLite。修订探针 SHA-256 为 `115f456fce57182264c7ff4db50923e7ca67bbb9b5511cebf7447935146219aa`，以原候选重新运行后退出 0，包含物理恢复和重新打开证据，以及恢复后零额外派发。正式包同版本探针亦独立通过。见 [候选修订重跑](frozen_run_hYPofIUm/combined-recovery.log) 和 [正式探针](frozen_run_Y3ovKjUF/combined-recovery.log)。
