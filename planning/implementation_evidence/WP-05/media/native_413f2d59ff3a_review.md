该切片能校验文件字节的摘要，但部分路径不能证明字节属于哪条权威 receipt，也不能保证 PG 会话中断后已提交文件仍可读取。STATIC_REVIEW_ONLY：结论仅基于所给 unified diff；未运行测试，未独立核验 Git 基线。

Full review comments:

- [P1] 将未保留 raw 的 normalized 字节绑定到权威 receipt — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:574-576
  当 raw 因 `policy_disabled` 未保留时，stage 只检查 `parsed_content` 是 JSON 对象；wire 摘要和 contract 摘要均不能证明这些字节来自该响应。如果集成层混用了两条响应的 `parsed_content`，`read_model_response` 仍会把错误内容随真实 receipt 返回，影响审计和重放。应在可信解析点计算 normalized 摘要并写入 receipt，读回时核对；这不要求保留 raw。[AGENTS.md:124](AGENTS.md#L124)

- [P1] 防止失去 PG 锁的文件线程删除重试已提交的字节 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/files.py:69-74
  若 PG 会话在文件操作期间断开，事务级 advisory lock 会释放，但 `asyncio.to_thread` 中的发布或清理不会停止。另一个进程可对同一 key 成功重试并提交；旧线程随后仍可凭先前的 `recover_orphan=True` 删除固定路径，使新 PG 绑定指向缺失或摘要不符的文件。发布和清理都需要覆盖绑定检查及文件变更的跨进程文件锁，或使用旧发布者无法覆盖、删除的新版本路径。[AGENTS.md:134](AGENTS.md#L134)

- [P1] 在 raw 落盘前核对目标 dispatch 的保留策略 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:548-549
  保留策略只在遍历已有 `dispatch.results` 时检查。若 receipt 尚未入 journal，此时把带 raw 的观察值错传给禁留原文的 dispatch，stage 仍会提交 `model_raw` 文件和绑定。之后即使权威 receipt 标明 `policy_disabled`，读取端也只是隐藏 raw，已落盘的敏感响应不会被删除。应在写文件前按目标 dispatch 的权威策略判定能否保留，不能把事后不可读等同于未保存。

- [P1] 对已提交 stage 但未完成 receipt 的 sidecar 提供恢复路径 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:503-504
  若进程在 `stage_tool_sidecar` 提交后、返回 `ToolWorkerResponse` 前崩溃，文件和 `dispatch_media` 行都存在，但没有成功 receipt 和 Attempt 输出可供读取。这里看到绑定就拒绝清理；同一 Execution 的新 dispatch 若重用该 `evidence_id`，还会被 `dispatch_media_evidence_once` 拒绝，即使字节完全相同。应区分暂存与已确认绑定，在确认旧 dispatch 终止且没有权威输出后，允许受控回收或接续，不改写已确认的历史证据。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 区分应有的 raw 绑定缺失与策略允许的省略 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:656-658
  对于已收到 HTTP headers、记录了非空响应摘要和长度、且没有 omission reason 的 receipt，`model_raw` 行缺失时这里仍返回 `None`，整个读取按成功结束。这样 stage 漏写或恢复时丢失绑定，与有意不保留字节走同一条成功路径。对 receipt 指明应有的 raw，应返回明确的缺失或待对账状态；只有明确允许省略时才正常返回空值。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 核对 geometry 字节与对应 Tool 输出，而不只检查 JSON 类型 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:808-810
  geometry 的内容检查到“是 JSON 对象”就结束。若两个 Object 的几何文件在 stage 时调换，`_tool_asset` 仍会用当前 dispatch 填入 Object 和 producer；读回时又跳过 mask 所有的 typed payload 核对。结果是文件摘要完全正确，但 `VerifiedEvidence` 归属错误。应由生成几何结果的一侧提供独立的内容摘要或可核对的几何值，并在 receipt 读取路径验证，而不是只对收到的文件自身算摘要。[AGENTS.md:61](AGENTS.md#L61)

- [P2] 允许按磁盘 key 发现并回收提交前崩溃遗留的原图 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:472-477
  原图在 Task intake 前发布。如果文件已写入而 PG 事务尚未提交时进程崩溃，磁盘只留下散列文件名，没有 `original_media` 行。这里的恢复入口却必须先知道原始 `scope` 和 `media_id`；若调用方未重试也未另存这些参数，就无法用该入口回收，私有原图会继续占用磁盘。应保留可枚举的发布意图，或支持从磁盘 key 出发、持同一把锁核对绑定后回收。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 持久化首次创建的存储根目录项 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/files.py:15-15
  `mkdir(parents=True)` 允许同时创建 `root` 及缺失的祖先，但初始化只 `fsync(root)`。这能同步 `media-v1` 在 `root` 下的目录项，不能保证新建的 `root` 在其父目录中的目录项已持久化。首次发布后立即掉电时，PG 绑定可能保留而整棵媒体路径消失。可要求部署预先提供持久根目录，或逐级同步本次新建目录的父目录。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 允许取消尚未取得解码槽位的请求 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/persistence.py:265-265
  `_durable(run())` 把等待 semaphore 的阶段也置于 `asyncio.shield` 之内。解码饱和时，已超时或取消的请求仍会持有整份图片字节排队，随后继续解码；并发槽只限制正在解码的数量，不限制这些废弃请求占用的内存。应让等待槽位可取消，仅在工作线程实际开始后保护其生命周期，直到线程结束再释放槽位。[AGENTS.md:126](AGENTS.md#L126)