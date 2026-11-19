历史投影会丢失 Tool 产出身份，也可能静默遗漏未被 coverage 引用的对象，因此不能保证评分忠实反映所选执行。历史路径读取的是持久化记录，但所给测试的模型响应、Tool 和标签来自 fixture；`auto_cleared=None` 表示未观测，非零放行风险来自单独的 `synthetic_inputs()`，不是历史放行实测。STATIC_REVIEW_ONLY：结论仅依据所给 diff；未独立核验冻结基线，TEST_NOT_RUN，未调用外部服务。

Full review comments:

- [P1] 按 Tool 产出绑定归属 Fact，不要只匹配 FactSchema — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/history_projection.py:212-218
  `_profile_binding` 核对了 ToolPlan、step、tool、output 和 fact key，但生成 `geometry` 时只保留 `fact_schema_ref`；这里也只按该 ref 接纳 `ToolFactSource`。两个不同输出复用同一 schema 时，只有一个输出到场就无法区分来源，两个都到场则报 `evaluation_geometry_fact_ambiguous`。应保留产出绑定并与历史 `ToolExecution` 对账；若评测契约只能按 schema 寻址，应在构建 catalogue 时拒绝歧义，而不是根据本次到场的 Fact 决定。参见 [AGENTS.md:61](AGENTS.md#L61)。

- [P1] 在投影前双向核对 grounding 对象与 coverage 清单 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/history_projection.py:253-256
  `project_profiles` 检查每个 coverage 引用能否找到 Object，却不检查每个传入的 Object 是否被 coverage 引用。例如 grounding 含 A、B 而 coverage 只引用 A 时，B 的 Fact 会通过 `object_map` 检查，随后 B 和这些 Fact 均不进入评测，也不报错。历史记录不一致因而表现为模型少检了一个对象。应先检查对象 ID 唯一性，按 Profile 双向核对成员，并拒绝没有对应对象的 Decision。参见 [AGENTS.md:59](AGENTS.md#L59)。

- [P2] 用导出时独立留存的 source pin 验证重载后的评测 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp06_eval_history.py:327-330
  测试虽然生成了 `restored`，实际评分却仍使用内存中的 `frozen`，且 `expected_source` 在调用点由待评分的同一对象计算。这样只证明可信进程内的对象可以评分，不能验证离线消费者会拒绝另一份内部自洽、但不是原先选定的导出。应在可信导出边界单独留存 pin，用它对 `restored` 评分，并增加另一份有效 source 与该固定 pin 不符时的拒绝用例。参见 [AGENTS.md:101](AGENTS.md#L101)。

- [P2] 让历史评分测试实际比较 Geometry 和 Rule 输出 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp06_eval_history.py:296-297
  正例对象的 `geometry` 和 `rules` 标签都为空。后续断言验证了 ToolExecution 数量、sidecar 可读性、任务级 Decision 和一个 Question，却没有验证任何几何值或逐 Rule 结果；即使投影始终返回空 `geometry` 和空 `rules`，这些断言也无法发现。应为 fixture 中至少一个已知 Tool 输出和 RulePath 给出独立期望，并断言对应评分的分子、分母。参见 [AGENTS.md:115](AGENTS.md#L115)。

- [P2] 分开测试错误 Scope 与正确 Scope 下记录不存在 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp06_eval_history.py:389-390
  该负例把 dataset 和 request 同时切到另一个 client，两者仍然相等，预期的 `execution_unavailable` 只验证另一个 Scope 下查不到所选 Execution。即使删除 `export()` 开头的 `dataset.scope != scope` 检查，该用例仍可通过。应保留当前用例，并只改其中一方，断言返回 `evaluation_scope_mismatch`；这两种失败不应互相替代。参见 [AGENTS.md:145](AGENTS.md#L145)。

- [P2] 为整个 trial 设置累计证据读取预算 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/history.py:639-644
  每次 `_sample` 都让 `_verify_responses` 和 `_verify_evidence` 分别从 128 MiB 重新计数。含 N 个样本的请求仅这两类内容就可读取最多 N × 256 MiB，而且读取期间一直持有同一个 `REPEATABLE READ` 事务。现有单样本上限不能在整批工作量过大时提前终止；应保留单项上限，同时在 trial 内共享累计字节预算，并测试多个单独合规的样本合计超限的情形。参见 [AGENTS.md:126](AGENTS.md#L126)。

- [P2] 每个 trial 只构建一次相同 Bundle 的评分 catalogue — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/history.py:608-610
  同一 Bundle 的每个样本都会重新遍历全部 Profile、producer declarations 和 Rule 来构建完整 catalogue，且每份 catalogue 都由 `exported` 保留到整批结束，最后才比较是否相同。多样本导出因此同时承担重复的 CPU 和内存开销。可继续逐 Execution 验证 pins，但按已验证的 Bundle digest 复用同一份 catalogue 及其摘要，并在发现不同 digest 时立即拒绝。参见 [AGENTS.md:87](AGENTS.md#L87)。

- [P2] 在批量导出错误中保留失败样本的身份 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/history.py:535-541
  多样本导出在首个错误处终止，但这里要么原样抛出只有 `code` 的 `EvaluationHistoryError`，要么把 `ValueError` 包成不带定位信息的错误码。例如两个样本中有一个缺少 evidence，调用方只能看到 `evaluation_evidence_missing`，无法从结构化结果确定应核查哪次执行。应保留稳定错误码和异常链，并附上当前已授权 selection 的 `sample_id`、`execution_id`，无需暴露原始证据内容。