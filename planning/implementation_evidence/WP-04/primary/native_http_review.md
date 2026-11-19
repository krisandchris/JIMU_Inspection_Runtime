接收端既可能接受违反固定派生 Schema 的结果，也可能拒绝带标准元数据的有效 Provider 响应；这两项直接影响 WP-04 成功路径。STATIC_REVIEW_ONLY：仅审查所附代码与测试，未调用工具或执行测试。

Full review comments:

- [P1] 用本次路由固定的派生 Schema 校验返回内容 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http.py:336-339
  当 `response_schema` 比 `response_model` 更严格时，发送给 Provider 的是派生 Schema，但接收端只执行 `response_model.model_validate_json(content)`。例如，派生 Schema 把 `finding` 限定为一个枚举，而基础模型只要求字符串；枚举外的值仍会作为成功的 `parsed_content` 返回。应在注册时为固定 Schema 建立本地校验器，返回前同时校验派生 Schema 和 Pydantic 模型，并补一个“通过基础模型、违反派生枚举”的负例。见 [AGENTS.md §7](AGENTS.md#L124)。

- [P1] 接受受支持 Provider 的标准响应元数据 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http_wire.py:312-315
  正常的 OpenAI 成功响应可以在 `usage` 中携带 `prompt_tokens_details` 和 `completion_tokens_details`；vLLM 的 `choices[]` 也可以携带 `stop_reason: null`。严格的 `_Usage`、`_Choice` 均未声明这些字段，因此即使模型身份、结束原因和业务内容全部有效，整次调用仍会返回 `http_invalid_response_envelope`。应按受支持的协议补齐已知可选字段，并为两个协议分别加入代表性响应 fixture，而不是关闭项目要求的 [严格外部契约](AGENTS.md#L99)。

- [P2] 拒绝未报告模型身份的成功响应 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http.py:320-325
  如果成功响应省略 `model` 或返回 `model: null`，这里会跳过与 `route.ownership.model_id` 的比对，而 `ChatResponse.model` 也允许这两种情况；仅含有效 `choices` 的响应因此可以在没有实际模型身份的情况下返回成功。应将这两个 Chat Completions 协议的成功响应 `model` 设为必填并无条件比对，分别覆盖缺失和 `null`，不要用配置值代填观测值。见 [AGENTS.md §7](AGENTS.md#L123)。

- [P2] 防止数值舍入改变已固定的请求 Schema — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http_wire.py:95-99
  外部提供的 `response_schema` 按原始字节计算 digest，但 `response_schema_document()` 会经 `_finite_float` 解析后再序列化。例如，合法约束 `minimum: 1e-400` 会通过 `isfinite` 检查并以 `minimum: 0.0` 发出，实际发送的约束已不同于路由固定的约束。应精确保留 Schema 数值，或在注册和请求比对时拒绝不能按数值无损往返的表示，并加入这个边界用例。见 [AGENTS.md §5](AGENTS.md#L101)。

- [P2] 将大载荷校验移出共享事件循环 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http.py:456-461
  接近允许上限的图像请求会在 `dispatch` 的事件循环上同步完成整包 JSON 解析、遍历和 Base64 解码；响应端的 `_parse_success` 同样同步解析并校验。执行这些步骤时，`asyncio.timeout_at` 不能及时交付取消，同一循环上的其他调用也不能读 socket，可能因另一请求的校验而超时。应把大载荷校验交给并发受限的 worker，并让未完成的校验继续计入资源预算。见 [AGENTS.md §7](AGENTS.md#L126)。

- [P2] 复用 TLS 验证上下文，避免逐请求同步加载 CA — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http.py:529-531
  每次 `_exchange` 都在事件循环内以 `verify=True` 构造新的 `AsyncHTTPTransport`，使 HTTPX 重建 SSLContext 并同步加载 CA；即使目标是明文 loopback，也要先完成这一步才到达下一次 deadline 检查。并发调用会重复承担这项启动成本，短 deadline 甚至可能在发送前耗尽。最小修订是在适配器初始化时创建验证用 SSLContext 并传给各次 transport，保留现有单次 POST、无重试和不复用连接的行为。

- [P2] 按 GMT 解析合法的 asctime 格式 Retry-After — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/http.py:181-184
  HTTP-date 允许不带显式时区的 `asctime-date`。`parsedate_to_datetime` 对该格式返回 naive datetime，导致此处直接返回 `None`；429 或 503 因而会丢失服务端给出的有效等待时间。应仅对匹配该 HTTP 日期格式的值补 UTC，再沿用现有上下限裁剪，并增加一个未来日期用例。