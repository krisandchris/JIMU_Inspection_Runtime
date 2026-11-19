所示 SQL 使用复合 owner 键限定读取，指标标签也有白名单；但调用归属、跨页审计一致性和保留数据的指标语义仍存在可定位的缺陷。STATIC_REVIEW_ONLY：结论仅基于所给 diff，未独立核验冻结基线，未运行测试或调用外部服务。

Full review comments:

- [P2] 保留迟到事件与具体调用之间的关联键 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:24-26
  `TraceEvent` 暴露 `span_id`，`CallSpan` 暴露 `dispatch_id`，但两者没有共同的调用级标识。同一 attempt 有多个 dispatch 时，消费者只能知道 `late_response` 属于哪个 attempt，不能确定它对应哪次调用；新增测试也只验证了 attempt 成员关系。应在有权威关联的记录上投影共同标识，并用同一 attempt 的两个 dispatch 验证不会错配；不要按时间猜测。多 Object 场景本身就要求区分多次模型请求，见 [AGENTS.md:60](AGENTS.md#L60)。

- [P2] 在调用视图中保留 operation_node_run_id — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:49-51
  `selected` 已读取 `operation_node_run_id`，但最终查询只返回 `i.node_run_id`，并用后者查找 `node_id`。当两个 ID 不同时，调用视图会丢失已持久化的操作节点归属；`parent_node_run_id` 表达的是步骤父子关系，不能替代它。应分别投影这两个角色，而不是让审计端从节点名称或 attempt 推断，尤其是 N05 的执行来源需要可追溯，见 [AGENTS.md:61](AGENTS.md#L61)。

- [P2] 区分事件高水位与实时执行状态 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:10-15
  `events()` 的续页沿用首屏的 `through`，却重新读取当前的 `task_status`、`execution_status` 和 `ended_at`。例如首屏之后执行完成，续页可以显示 `completed`，而完成事件被同页的高水位排除。这样拼出的审计记录不属于同一个时点。应让摘要与 `through` 对齐，或明确把摘要标为实时数据并返回其自身的高水位。

- [P2] 让调用和工具步骤续页能识别并发更新 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:43-47
  `calls()` 的游标按 intent 的 `(recorded_at, dispatch_id)` 推进，而不是按 receipt 推进。若首页返回的 dispatch 随后收到迟到回执，续页不再读取它，却可以读到该回执之后新建的 dispatch。`tool_steps()` 按 `child_attempt_id` 翻页时也会漏看已翻过步骤的 completion。`REPEATABLE READ` 只覆盖单页；若要支持完整审计遍历，应固定涵盖回执和完成记录的读取版本，或检测版本变化并要求重新读取。

- [P2] 按已报告的测量值去重，而非整条优先采用普通回执 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:115-117
  `physical_results` 只要求 `outcome` 非空，然后优先保留普通回执。如果普通回执有失败 outcome 但没有 token usage，而同一 dispatch 的迟到回执带有实际 usage，后者会在聚合之前被丢弃，导致已报告 token 的总量和观测数都少计。应对每个方向的 usage 分别选择一份实际报告值，对耗时选择一组完整起止时间，同时保持每个 dispatch 每项测量最多计一次。

- [P2] 不要把可回退的保留数据统计导出为累计 histogram — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/prometheus.py:182-185
  耗时桶、`_count` 和 `_sum` 每次都从当前保留行重新计算。清理一部分历史行后，这些值可以下降，但经典 Prometheus `histogram` 的桶和计数是 counter；`rate()` 会把下降解释为计数器重置，进而扭曲延迟分位数。应使用持久单调累计值，或把保留窗口的桶、总数与总和另命名为快照 gauge。

- [P2] 用实际投影所需的列验证读取就绪状态 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/queries.py:249-250
  `READINESS` 对每张表只选择常量并引用 owner 两列。即使读取角色无权读取 `dispatch_results.record`，或部署的表尚无 `send_admitted` 列，这些探测仍可成功，使 `/readyz` 返回 200，而实际 `/metrics` 查询失败。可以用不读取业务行的零行投影检查真实查询所依赖的列和权限，无需扩大数据库授权。

- [P3] 按标签稳定排列同一指标族的样本 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/prometheus.py:197-198
  这里只排序指标族，族内样本沿用数据库返回顺序，而 `COUNTS` 和 `HISTOGRAMS` 的最终查询都没有 `ORDER BY`。同一快照中有多个状态或节点时，样本值完全相同也可能生成不同字节，违反新增契约测试对两次 `metrics()` 输出逐字节相等的断言。应在渲染前按 family 和标签排序输入行，同时保留每个 histogram 内的桶顺序。

- [P3] 按 HTTP 规则不区分大小写地解析 Bearer 方案 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/observability/serve.py:146-147
  当前比较把认证方案和 token 拼成一个区分大小写的字节串，因此持有正确 token 的客户端使用合法的 `bearer` 方案也会收到 401。应不区分大小写地识别字段名和认证方案，只对 token 原始字节执行 `compare_digest`，并继续拒绝重复认证头。

- [P3] 用实际导出的指标名断言缺失 token 不会被补零 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/unit/test_wp07_observability_render.py:29-29
  测试检查的是 `jimu_reported_tokens`，实际注册的是 `jimu_retained_reported_tokens`；前者不是后者的子串。即使渲染器错误地输出了真实指标名和零值，这条单元断言仍会通过。应检查实际注册的指标名，以区分“没有报告”与“报告为零”。