子 Attempt 可以合法进入 `timed_out` 终态，但这不证明工具响应按时到达；当前实现会在该路径持久化错误的执行记录。仅审查所附 diff：`STATIC_REVIEW_ONLY`，`TEST_NOT_RUN`。

Review comment:

- [P2] 不要将截止后返回的 timeout 结果标记为 on_time — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/step_attempts.py:1029-1031
  当父 claim 仍有效、子步骤已过 deadline 时，`_lock_step(..., timeout_result=True)` 允许提交超时终态。若返回的是 `FailedToolResult(timeout)`，且 `tool.completed_at` 已晚于子 deadline，这里仍会把 `unresolved` 改成 `on_time`。这不会产生成功 selection，但会把错误的到达时间判定写进不可变的 ToolExecution。应保留超时终结路径，仅在有按时到达的依据时写入 `on_time`。参见 [AGENTS.md:61](AGENTS.md#L61)。