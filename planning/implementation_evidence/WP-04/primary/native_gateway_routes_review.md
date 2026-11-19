STATIC_REVIEW_ONLY：仅审查所附增量，未调用工具、未执行测试。当前代码可将显式未验证的内容发布为成功输出，也无法在所示派发路径中识别已过期的路由授权或截短的 fallback 历史，建议修订后再验收。

Full review comments:

- [P1] 在发布模型输出前校验固定的响应契约 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:313-314
  将附件中的 `_successful_result()` 替换为 `parsed_content=b"not-json"` 并标记 `response_bytes_omission_reason="unvalidated"`，仍会通过这里的非空检查。后续 `_model_record` 不接收这些内容，因此即使 wire digest 正确，`project_dispatch` 也会生成 `SucceededInvocation` 并返回无效输出。最小修订是在成功分支前拒绝显式 `unvalidated` 的结果，并要求内容通过当前 operation 固定的响应 schema 校验；用上述反例断言 `output is None`。[AGENTS.md#L124](AGENTS.md#L124)

- [P1] 阻止凭据回显通过 parsed_content 返回 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:99-103
  当 transport 标记 `credential_echo` 时，这里只禁止保留 `response_bytes`，并不禁止 `parsed_content` 含有同一回显值。若其他成功字段齐全，该值会作为 `DispatchOutcome.output` 返回；即使设置了 `error`，它仍可从 `transport_result.parsed_content` 读取。最小修订是对 `credential_echo` 禁止携带未脱敏的解析内容，仅保留 digest 和安全元数据，并增加同时检查这两个返回路径的测试。这与 schema 校验不同：凭据字符串本身可以符合 schema。[AGENTS.md#L119-L121](AGENTS.md#L119-L121)

- [P1] 在物理发送前复核路由授权的有效期和撤销状态 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:330-333
  若请求在 `plan_primary` 后排队，直到 ownership 的 `valid_until` 才发送，`started_at >= approved_at` 仍然成立。`PlannedRoute` 未携带该截止时间，`_model_record` 也未接收这条 ownership registration，因此普通 primary 调用仍可被投影为成功。附件中名为 `at_dispatch` 的测试只调用了 planner，未覆盖这个时间差。最小修订是在发送边界用实际发送时间复核当前 ownership、capability 和适用 review；把最早失效时间带入 route，供投影拒绝越期记录，而不只在调用完成后检查下界。[AGENTS.md#L122-L123](AGENTS.md#L122-L123)

- [P1] 核对 fallback 历史末项是否为该操作的最新调用 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/routing.py:663-665
  这里只证明传入的 Attempt 编号递增，不能识别过期的历史前缀。以附件的 `REMOTE → LOCAL` 为例：Attempt 1 在 REMOTE 失败，Attempt 2 已调用 LOCAL；Attempt 3 若只传 `history=(first,)`，校验仍通过，`visited` 也只有 REMOTE，于是再次以 Attempt 1 为触发依据选择 LOCAL。最小修订是用调度器为该 operation 保存的权威链头或历史摘要校验传入历史，再计算可选边；不要要求节点级 Attempt 编号连续，因为中间编号可能属于其他 operation。[AGENTS.md#L93](AGENTS.md#L93)

- [P2] 禁止将明确的 HTTP 错误响应投影为成功 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:119-120
  将 `_successful_result()` 的 `response_status` 改为 `429`、其余字段不变，是当前 `TransportResult` 接受的输入。状态码只用于判断 `acknowledged`，不参与 outcome 分类，因此该响应会被记为成功，而不是可供策略判断的 rate-limit 失败。最小修订是要求 4xx/5xx 携带对应的 `InvocationError`，或在投影时将其映射为失败；保留响应 digest，但不发布输出。

- [P2] 在构造派发请求时执行输入字节上限 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:43-46
  附件 fixture 明确把 `input_byte_limit` 固定为 65536，但以同一 route 构造 65537 字节的 `DispatchRequest` 仍能通过，并可直接交给 `ModelTransport.dispatch`。最小修订是在这个持有实际 payload 和固定资源策略的边界检查长度，补充恰好到限与超限一字节两个用例，确保超限请求在发送前失败。[AGENTS.md#L126](AGENTS.md#L126)

- [P2] 校验完整响应字节与观测长度一致 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:114-118
  当 `response_bytes` 仅含已收到正文的前缀，而 `observed_response_bytes` 大于该前缀长度时，当前校验只检查计数非负。`project_dispatch` 随后会对前缀计算 digest，并把它当作完整响应摘要。最小修订是在同时提供这两个字段时要求计数等于 `len(response_bytes)`；不完整正文应只保留观测长度及 `incomplete` 标记，不生成完整 digest。

- [P2] 将已收到部分响应字节的调用标为 acknowledged — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/invocation.py:305-308
  若 transport 在收到 128 个响应字节后超时，未提供状态码或 provider request ID，且按约定没有完整 digest，当前分支会返回 `sent_unknown`。此时未知的是响应是否完整，而不是对端是否已经响应。最小修订是将正的 `observed_response_bytes` 也作为 `acknowledged` 的依据，并在现有 partial-response 测试中增加无状态码的变体，同时保持 `response_digest is None`。