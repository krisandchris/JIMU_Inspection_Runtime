# WP-05 Decision 原生代码复核处置

审查输入为本目录 `native_49946583d1b0.patch`，审查报告为 `native_49946583d1b0_review.md`。报告当轮为 `STATIC_REVIEW_ONLY`。以下按原顺序记录整改和验证。

| # | 处置 | 定向验证 |
|---|---|---|
| 1 | `validate_decision` 强制由调度器提供已认领的 envelope、Object 和 Attempt 开始时间，再按实际持久输入重建。 | 同一 Execution 的自洽消息换 Attempt 时拒绝。 |
| 2 | 发送过的候选必须提供独立模型证据。Worker 从实际归档回执和 normalized bytes 解码，按固定 Binding 检查类别和分数。 | 同一个合法类别被加到候选且重新组成匹配 pins，仍因实际响应为空而拒绝。 |
| 3 | Worker 复核候选逻辑请求和 DispatchIntent。N06 检查签名 Release 固定的主 Provider、部署、可用版本及适用的显式 fallback 路由。 | 合法 Object、Attempt 和候选 Binding 下的未固定实际模型版本被拒绝。 |
| 4 | Fact 和 Authority Selection 的 ID、Execution/Object/Profile 归属及内容摘要在创建 DecisionInput 时验证。 | 同 ID 异内容的 Fact 和 Selection pin 被拒绝。 |
| 5 | Pre-N02 的精确 Profile trace 作为 N06 上游 pin，重核 Execution、Release、Bundle、Snapshot、目标及 Gate 来源。 | 同 ID 异内容的 Profile trace pin 被拒绝。 |
| 6 | Context Snapshot、Region 和 Profile 选择器按各自已签名 Schema 注册解析。Region 可采用有来源证明的 Snapshot 值。缺失来源保留 UNKNOWN。 | 签名场景覆盖 Context T/F/U 及 Region/Profile 同名 Task 字段冲突。 |
| 7 | N06 从实际 N01 输入、签名 Region 和受信 Task 元数据重算已固定的 Context 输出及 optional issue。 | 仅修改 N01 报告、保持 Snapshot digest 的尝试被拒绝。 |
| 8 | Worker 将第一次读取缺口固定在同一 Claim 内。已经成功读取的证据复验时再次验证原摘要和长度。若不再可用则拒绝提交该次 N06，保留历史 Decision guard 的原始结论与证据引用。 | 本地纯计算用例确认可用性改变不能静默改写结果，Worker 的恢复字节故障注入由集成测试核验。 |
| 9 | 当前 RulePolicy 的扫描模式为 `all_mandatory`。无已配置的 optional Rule 选择时，直接不运行其 gate 和 condition。 | 仅含 optional Rule 的组不会产生虚构的子级 trace，并保留实际运行过的 RuleSet trace。 |
| 10 | Object 的适用性结果基于已配置 mandatory Rule 和全部 RuleSet 账本。 | 无适用 mandatory Rule 且账本确定排除时可产生 `not_applicable`。 |
| 11 | RuleSet 自有 UNKNOWN 义务参与 Object 汇总，包括仅含 optional Rule 的 RuleSet。 | 同一 Object 的另一个 mandatory Rule 为 FALSE 时结果 uncertain，为 TRUE 且 guard 完整时保留 violation。两种均保留部分覆盖。 |
| 12 | 无实际调用的候选省略在内部 DecisionMessage 保留有界 `candidate_diagnostic`，继续完整 mandatory 扫描。多重候选绑定也保留独立诊断。 | 有两个已签名候选 Binding 时阻止调用任何模型，保留确定命中和重开后的诊断。 |

终态校验还核对决定性排除祖先的同一 Object、RuleSet、Attempt、原始 ContextSnapshot、子轨迹的确切父级前缀和四个 N06 输出摘要。覆盖层接受在实际成功的 N03 Attempt 中、针对原 N02 hit 经 journal 认证且始终缺少合格 producer 的单 Profile 缺口。内部工作记录保留 `not_dispatched`、`sent_unknown` 或 `acknowledged`。相邻 Profile 的合法定位保留。`test_wp05_decision_grounding_gap.py` 覆盖三种边界及错配、重复和缺失见证。

Ruff、strict mypy 和公开 Schema 的逐字节比对通过。定向与兼容回归共 159 项通过，见 `after_native.log` 和对应的 JUnit XML。模型质量、目标 GPU 和生产检查为 `RUNTIME_NOT_RUN`。
