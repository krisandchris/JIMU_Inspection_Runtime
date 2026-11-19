当前 diff 不能保证删除时引用集合仍然有效，也不能保证通过不同别名读取或处置同一文件时执行相同的授权与来源校验；崩溃后的收尾还依赖重新读取策略库。STATIC_REVIEW_ONLY：结论限于所给 diff，未独立核验冻结基线，未调用外部服务；TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P1] 让新引用的发布与同一物理文件的处置互斥 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:635-639
  `_prepare` 在事务内扫描 `produced_outputs` 并提交 intent，随后 `_dispose` 才删除文件。若另一个 Execution 在扫描后发布指向同一文件的输出，本次迁移只为该表增加了查询索引，没有让引用写入与 intent 互斥；锁住原 Execution 也不能保护另一个 Execution 的写入。结果是新引用可以指向已删除的字节。应让发布方按同一物理对象加锁，并在提交引用前拒绝已有处置 intent 的对象。[AGENTS.md:133–134](AGENTS.md#L133-L134)

- [P1] 读取别名字节前校验物理来源的访问范围 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:369-373
  `read_evidence` 只按所选别名的 `access_scope` 授权；`_binding` 核对 Execution、类型、摘要和大小，但不核对物理来源的访问范围。同一 Execution 中，若 project 级别名指向 task 级主证据，无 `task_id` 的 grant 会被主证据拒绝，却能通过别名读到相同字节。应在返回内容前核对主证据的访问范围，不能仅凭别名放宽授权。

- [P1] 对每个待处置别名执行相同的来源校验 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:829-836
  `_resolve` 会拒绝 `spatial.original_media_ref` 与所属 Task 不符的图像证据，但 `_references` 对同文件的其他别名没有这项检查。若一个 mask 别名引用了错误的原图，直接处置它会失败，从另一个有效 locator 发起却能把它计入已验证的 pins 并删除共享字节。应对每个别名复用相同的来源与物理类型校验，使处置结果不取决于调用方选了哪个 locator。[AGENTS.md:127](AGENTS.md#L127)

- [P1] 为请求证据的保留期校验可信起点 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:855-857
  对 `MODEL_REQUEST`，`_verify_physical_owner` 只要求存在同 `evidence_id` 的候选，截止时间却完全取自输出声明的 `stage.verified_at`。测试中的 `Scenario.archive` 就是在 staging 新字节后写入更早的验证时间；这使刚落盘的请求也能被判定已过保留期。应把用于删除判定的起点绑定到可核验的物理验证记录，无法核验时不要自动处置。

- [P1] 恢复删除前验证 intent 确实覆盖所有保留期限 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:698-700
  已有 pending intent 的分支在到期检查之前返回。模型和 SQL 都允许 `requested_at` 早于某个 pin 的 `expires_at`；只要存量记录的 storage、摘要和 references 与当前记录一致，即使保留期尚未结束，`_dispose` 也会继续 unlink。应把“提交时间不早于所有 pin 的截止时间”设为持久化 intent 的不变量，并在恢复尚未执行的删除前验证它。[AGENTS.md:97](AGENTS.md#L97)

- [P1] 仅让有效的 release 记录解除 hold — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:125-127
  `_HELD` 仅凭匹配的 release 行存在就忽略 hold，而迁移对 release JSON 只校验版本和 `hold_id`。因此缺少 `released_at` 的记录，或释放时间早于 `placed_at` 的记录，虽然不满足 `_release_hold` 的契约，仍会使处置继续。对于不可逆删除，应在数据库写入边界强制这些约束，或在处置前验证 release；无效记录应保留保护。[AGENTS.md:97](AGENTS.md#L97)

- [P2] 允许已删除字节的处置在策略库离线时收尾 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:673-676
  若进程在 unlink 后、写 completion 前停止，重试会先调用 `_references` 重新读取历史 Bundle，之后才查询已提交的 intent。此时只要策略库不可读，重试便返回 `policy_unverified`，已经不存在的文件仍显示 `disposition_pending`；甚至已有 completion 的幂等查询也会失败。应先校验已存处置记录的归属和 pins：对已完成或已确认字节缺失的恢复，不应再把策略库可用性作为收尾条件。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 区分已释放 hold 的重放与当前有效的保护 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:529-534
  `_HOLD` 已读出 `release`，但相同放置请求重试时直接返回旧 hold，且早于 `_PENDING` 检查。这样，一个延迟到释放甚至处置完成之后的重试，得到的成功结果与仍受保护时完全相同。应保持历史记录不可变，同时向调用方明确返回“已释放”或冲突，而不是让它误以为当前 hold 生效。

- [P2] 只将有效且覆盖当前 locator 的处置记录报告为过期 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:390-392
  `read_evidence` 仅由 `file_key` 命中和 completion 非空就报告 `expired`，不解析记录，也不确认当前 locator 在 intent 的 references 中。处置后新增的同文件别名因此会被错误记成已获授权的历史处置对象。此外，迁移中的 `CHECK (... IN (...))` 对缺失 `outcome` 得到 SQL `NULL` 并放行，这样的 completion 同样会被报告为成功。应验证记录、作用域和 pin 成员关系，并让数据库明确拒绝空 outcome。[AGENTS.md:97](AGENTS.md#L97)

- [P2] 按 store_id 和 object_key 共同查找物理别名 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:77-80
  `_key` 将 `store_id` 视为物理身份的一部分，但 `_ALIASES` 在全部 Evidence 输出中只按 `object_key` 查询。若另一个 store 使用相同路径，`_references` 会把实际独立的对象判为 `shared_reference`，使本地文件无法正常处置；其他 store 的记录还会占用 1024 条上限。查询及索引应使用完整物理身份。

- [P2] 将历史 Bundle 解析移出 Execution 行锁 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/media/retention.py:841-845
  `_prepare` 已取得文件键锁和 `FOR UPDATE OF e`，随后在事务内等待策略解析。`HistoricalEvidencePolicies` 对每个不同策略摘要都会读取并解析同一归档、重建 catalogue；别名较多或存储缓慢时，同一 Execution 下其他文件的处置也会被这段 I/O 串行阻塞。应在锁外解析不可变、digest-pinned 的 Bundle，并在短事务内复核引用集合和 hold 后提交 intent。

- [P2] 用实际交付的迁移链验证 011 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp06_retention_postgres.py:106-110
  当 009 或 010 缺失时，fixture 会自行生成占位迁移，再对修改过的目录断言版本 1–11 全部应用。即使该断言通过，也不能证明交付包的真实 catalogue 完整，或 011 能与实际前置迁移共同工作。迁移链验证应直接使用打包资源；若有意支持不完整快照，应将该场景与真实升级验证分开。[AGENTS.md:165](AGENTS.md#L165)