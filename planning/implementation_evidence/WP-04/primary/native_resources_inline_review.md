旧终态证据可以提前释放新的 provider 配额；本地路径则存在超时后进程继续运行、或进程已退出但容量无法回收的情况。结论仅基于附送 diff 的静态审查（`STATIC_REVIEW_ONLY`）；未执行测试或运行时验证（`TEST_NOT_RUN`、`RUNTIME_NOT_RUN`）。

Full review comments:

- [P1] 将 provider 终态证据绑定到不可复用的派发尝试 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/resources.py:489-491
  第一次调用结算后，删除 lease 也删除了 `dispatch_id` 的唯一性记录。第二个 lease 可以在同一 scope 使用相同的 `dispatch_id`；此时用新 `lease_id` 和旧 `terminal_evidence_id` 调用 `settle_provider()`，verifier 收到的参数与第一次完全相同，会在第二次调用仍在途时释放配额。应持久化已使用的派发身份，或让可信终态证据同时绑定本次不可复用的 attempt。参见 [AGENTS.md:93](AGENTS.md#L93)。

- [P1] 让 guardian 独立执行 worker 的截止时间 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/process_guardian.py:46-49
  worker 启动后，guardian 只等控制 socket 可读，没有收到本次调用的 deadline。若 supervisor 被 `SIGSTOP` 而非退出，socket 不会关闭；即使其他进程已将过期 lease 转为 `DRAINING`，guardian 仍会继续等待，超时任务可以无限占用唯一的 CPU/GPU 槽。应把绝对截止时间交给 guardian，到期后由它执行现有的停止、回收和回执流程，而不是仅依赖 supervisor 关闭连接。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P1] 为容器重建后的本地 lease 提供可验证的回收路径 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/process_identity.py:37-38
  若 Compose 重建持有活动 lease 的容器，宿主机 `boot_id` 不变，但新 supervisor 的 PID namespace 与旧记录不同。即使旧容器所有进程均已退出，`process_exited()` 也始终返回 `False`：本地 transport 无法回收，已有 `tree_reaped` 回执的 group 同样无法回收，单槽服务可一直拒绝新任务直到宿主机重启。应提供跨容器稳定的宿主机观察者或旧 namespace 已终止的可信证明，而不是直接将 namespace 不同视为退出。Compose 是[首期部署约束](AGENTS.md#L11)。

- [P1] 避免高编号控制句柄使 guardian 在启动 worker 后退出 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/process_guardian.py:47-47
  `pass_fds` 保留 supervisor 中的原始 fd 编号。在 Linux 上，只要控制 socket 的 fd 达到 `FD_SETSIZE`（通常为 1024），`select.select()` 就会抛出 `ValueError`。此时 worker 已启动，guardian 却未经停树和写回执就退出，既可能留下仍在运行的任务，也会让该槽在本次启动周期内无法回收。这里应使用支持高编号 fd 的 `poll` 或 `DefaultSelector`。

- [P2] 在 SQLite 临时锁冲突后重试写入停树回执 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/process_guardian.py:106-109
  若另一进程持有 SQLite 写事务超过两秒，`_record_reaped()` 会因 `SQLITE_BUSY` 退出。此时 guardian 已经确认没有子进程，却在持久化这一事实前消失；即使数据库立即恢复，`_physically_exited()` 仍因缺少 `tree_reaped` 拒绝回收。应让仍持有停树证明的 guardian 对临时锁冲突退避重试，同时保持容量被占用，直至回执提交。

- [P2] 区分 guardian 异常退出与 worker 正常报告失败 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/lifecycle.py:225-229
  `child.poll()` 返回的是 guardian 的状态。若已收到启动 ACK 且 stdout 已关闭，随后 guardian 被信号终止，这里仍会报告 `PROCESS_FAILED` 并附上已读字节，尽管 worker 可能尚未退出，那些字节也不一定是完整的失败响应。应只在确认 guardian 正常转交 worker 退出状态并留下停树回执时报告带输出的 `PROCESS_FAILED`；失去见证时应报告 `INTERRUPTED`。

- [P2] 将线程池排队纳入调用方可观察的超时 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/lifecycle.py:99-103
  `run()` 在提交任务前计算 deadline，却只在线程获得执行机会后检查它。若 asyncio 默认线程池已被长期任务占满，尚未派发的调用会在 `shield(task)` 中等待远超 `timeout_ms`；此时取消也会再次等待同一个排队任务。应让排队阶段可超时或取消，并在确认尚未派发时释放 reservation，确保后台任务之后不能再启动该 worker。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 只发布已完整初始化的资源账本 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workers/resources.py:161-164
  `provision()` 先以最终路径创建空文件，再写 schema 和 inventory。若进程在两步之间退出，重试初始化会得到 `FileExistsError`，正常打开则得到 `ledger_unavailable` 或 `inventory_mismatch`；一次初始化中断就需要人工处理。应先在同目录完成临时账本的初始化和提交，再以不覆盖已有账本的方式原子发布。

- [P2] 用同步屏障固定重启测试中的存活窗口 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp04_worker_lifecycle.py:237-238
  `supervisor.kill()` 会立即触发 guardian 停树。若测试进程在 `join()` 后被延迟调度，guardian 可以先写好回执并成为 zombie；按当前实现，`restarted.release(lease)` 此时正确地返回 `True`，测试却要求它必须返回 `False`。应在杀 supervisor 前暂停 guardian 或设置显式屏障，先验证存活时拒绝释放，再放行并验证回收，避免把调度先后当作恢复契约。参见 [AGENTS.md:147](AGENTS.md#L147)。