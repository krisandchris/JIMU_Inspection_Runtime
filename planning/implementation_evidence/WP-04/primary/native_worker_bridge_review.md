数据库 fence 通过并不代表 Tool 注册仍然有效；此外，发送前取消、N03 超时起点和硬预算分类均会影响实际调度或接管路径。仅对所附限定 diff 做了静态审查（STATIC_REVIEW_ONLY），未运行测试。

Full review comments:

- [P1] 在允许 Tool 启动前复核实时注册 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge_tools.py:483-484
  `before_dispatch()` 在等待 `journal.begin()` 之前检查注册。若等待数据库锁期间注册到期或被撤销，父子 claim 的 fence 仍可能通过，这里就会返回 `True`，允许启动已失去注册资格的 Tool。执行后的 `_available()` 只能阻止采用输出，不能撤回物理调用。应在 intent 提交后、返回 `True` 前复核注册；若已失效，用该 intent 记录 `not_dispatched`。见 [AGENTS.md:125](AGENTS.md#L125)。

- [P2] 为 intent 提交后的发送前取消记录未发送结果 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge.py:978-982
  `journal.begin()` 已提交后，若 worker 在等待 `current_plan()` 时被取消，`CancelledError` 会直接进入释放 lease 的 `finally`。此时尚未调用 transport，却留下没有结果的已准入 intent。若 Execution 仍需接管，后续 Attempt 在禁用歧义主路重试时会被 `_plan()` 拒绝。应在已提交 intent、尚未进入发送的取消路径写入 `not_dispatched`，再传播取消；不要将它与发送中的取消混同。见 [AGENTS.md:93](AGENTS.md#L93)。

- [P2] 从本次 grounding 调用开始计算 Tool 超时 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge_tools.py:297-298
  对 N03 `GroundingCall`，`attempt` 是父节点 Attempt，而 `policy.timeout_ms` 属于本次 Tool。若父节点准备输入等工作已耗时超过这个 Tool 的超时，传给 executor 的 `deadline` 在 Tool 启动前就已过期，即使父 claim 仍有效。N03 应从本次调用开始计时，并受父级剩余期限约束；N05 有独立子 Attempt，不必改变其计时起点。见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 区分永久调用预算耗尽与临时资源不足 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge.py:210-211
  `budget_guard()` 将节点或操作的硬上限耗尽也返回为 `resource_exhausted`，这里会把它转成 `RetryableNodeFailure`。当额度已被确认发送的请求用完时，退避、创建新 Attempt 或切换 deployment 都不能恢复该额度，只会消耗重试次数并延迟终态。应区分耗尽的预算维度，对节点或操作硬上限返回不可重试失败，保留临时资源不足的重试路径。