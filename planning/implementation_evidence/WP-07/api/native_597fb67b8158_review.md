补丁确实调用了宿主认证和动作 ACL，不能据此断言宿主允许越权；但幂等重提返回实时任务视图、Bundle 操作键未区分 scope，以及证据读取晚于资源分配才检查预算，均可从 diff 直接定位。此次为 `STATIC_REVIEW_ONLY`：仅审查所给 diff，本地基线未能独立核验；`TEST_NOT_RUN`，未调用外部服务。

Full review comments:

- [P1] 不要让幂等重提绕过任务读取授权 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/inference_routes.py:87-88
  当主体保留 `TASK_SUBMIT`、但没有或已失去某个 Task 的 `TASK_READ` 权限时，重提原先已受理的请求仍会调用 `records.primary()` 并返回最新 `TaskView`。这与受读取 ACL 保护的 GET 返回相同内容，包括当前执行状态和 `cancel_requested_at`，而不只是原始受理回执。应返回稳定的受理回执，或在披露实时视图前校验该 Task 的读取权限。

- [P2] 按已授权 scope 隔离 Bundle admission 幂等键 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/admin_routes.py:87-87
  当一个 `Administration` 服务允许两个 project/client scope 提交 Bundle 时，ACL 区分这两个 scope，但写入同一个 `admin.bundles` 的 `operation_id` 都是客户端原样提供的键。测试也仅用 `store.get_admission(operation)` 定位记录。因此两个独立客户端使用同一键时不能各自拥有一条独立操作记录；无需假定底层会返回旧回执，键冲突本身就破坏了隔离。应将可信 scope 纳入持久化操作键，或按 scope 选择独立 store。

- [P2] 在披露审计材料状态前校验 digest — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/evidence_routes.py:95-98
  对于 raw/normalized 请求，`read_audit()` 没有收到所请求的 digest。即使 digest 不匹配，已过期材料也会先返回 410，超限材料先返回 413；只有正常大小的可用材料才会走到 digest 不匹配的 404。这使有该材料读取权限的调用方仅凭 dispatch ID 和错误 digest 就能区分材料状态。应使用持久化 digest 核对精确定位信息，再决定是否返回该材料的状态；不能只依赖对可用正文重新计算哈希。

- [P2] 将 evidence 字节预算落实到实际读取端 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/evidence_routes.py:105-106
  `EvidencePort` 必须先交付完整的 `bytes`，路由随后才检查 `evidence_bytes`。当证据大于 HTTP 配额时，413 限制了发送量，却没有限制读取或驻留内存的字节数；asset 和 audit 两条路径均如此。应把预算传给 reader，在可信长度检查后执行有界读取，并在读取过程中维持上限。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 在缓冲 ZIP 前限制并发 admission 数量 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/admin_routes.py:78-84
  `max_archive_bytes` 只限制单个 ZIP。并发上传时，每个请求都可以先持有完整 `raw`，再等待 `admission_environment()` 或线程池中的 `admit_bundle()`；线程数有限并不限制排队中的归档总量。应在读取正文前取得有界 admission 配额，容量不足时实施背压，并在后台操作实际结束后释放配额。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 在解析和查重前限制 query string — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/boundary.py:78-81
  `SafeRoute` 在认证依赖运行前解析全部查询参数，并对每个不同的键再次调用 `getlist()`；即使该端点不使用查询参数，也要承担这部分工作。正文的字节预算不覆盖 URL，这里也没有查询串长度或字段数上限。应先限制原始 query string 的字节数，再单次遍历参数对完成计数和查重。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 为请求正文读取设置截止时间 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/boundary.py:210-214
  `read_body()` 只在下一个 chunk 到达时检查字节数。如果客户端上传部分 JSON 或 ZIP 后保持连接而不结束请求，协程和已缓冲数据可以一直被占用，即使没有超过字节上限。应为整个正文读取设置可配置的截止时间，并在超时后释放缓冲和请求配额。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 增量计算 trace 页大小，避免重复序列化整页 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/records.py:140-140
  每加入一个事件，`RuntimeRecords.trace()` 都在事件循环上同步序列化从第一页首到当前事件的全部内容；路由返回时还会再序列化一次。对于包含许多事件的页，`trace_bytes` 限制了最终响应，却没有避免按页前缀反复编码的 CPU 和内存复制开销。应让每个事件只编码一次，累计其字节数，并计入页头、分隔符和游标。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 区分事务不变量失败与暂时性数据库故障 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/api/boundary.py:113-115
  捕获整个 `SQLAlchemyError` 会把永久性错误也报告为可重试的 `503 runtime_unavailable`。例如 `primary()` 明确读取至多两条 accepted intent；若出现两条，`scalar_one_or_none()` 的 `MultipleResultsFound` 表示主执行唯一性检查失败，而不是存储暂时繁忙。应只把明确的暂时性异常映射为 503，并为不变量失败保留脱敏的内部错误，避免调用方持续重试。

- [P2] 让 late 权限测试能区分拒绝访问与材料不存在 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp07_api_wp06.py:326-328
  测试选取的是 `NOT late` 的 journal 记录，随后把 `late=True` 的 404 当作权限检查证据。即使移除 `EVIDENCE_LATE` 检查，reader 也可以因为没有对应的 late 材料而返回同样的 404。应断言拒绝时 reader 未被调用，或使用确实存在的 late 材料验证撤权时为 404、授权后可读取。

- [P3] 对结构化日志字段执行脱敏断言 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/negative/test_wp07_api_boundary.py:86-88
  `SafeRoute` 把审计数据写入 `LogRecord.extra`，但测试只在 `caplog.text` 中搜索 token 和正文，并只排除一个名为 `resource_id` 的字段。默认日志格式不显示任意 extra 字段，因此把凭据写入其他结构化字段时，这个测试仍可通过。应检查捕获记录的自定义字段名和值，而不只检查格式化文本。参见 [AGENTS.md:121](AGENTS.md#L121)。