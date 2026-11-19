即使 PG bridge 正确放行、host 正确注册 handle，已校验文件与实际启动文件仍可不同，远端失败的终态凭据也可能未被用于归还配额；存证故障还会让已执行调用失去审计结果。这些问题影响本地工程验收。本次仅审查所附 diff：STATIC_REVIEW_ONLY，TEST_NOT_RUN。

Full review comments:

- [P1] 确保 CPU worker 执行的是已校验的同一份文件 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/registry.py:251-254
  摘要校验后还要等待资源预留和 `before_dispatch`，随后 runner 才按 `self._argv` 重新打开路径。如果本地升级在等待期间原子替换了脚本，即使 host 注册和 PG 放行都正确，进程也会运行新内容，却返回旧版本和 proof。现有回归只覆盖调用前替换；应从已校验的不可变文件快照启动，而不是再次解析可变路径。[AGENTS.md:87](AGENTS.md#L87)

- [P1] 用失败响应携带的终态凭据结算远端配额 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/registry.py:353-356
  当 `send` 抛出 `ToolWorkerFailure(dispatched=True, response=...)`，即使 `response.terminal_evidence_id` 有效，也会跳过 `settle_provider`。所附单槽 `PROVIDER_INFLIGHT` 回归已表明，普通 release 不会归还这种配额；因此一次已确认终止的失败就能阻塞下一次调用。应让正常响应和异常携带的响应经过同一终态验证路径，再原样传播失败，而不是无条件释放槽位。[AGENTS.md:126](AGENTS.md#L126)

- [P1] 为响应存证设置有界等待 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1449-1451
  `asyncio.timeout` 在 `worker.run` 返回时已经退出。如果 `record_response` 等待对象存储而不返回，即使 worker 按时完成，公开的 `execute` 也会无限挂起，调用方拿不到已经算出的响应摘要。应给存证设置明确的剩余预算或独立收尾预算，并在超时时保留已收到响应这一事实。[AGENTS.md:126](AGENTS.md#L126)

- [P1] 在存证 I/O 失败时保留已执行调用的审计结果 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1555-1558
  如果 `record_response` 在写文件或对象存储时抛出 `OSError`，响应处理只捕获 `ValueError` 和取消，因而 `execute` 直接抛出裸异常。此时请求已派发，响应摘要、长度和实现版本也已取得；调用方却拿不到这些信息来区分「存证失败」与「工具未执行」。应返回或随异常携带已知的部分审计结果，不要把未写成的资产标记为已保存。[AGENTS.md:61](AGENTS.md#L61)

- [P2] 将远端租约激活移出事件循环并在发送前复核截止时间 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/registry.py:340-342
  `activate_remote` 与 reserve、settle 一样操作本地持久化租约，但这里直接在事件循环调用。SQLite 写锁竞争时，它会连带阻塞其他调用的超时和取消；激活返回时也可能已超过本次 deadline，而代码会直接进入 `send`。应异步等待激活、妥善结算取消期间完成的激活，并在发送前再次检查时间。[AGENTS.md:126](AGENTS.md#L126)

- [P2] 在取消预留时确保后台取得的租约得到释放 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/registry.py:137-143
  如果线程中的 `admission.reserve` 尚未返回，外层调用就超时并退出 `asyncio.run`，事件循环关闭时会取消 `pending`。`shield` 不能阻止这种直接取消，也不能停止已运行的线程；线程随后仍可能成功写入租约，而 `release_later` 因 `completed.cancelled()` 跳过释放。与回归相同的短生命周期事件循环因此可能让下一次调用在租约过期前误报容量耗尽。清理不应依赖即将关闭的循环再启动一个后台任务。[AGENTS.md:126](AGENTS.md#L126)

- [P2] 避免单项输出错误抹掉同批有效 Fact — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:733-738
  当某个映射的 `value_path` 也是 pinned payload schema 的必填字段时，代码先把非法值移除并将其状态改为 `ERROR`，随后又用要求该字段存在的完整 schema 校验剩余 payload。第二次校验会把整个调用变成 `tool_result_invalid`，本来有效的兄弟 Fact 也随之丢失。当前 `missing` 回归未把 `outside` 设为 payload 必填，覆盖不到此分支；应在接纳 schema 时确保它能表达部分结果，或按部分结果契约校验保留的字段。

- [P2] 在失败附带响应时保留独立核实的实现版本 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1438-1441
  `ToolWorkerFailure` 可以同时携带已观察并匹配 pin 的 `observed_implementation` 和一个没有版本元数据的有界 `response`。没有响应时 executor 会记录已核实版本；仅仅多附上响应字节，就改为只查 `response.actual_*`，把同一版本记成 `unavailable`。应合并这两个证明来源：缺失不应覆盖已核实的观察，二者冲突时才按冲突处理。[AGENTS.md:87](AGENTS.md#L87)

- [P2] 保留 worker 失败原因与已验证诊断证据的关联 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1384-1388
  当 `ToolWorkerFailure.error.diagnostic_refs` 指向异常响应中随附的有效证据时，`_tool_error` 会在验证这些资产前无条件清空引用。后续即使把资产保存在 `evidence` 中，失败记录也无法指出哪一项支持该错误。应在响应证据通过作用域检查后保留可解析的诊断引用，而不是丢弃全部引用或直接信任未经验证的引用。[AGENTS.md:61](AGENTS.md#L61)

- [P2] 合并由两个渠道返回的同一份响应证据 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:569-571
  如果 worker 在 `response.evidence` 中返回原始输出资产，而幂等的 `record_response` 对同一份字节返回同一个 `EvidenceAsset`，两边各自都没有重复，合并后的长度检查却会把健康响应降为 `tool_result_invalid`。应区分「同一 ID、完全相同的资产」与「同一 ID、摘要或作用域冲突」：前者只记录一次，后者继续拒绝。