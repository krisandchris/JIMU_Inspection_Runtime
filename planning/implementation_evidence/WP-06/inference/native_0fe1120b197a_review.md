所示路径在幂等返回前重新授权，要求 target pins 等于 source pins，并在同一事务中写入目标 Execution、谱系和首个 job；启动、取消及完成分支也跳过了原 Task 的状态投影和相应转换事件。但终态源可在没有冻结输入时被受理，且回放入队仍与原 Execution 状态锁竞争，因此不能确认严格隔离和可重开性。STATIC_REVIEW_ONLY：结论限于所给 diff，未独立核验冻结基线；TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P1] 拒绝没有冻结采集快照的终态源执行 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/inference.py:167-168
  原 Execution 若在 N01 产出快照前被取消，且历史 capabilities 存在，这个条件不会拒绝请求。随后受理事务会把 `capture_execution_id` 和 `capture_snapshot_digest` 都写成 NULL；Worker 重开时也拿不到历史上下文。即使媒体和 Release pins 校验通过，也无法确认使用了原 device/camera 修订。应在源快照和继承快照都不可用时拒绝受理，并覆盖「原执行在 N01 前取消」的用例。[N01 不可变快照约束](AGENTS.md#L57)

- [P2] 避免回放入队与原 Execution 的状态锁互相阻塞 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/repository.py:490-490
  原 Worker 持有 `FOR UPDATE OF e` 时，回放受理插入 `inference_replay_runs` 所需的源 Execution 外键 `KEY SHARE` 锁仍会等待；只降低 Task 行的锁强度没有消除这项竞争。新增测试是在 `submit()` 完成后才锁住源执行，因此只覆盖回放取消，不覆盖入队。对于不修改引用键的状态写入，应使用与 `KEY SHARE` 兼容的行锁，并验证持有源执行锁时新回放仍能受理。

- [P2] 不要在授权前暴露源 Execution 是否存在 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/inference.py:127-131
  同一个无权回放的 principal 在源不存在时会直接收到 `inference_replay_source_missing`，在源存在时才进入 Host 并收到授权错误。仅凭 `submit()` 的错误类型即可区分两种情况。应让无权访问者看到一致的结果，同时保留对实际源 Task 的授权检查，包括幂等命中路径。

- [P2] 在重开时校验采集快照确实来自源执行链 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/workflow/worker_state.py:272-276
  同一 Task 可以有多个相同 pins 的回放。若持久化的 capture ID 和 digest 一起错指另一个同级回放，外键、这里的 Task/pins 比较以及后续快照摘要校验都会通过：它们证明读到的是被指向的快照，却没有证明它属于 `request.source` 的继承链。应验证 capture 是直接源执行或该源已固定的祖先 capture，并用同级回放错配作负例。[N01 不可变快照约束](AGENTS.md#L57)

- [P2] 明确幂等受理事务所需的 PostgreSQL 隔离级别 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/inference.py:201-205
  若传入的引擎使用 `REPEATABLE READ` 或 `SERIALIZABLE`，并发同键请求中的等待者会在获取 advisory lock 的 SELECT 开始时固定快照。即使先行事务随后提交，锁后的 `_find_accepted()` 仍可能看不到已有行，最终得到唯一约束或序列化错误，而不是同一个 run。应为该事务明确使用 `READ COMMITTED`，或在新事务中重试并读取已提交结果。[回放与幂等去重约束](AGENTS.md#L93)