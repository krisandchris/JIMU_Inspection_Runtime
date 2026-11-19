Evidence 缺少摘要 pin 时仍可形成完整证明；反过来，候选选择顺序和失败缓存扣费都能阻断证据充足的 Rule。STATIC_REVIEW_ONLY：结论限于所给 unified diff，未独立核验 Git 基线，未运行测试。

Full review comments:

- [P1] 要求进入 guard 证明的 Evidence 匹配 N06 摘要 pin — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/rules/_guard_sources.py:108-112
  `check_input_pin()` 在找不到 pin 时直接放行。独立传入的 `evidence_refs` 和 `evidence` 只需按 ID 相符；对于 `ORIGINAL_IMAGE`，也不要求有 selected Fact 引用它。因此，一条满足作用域、策略和字节校验、但不在 N06 摘要 pin 集中的原图记录，仍能使 guard 返回 `complete`。fence 保证本次读取一致，却不能证明这条记录就是 N06 输入固定的版本。应在采用 Evidence 前要求其元数据摘要与该输入的 pin 匹配。见 [AGENTS.md:101](AGENTS.md#L101)。

- [P2] 按大小选择同类 Evidence，避免可满足的 Rule 被预算阻断 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/rules/_guard_sources.py:358-361
  `required()` 按 `record_id` 顺序取每种类型的第一条可读记录。若预算为 10 MiB，第一种必需类型有 8 MiB 和 2 MiB 两条合格记录，第二种只有 6 MiB，先选 8 MiB 就会返回 `EVIDENCE_UNAVAILABLE`，尽管 2 + 6 MiB 的组合可以完整验证。对同类合格候选按 `(byte_size, record_id)` 排序即可消除这个由 ID 顺序造成的误阻断，无需提高读取上限。

- [P2] 跳过已缓存为不可读的 Evidence，不再扣减后续 Rule 的预算 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/rules/_guard_sources.py:365-366
  `remaining_bytes` 在 `_readable()` 查询缓存之前就被扣减。若同类 A、B 等大，每条 Rule 的预算恰好能读取一条，前一条 Rule 已确认 A 摘要错误，后一条 Rule 仍会在 A 上耗尽预算，既没有再次读取 A，也不会尝试健康的 B。应在扣减预算前跳过缓存值为 `False` 的候选，保留对实际读取的预算限制。