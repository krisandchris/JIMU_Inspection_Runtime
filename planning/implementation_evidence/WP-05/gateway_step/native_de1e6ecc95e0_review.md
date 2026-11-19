补丁没有充分区分真实派发、证据留存和父节点发布三个状态；同时，新校验会使部分旧记录不可读，恢复路径也可能把已选步骤报为未执行。STATIC_REVIEW_ONLY：结论限于所给 unified diff；本地沙箱阻止了对冻结基线的独立核验，未运行测试或调用外部服务。

Full review comments:

- [P1] 将祖先凭证的新写入要求与历史记录读取分开 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/step_attempts.py:131-132
  旧版 `_StepInput` 没有 `ancestor_inputs`。若已持久化的 `wheel_boundary_geometry` 包含必需的 `StepOutputInput`，读取时该字段默认为空，随后就被 `required <= paths` 拒绝。这样不仅不能复用该结果，`load_parent_step_history()` 甚至无法读出这条历史。应允许旧格式作为历史证据读取，但不把它当作具备新凭证的可复用结果；新派发仍应强制校验。[AGENTS.md:105](AGENTS.md#L105)

- [P1] 在写入证据和 journal 前核对回执的 dispatch ID — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge.py:1443-1447
  这里使用 `request.dispatch_id` 归档响应，却没有与回执自带的 `response_dispatch_id` 比较。如果 transport 交回另一笔并发请求的回执，其 raw HTTP 和 normalized 摘要会被归到当前请求；即使 writer 检出错配并抛错，`finally` 仍会写入原 outcome。应在两个写入动作之前核对身份，将错配记录为归属错误，而不是当前派发的成功响应。[AGENTS.md:93](AGENTS.md#L93)

- [P1] 分别记录 HTTP 派发结果与响应证据的留存结果 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge.py:1456-1461
  真实 HTTP 调用成功、但 `stage_model_response()` 写入失败时，`finally` 仍按原成功 outcome 追加 journal，而调用方收到的是存储异常。该记录与留存成功时没有区别，恢复时也不能从摘要重建缺失的响应 bytes。应保留真实派发成功的记录，同时持久化留存失败或待对账状态，使恢复流程不会把缺失证据当成已落盘，也不必为补证据重发模型请求。[AGENTS.md:134](AGENTS.md#L134)

- [P1] 恢复 N05 时先还原已选子步骤，再处理原图不可用 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/node.py:610-613
  如果子步骤已提交 selection、父 Attempt 在提交汇总前中断，下一次执行会从空 `_Round` 开始。此时只要原图读回失败，这个分支就把所有步骤写成 `UninvokedToolStep`，返回不含既有 ToolExecution、Fact 和 selection 的失败结果。原图不可用可以阻止新的派发，但不能把已发生的派发改报为未执行；应先从持久历史还原已选步骤。[AGENTS.md:61](AGENTS.md#L61)

- [P1] 不要将读回验证失败的 sidecar 作为已验证输出发布 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/node.py:802-806
  选中的 mask 或最终 geometry sidecar 缺失、损坏时，这里只追加 issue；该 completion 仍留在 `state.completions`。随后 `_completion_output()` 会发布它的 evidence、Fact 和 selection，且因已有 authority 而生成 `partial` 汇总和 `SUCCEEDED` envelope。对最终 geometry 步骤尤其明显：没有后继步骤再拦截这份输出。应保留原 selection 供审计，但让父节点明确阻断依赖该未验证资产的结果发布。[AGENTS.md:134](AGENTS.md#L134)

- [P2] 按 Tool 定义解析祖先输出名，不要使用字段路径末段 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/step_attempts.py:318-320
  `_source_field()` 按 `ToolDefinition.fact_outputs` 将 `value_path` 解析为 `output_name`，但 `_ancestor_pin()` 又要求 Fact 的 `output_name` 等于路径末段。当配置将某个 payload 字段映射到不同名称的 mask 输出时，前者能找到正确的已选资产，后者却在打开子 Attempt 前拒绝同一证明。两处应使用同一份固定版本的输出映射，并独立核对命名 evidence slot。[AGENTS.md:61](AGENTS.md#L61)

- [P2] 区分空的 physical request 与未提供 physical request — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/gateway/bridge_tools.py:485-485
  当 `physical_request` 明确为 `b""`、逻辑请求非空时，`or` 会回退到 `dispatch.request`，使 journal 的 physical digest 实际成为 logical digest。应仅在 `physical_request is None` 时回退；若空请求不合法，应明确拒绝，而不是记录另一组字节的摘要。