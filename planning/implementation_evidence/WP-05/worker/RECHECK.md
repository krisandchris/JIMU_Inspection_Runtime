# WP-05 正式 Worker 接线复核

本地范围包括已准入场景、正式 Intake 及 WorkflowWorker、PostgreSQL 派发和 Attempt 账本、受控文件存储、回环 HTTP 与 CPU 子进程。配置入口为 `jimu_runtime.workflow.worker_inspection.inspection_worker`。调用方配置本地 media store、历史能力读取及现有网关与工具端口。模型请求的 prompt 与 Schema 使用同一个 LocalMediaStore，保留独立的角色、内容摘要和首次持久化时间。

| 审查项 | 实际处置 | 回归 |
|---|---|---|
| N06 RuleSet 排除证明 | 原已选 Decision 的 traces 随发布传递。父子迹线均须匹配 N06 输入中 ContextSnapshot digest。N06 计算与重验证分别核对已持久化 N01 和现有 Object/Attempt。 | Decision contract 谓词测试和正式 Worker 发布。 |
| N04 失败分类和历史重试 | 从真实 Invocation 分类并核对 NodeError 错误类别、诊断及可重试标记。上一轮由相同 NodeRun 的已提交 Attempt 选取，并受原始 Question 集、请求 Schema 与派发 scope 约束。 | `test_wp05_worker_observation.py` 和回环整批失败重试。 |
| 恢复输入错误 | 确定性的历史重建 ValueError 提交在实际 Fenced Attempt 上，数据库异常保留现有 lease 恢复路径。 | 当前 N04 历史校验注入失败，旧 NodeAttempt 无远端派发。 |
| N01 历史 capture | 已完成 N01 后使用确切的历史 RegistryRecordRef 与持久化 MediaAsset，实际字节消费仍验证内容。 | 测试拒绝二次读取当前采集注册表后仍完成六节点。 |
| N03 Evidence 与组间边界 | N06 只接纳所选 Object 及其 GroundingRecord 指向的本 Scope 证据。模型组间缺口从 exact dispatch ID 固定，不假设 no_hits/no_target。 | 混合模型组隔离 PG/HTTP 路径。 |
| N06 证据占用与重验证 | 规则按需读取，CPU 计算与文件 I/O 线程池分开。没有可用内容的读结果固定在当前 Attempt，已验证原件重开后仍须匹配 digest。 | 单线程默认 I/O executor、原图同 ID 改 metadata、首次缺 geometry 等回归。 |
| 请求阶段 lease / orphan | 发布使用统一 DB 实例绑定和文件锁。同 ID 首次注册时间来自 PostgreSQL。终态清理只为无正常输出引用、无真实调用引用且无未解在途发送的记录追加 retire 标记，后续移除文件。 | 真实 lease 过期后同 ID 原件与时间一致。终态清理保留普通和迟到调用引用。 |
| Reader 边界 | 配置消息持久化时包含 `worker_format=configured-v1`。兼容入口认出已选 N01 格式后拒领；注册格式未知的旧 reader 会拒绝消息。legacy 继续写原格式。 | Pinned 执行重开以及兼容入口拒领。 |
| 网关归属及存储独立 | 使用回执自身 dispatch ID 先做归属拦截。响应存储故障保存实际物理回执与独立错误，禁止其成为业务响应。normalized 具有独立 parser digest/size，raw 授权来自实际端点配置。 | `test_wp05_worker_gateway_receipts.py`、真实候选存储失败时 mandatory Rule 继续。 |
| N05 物理请求和祖先 | 工具派发摘要使用明确的物理载荷，logical digest 仍用于恢复。子步骤在数据库事务内验证原祖先选中结果及适配映射。 | PostgreSQL ToolExecution、CPU 响应与重新打开的掩模。 |

隔离通过记录与最终版本全量记录分开。真实外部 Provider、RTX 4090、目标主机和生产数据库为 `RUNTIME_NOT_RUN`。

## 实际验证

隔离运行使用 CPython 3.12.3、Pydantic 2.12.5、SQLAlchemy 2.0.52、psycopg 3.3.5、httpx 0.28.1、Pillow 12.3.0、pytest 8.4.2、Ruff 0.14.14 和 mypy 1.18.2。PostgreSQL 由测试在随机回环端口启动，固定镜像为 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。

- `pytest -q -ra tests/contract/test_wp05_worker_inspection.py tests/contract/test_wp05_media_postgres.py tests/unit/test_wp05_worker_observation.py tests/unit/test_wp05_worker_gateway_receipts.py` 退出 0，32 passed，139.08 秒。该轮完成于预算定向补充之前。 [运行日志](acceptance-runtime.log)
- `pytest -xq --tb=short tests/negative/test_wp03_jobs_scheduling_rejections.py tests/unit/test_wp05_worker_observation.py tests/unit/test_wp05_worker_gateway_receipts.py` 退出 0，29 passed，1.52 秒。
- `pytest -xq --tb=short tests/contract/test_wp05_worker_inspection.py -k observation_exhausted` 退出 0，1 passed，6 deselected，21.48 秒。隔离回环收到两次失败的完整 Joint 请求，第一次进入 Retry，第二次耗尽所选 N04 预算。最终无 N04 authority 和 Facts，Decision 保持 uncertain。
- `pytest -xq --tb=short tests/contract/test_wp05_media_postgres.py -k observation_request_bytes` 退出 0，1 passed，13 deselected，3.53 秒。覆盖真实旧 lease 结束、新 owner 接手、原字节与时间可重开、跨 client 拒绝及终态孤儿回收。
- Ruff、格式、strict mypy、Schema 校验和 `git diff --check` 均有本地结果。 [静态日志](acceptance-static.log)

WP-05 已评审的 N04 解析或关联失败重试使用合格的同一主部署。先前经过 fallback 且返回了已验证的物理响应，随后发生消费者整批失败时，当前不自动重新选路。WP-04 的传输错误 fallback 路径仍按原授权规则执行。
