关键阻断是 N06 排除证明：发布路径未接入已暂存的 trace，而证明本身也未绑定 N01 的实际摘要。STATIC_REVIEW_ONLY：结论仅基于所给 unified diff；本地只读沙箱未能启动，冻结基线未独立核验，未调用外部服务。TEST_NOT_RUN / RUNTIME_NOT_RUN。

Full review comments:

- [P1] 将已暂存的 applicability traces 传入 Decision 发布校验 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/engine.py:243-244
  当 RuleSet 已被排除、其 mandatory Rule 因祖先结论保持 `not_evaluated` 时，`no_violation` 必须由父子 trace 共同证明。所给 diff 新增了转发参数，却没有在工作流发布调用处传入 `DecisionMessage.traces`；沿用原调用时默认值为 `()`，`proves_exclusion()` 必然找不到 trace，导致证据完整的 Decision 仍无法发布。应在发布边界传入该 stage 的确切 traces，而不是放宽排除条件。见 [AGENTS.md:115](AGENTS.md#L115)。

- [P1] 用 N01 固定的摘要验证 RuleSet 排除证明 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/domain/nodes/decision.py:188-189
  这里仅要求父、子 trace 的 `context_snapshot_digest` 彼此相等。如果两者保留正确的 snapshot ref，却都带有同一个错误摘要，该条件仍成立；`matches_output()` 也只证明它们是本次 N06 输出的字节，不能证明它们对应实际选中的 Context。应将两条 trace 的摘要与 N01 已固定的摘要比较，再允许该排除证明支撑 `no_violation`。见 [AGENTS.md:101](AGENTS.md#L101)。

- [P2] 按真实 Invocation 失败原因校验全批重试类别 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_validation.py:253-257
  对 `transport_error` 批次，这段校验只排除了 `validation`，并未核对实际 Invocation 的失败原因。同一份超时记录，若 adapter 将可重试的 NodeError 分别标为 `transport` 或 `resource`，两者都能通过这里的检查，但 `JobBudget` 可以只允许其中一类，重试决定因而取决于 adapter 填写的标签。应从已验证的 Invocation 结果映射并核对 NodeError 类别。

- [P2] 在 Attempt 的失败处理边界内校验恢复输入 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker.py:526-531
  `read_observation_retry()` 会对不一致的历史批次抛出 `ValueError`，`runtime_authority_records()` 也会对同一 Evidence ID 的冲突元数据抛错。这些调用均发生在 claim 之后、调用 `_failed_node()` 的 `try` 之前，因此这条路径不会为本次 Attempt 记录实际的校验失败；重新领取任务也不会修复同一份记录。应将这些确定性校验纳入受 claim 保护的失败处理，并与临时数据库错误区分。

- [P2] 恢复已固定的 Context 时不要再依赖当前采集注册表 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_source.py:200-203
  `inspection_worker()` 接受普通 `WorkerHost`，但当该 host 未实现 `HistoricalWorkerHost` 时，恢复分支先调用实时 `worker_source()`，成功后才用 snapshot 覆盖 device/camera。若原设备已注销或注册表暂不可用，即使原图可验证、N01 引用完整，恢复仍会在覆盖之前失败。应直接用固定引用和已验证媒体构造历史 source，或在配置 worker 时明确要求历史读取能力。见 [AGENTS.md:57](AGENTS.md#L57)。

- [P2] 将属于当前 Object 的 grounding 证据纳入 N06 输入 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_nodes.py:124-126
  `runtime_authority_records()` 会收集 `GroundingMessage.evidence`，但调度和执行 N06 共用的 `selected_evidence()` 只读取 N04、N05。若当前 Object 所需证据仅由 N03 持久化，它既不会出现在 N06 upstream pins，也不在证据读取白名单内；执行级记录中有证据，Rule guard 却无法使用。应按当前 Object 的引用选择对应的 grounding 证据，而不是纳入整个 N03 批次。

- [P2] 避免 Decision 线程阻塞等待同一线程池读取证据 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_inspection.py:287-291
  `build_decision` 和 `validate_decision` 通过 `to_thread()` 占用默认线程池，guard 又在此同步等待 event loop；读取 `MODEL_REQUEST` 时，`LocalRequestEvidence.read()` 还要通过 `to_thread()` 使用同一线程池。线程池被并发 N06 占满时，文件读取无法启动，随后超时被转换成 `None`，使实际存在的证据被当作不可用。应分离这两类执行资源，或在进入同步 guard 前完成受限读取。

- [P2] 在丢失 lease 后保持请求证据 ID 与元数据一致 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_request_evidence.py:90-94
  若第一次 Attempt 已 stage 请求证据、但在提交 `ObservationMessage` 前丢失 lease，下一次没有 retry witness 可复用。相同 Object、kind 和内容会生成同一 `evidence_id`，但 `staged_at`、`verified_at` 和 `created_at` 会改用新 Attempt 的时间，导致同一不可变 ID 对应两个不同的 artifact digest。应按 ID 复用首次确定的元数据，或让不同 Attempt 的记录具有不同身份。见 [AGENTS.md:101](AGENTS.md#L101)。

- [P2] 为未提交 N04 消息的请求证据提供补偿清理 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_request_evidence.py:113-115
  这里的 PostgreSQL 事务只取得 advisory lock；文件发布后，并没有在该事务中记录可对账的 staging 状态。如果 worker 随后退出或任务取消，fenced N04 消息不会引用该文件。`recover_orphan=True` 只能在同一 key 再次发布时起作用，不能处理永不重试的任务。应让未提交文件可被发现，并在确认没有持久引用后回收。见 [AGENTS.md:134](AGENTS.md#L134)。

- [P2] 为新持久化消息设置旧 reader 可识别的版本边界 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_records.py:225-227
  默认空 tuple 解决的是新 reader 读取缺字段的旧消息，不是反向兼容。配置模式成功的 N04 消息必须携带旧严格模型不认识的 `durable_request_evidence`；失败批次的 `authority=null` 和无候选调用时 N06 的 `invocation=null` 也不符合原类型。若旧 worker 接手新 worker 已写入的 Execution，恢复会在解析消息时失败。应保留 legacy 写出形状，并为新形状明确分版或禁止旧 reader 接管。见 [AGENTS.md:99–100](AGENTS.md#L99-L100)。