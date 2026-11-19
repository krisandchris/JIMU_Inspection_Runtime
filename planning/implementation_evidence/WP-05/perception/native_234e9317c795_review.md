正常路径可见 N01 固定 Context、N02 按 eligible 分组校验命中、N03 按 Profile 独立请求，以及 N04 每个 Object 一次联合 dispatch、以同一 invocation 逐题生成 Facts。主要缺口在失败隔离与重试历史归属；N06 仅展示候选适配器，不能据此确认未提供的 Worker 一定执行全部 mandatory Rules。STATIC_REVIEW_ONLY：仅审查用户标注冻结基线的 diff，未独立核验 HEAD，未调用外部服务，TEST_NOT_RUN / RUNTIME_NOT_RUN。

Full review comments:

- [P1] 用可信前序批次校验 N04 重试归属 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/observation.py:557-557
  `verify_observation_result()` 重建输入时直接回填待验证消息自己的 `inputs.retry`，且没有接收历史 batch 或 attempt。即使当前响应字节、invocation 和逐题 Facts 全部正确，这个校验也不能证明 `previous_batch_ref` 指向同一 Object、同一 NodeRun 的较早失败批次。应从独立取得的历史记录重建 retry 引用及 reason，再与消息比对；当前请求的 receipt 不能替代前序 attempt 的归属证明。见 [AGENTS.md:93](AGENTS.md#L93)。

- [P2] 在重发 N04 请求前检查前次失败是否允许重试 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/adapter.py:45-51
  这里检查了历史 authority、NodeRun 和 attempt 顺序，却没有检查 `previous.message.output.error.retryable`；传给 `observe()` 后又只剩 batch。因而，只要调用方传入归属正确但标记为不可重试的失败记录，该入口仍会重新暂存证据并调用模型。应在丢弃历史 `FailedNodeOutput` 前核验重试资格，而不是仅依据 batch 不属于 `complete/partial`。

- [P2] 将迟到的 N04 成功批次保留为非权威历史 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/observation.py:772-773
  联合响应可解析但 `completed_at > claimed.deadline` 时，代码已生成带原 attempt 和真实 invocation 的 `late` batch，却在返回 artifacts 之前抛错。这样适配器既不授予 authority，也不交回该批次供历史归档；两者应分开。应通过不授予 authority 的历史路径交回迟到结果，同时保持当前 lease 的提交限制。见 [AGENTS.md:93](AGENTS.md#L93)。

- [P2] 保留 N04 回执不一致的具体失败原因 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/observation.py:726-728
  当响应已解析、但本地复核的 invalid/missing slots 与 transport receipt 不同时，明确生成的 `perception.joint_slot_receipt_mismatch` 随即被丢弃。此路径已有成功 invocation 且 `dispatched.error is None`，所以 `_failed_batch()` 将其记成通用的 `perception.observation_unavailable` 和 `parse_error`。历史记录及后续 `ObservationRetry.reason` 因而无法区分回执不一致与内容解析失败；应保留捕获的错误码，并按实际失败阶段分类。

- [P2] 回放失败批次时核对失败类别和重试性 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/observation.py:573-579
  `verify_observation_result()` 对失败分支只比对错误码、空题集及 issue 数量，没有从已认证失败重建 `batch.outcome`、`error_class` 或 `retryable`。例如真实 invocation 为 timeout 时，仅核对相同的 code 并不能证明历史批次记录的是 `transport_error`，也不能证明保存的重试决策正确。应让生成与复核使用同一套失败分类逻辑。

- [P2] 按 Question 的固定策略判定 UNKNOWN — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/observation.py:432-435
  `_resolve_questions()` 解析了 `unknown_policy_ref` 却未保留或检查策略内容；只要联合 slot 通过验证且值为 `insufficient_visibility`，这里便无条件产出 validation 为 `validated` 的 `UNKNOWN` Fact。这样只能证明模型返回了该字符串，不能证明这道题的策略允许如此解释。应按固定策略判定，或在构建 plan 时显式拒绝不支持的策略，同时保持错误仅影响对应题目。见 [AGENTS.md:77](AGENTS.md#L77)。

- [P2] 不要把可选 Context 字段的冲突记为必需字段失败 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/context.py:184-189
  当一个 `required=False` 的字段同时来自 Task metadata 和 Region，且两个合法值不一致时，这里绕过按 `entry.required` 选好的 `diagnostics`，直接追加到 `required`。即使所有必需字段均有效，`build_context()` 仍返回 `required_context_failure` 且不生成快照。应省略有冲突的可选值并记录 optional issue，而不是将其报告为必需字段失败。

- [P2] 将无效的 N02 提案隔离在对应 binding 分组 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/discovery.py:154-157
  当 binding A 已返回合法命中，而 binding B 返回重复或不属于 B 的 Profile ID 时，`accepted.clear()` 会删除 A 的命中，`break` 还会跳过其余独立分组。拒绝 B 的整份提案足以保证只接受 eligible Profiles；不必撤销另一真实 invocation 已验证的结果。应像无输出分组一样记录该组失败、继续处理，并在存在有效结果时返回 `partial`。

- [P2] 在 N02 partial 输出中保留失败分组的诊断 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/discovery.py:244-247
  当一个分组完成、另一个分组无输出且无 invocation 时，后者的 `NodeError` 仅用于选择 `partial`：成功 envelope 不携带它，`diagnostics` 又固定为空，也没有失败 invocation 可回查。调用方能看到结果不完整，却无法从返回记录查到该组的失败原因。应在 diagnostics 中保留对应 binding/Profile 及错误码，以区分未完成检测与已完成但未命中。见 [AGENTS.md:79](AGENTS.md#L79)。

- [P2] 隔离 N03 单个 Profile 的未分发失败 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/grounding.py:135-139
  多个 Profile 顺序 grounding 时，只要其中一次没有 invocation，这里便退出整个 `ground()`：此前积累的 records、Objects 和 invocation refs 不会出现在 `NodeResult`，后续 Profile 也不会执行。没有真实 invocation 时不能伪造 producer，但应把该 Profile 的未分发失败与其他 Profile 已完成的 grounding 分开处理。见 [AGENTS.md:59](AGENTS.md#L59)。

- [P2] 将候选模型的图像读取失败作为 advisory 失败返回 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/perception/candidates.py:58-58
  N06 在处理 `ModelCallFailure` 的边界之外调用 `access.image()`。若 host loader 因读取超时而失败，`predict_candidates()` 会抛出异常，而不是像模型不可用时那样返回空 candidates 和 failure code。应将可恢复的读取失败纳入 advisory 返回路径，保留媒体摘要不匹配等完整性错误的单独处理，使调用方不依赖异常兜底也能继续 mandatory Rule 检查。见 [AGENTS.md:62](AGENTS.md#L62)。