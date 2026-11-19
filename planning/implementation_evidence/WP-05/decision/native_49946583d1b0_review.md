可从 diff 确认：mandatory 路径按 Bundle 成员枚举，不以 Candidate 为门；`collect_all` 保留所有实际匹配，blocked guard 也不会被 TRUE predicate 覆盖。但输入、继承 trace 和 Candidate 的独立来源校验不足，重建相等不能证明 Decision 来自指定 Bundle 下被接受的那次 N06 调用。STATIC_REVIEW_ONLY：结论限于用户提供的 diff；本地沙箱未能读取仓库，未独立核验冻结基线，未运行测试。

Full review comments:

- [P1] 独立核对被重建的 N06 输入与 attempt — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:944-946
  `validate_decision()` 从待验消息本身取得 envelope、Object 和开始时间，再用它们重建消息。这样验证的是“这份输出与它自报的输入一致”，而不是“它回答了调度器接受的那次 N06 调用”。同一 Execution 中另一份合法且自洽的 attempt 消息没有独立的期望值可供区分。应从受信来源取得预期 attempt、Object 和完整 upstream pins，先比对再重建。[AGENTS.md:93](AGENTS.md#L93)

- [P1] 从独立保存的模型响应验证 Candidate — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:936-939
  未显式传入 `candidate` 时，重建使用待验 prediction 的 categories、failure code 和同一消息中的 invocation。即使原响应对应类别 A，只要消息改为同一 Profile 下合法的类别 B，并同步更新输出 pin，重建仍会重复 B；类别白名单不能证明模型实际返回了 B。应从独立持久化的响应或已验摘要恢复 Candidate，而不是从待验输出构造期望值。[AGENTS.md:90](AGENTS.md#L90)

- [P1] 将 Candidate 调用身份绑定到 exact Bundle 和请求输入 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:181-185
  身份比较核对了 attempt、Object、purpose 和 `binding_ref`，但没有对照固定 Bundle 的 ModelBinding 核对实际 provider/deployment，也没有核对该 invocation 所用请求的输入摘要。若运行记录带着正确的 binding 引用，却使用了另一部署或另一组输入，这组比较无法区分；之后 prediction 还会被盖上当前 envelope 的 Bundle digest。应验证允许的实际部署（含显式 fallback）以及本次 Candidate 请求实际使用的输入。[AGENTS.md:122–123](AGENTS.md#L122-L123)

- [P1] 在构造 N06 输入时核对 Fact 和 selection 的内容 pin — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:297-301
  `_new_decision_input()` 对 Snapshot、Gate、Object 调用了 `check_input_pin()`，但对 Fact 和后面的 authority selection 只检查 ID 是否存在，随后就把引用写入 `DecisionInput`。同 ID 不同内容不能被这一步发现，ID 存在也不能证明 Object-scope 记录属于当前 Object。应在输入边界逐条验证记录摘要和适用作用域，而非依赖某条 Rule 最终是否进入 guard。[AGENTS.md:101](AGENTS.md#L101)

- [P1] 继承 Profile trace 前核对其完整执行 pin — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:814-816
  此处只核对 trace 引用在 Gate 列表中、真值为 TRUE、Snapshot digest 相等。Profile trace ID 的生成不包含 Release 或 Bundle；而 `extend_trace()` 会复制父 trace，`scan()` 还会把父 trace 的 Release、Bundle 和 Snapshot 引用写入 RuleSet 记录。因此，同 ID、同 Snapshot digest 但 Bundle 不同的 trace 能通过这里的检查，子记录与使用 envelope pin 的 Decision 将出现分歧。应验证所选 trace 的内容 pin，并逐项核对 Execution、Release、Bundle 和 Snapshot 引用。[AGENTS.md:87](AGENTS.md#L87)

- [P1] 按 selector 的声明来源提供 applicability 元数据 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/applicability.py:130-133
  `fields()` 无条件跳过 `PROFILE_METADATA`，还用 Snapshot 的 schema ref 过滤所有其他来源。只要策略声明 Profile 字段或使用独立 schema 的 Region 字段，即使固定 Bundle 中存在该值，传给 predicate 的 `context_fields` 也不包含它，FALSE 分支的 `reason_evidence_refs` 同样无法列出该字段。应按 selector 的 source 和固定 schema 解析；若不支持某来源，应在准入时明确拒绝，而非在求值时静默丢弃。[AGENTS.md:87](AGENTS.md#L87)

- [P2] 固定会影响 N06 trace 的 N01 校验报告 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/applicability.py:159-161
  `missing_diagnostics()` 读取 N01 输出的 `validation_report.optional_issues`，但 N06 只核对了 Snapshot 内容，并未把这份报告与选中的 N01 输出按摘要绑定。对于同一个缺字段的 Snapshot，把对应 issue 从 missing 改为 invalid，就能在 N06 pins 不变时改变 `reason_code`、诊断和 unresolved obligation。应固定该报告或包含它的 N01 输出，而不只是 Snapshot。[AGENTS.md:101](AGENTS.md#L101)

- [P2] 为 deterministic rebuild 固定证据读取结果 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:835-838
  `DecisionSourceSnapshot` 保存的是可再次调用的 `read_evidence`，不是原次 guard 观察到的证据可用性。如果首次读取返回 `None`、复验时同一引用恢复可读，`validate_decision()` 会重新构建 guard，原本正确的 blocked 消息可能因外部状态变化而无法重建。应保存原次读取结果及已核验摘要，把复验历史 Decision 与检查证据当前是否可读分开。[AGENTS.md:90](AGENTS.md#L90)

- [P2] 保留已求值为 UNKNOWN 的 optional Rule trace — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:509-510
  父 RuleSet 为 TRUE 时，optional Rule 的 applicability 已经执行；若结果为 UNKNOWN，这个提前返回会同时丢掉 Rule evaluation 引用、trace 和诊断。相同 Rule 返回 TRUE 或 FALSE 却会被记录，审计者因而无法区分“执行过 gate 但元数据不足”和“从未访问这条 Rule”。应保留已到达的 gate 结果，不必因此制造 mandatory obligation。[AGENTS.md:115](AGENTS.md#L115)

- [P2] 区分没有适用规则与适用规则未被选择 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:718-721
  若所有 mandatory 路径都被排除，但有一条 optional Rule 的 applicability 为 TRUE，它会以 `OPTIONAL_NOT_SELECTED` 出现在 `applicable_rules` 中。此时 `mandatory_evaluated` 和 `mandatory_gap` 都为假，最终却选择 `NOT_APPLICABLE`。这使结果与同一 Decision 的 explanation 矛盾；最终状态应区分“全部不适用”和“存在适用规则但未执行 predicate”。[AGENTS.md:77](AGENTS.md#L77)

- [P2] 统一 UNKNOWN RuleSet 的义务记录与 Decision 汇总 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:466-468
  每个 RuleSet 都会创建根 `MandatoryRuleObligation`，真值 UNKNOWN 时其 outcome 是 `UnresolvedMandatoryObligation`；但此处仅当集合含 mandatory 子规则时才设置 `mandatory_gap`。若该集合只有 optional Rule，而另一集合的 mandatory Rule 已求值为 FALSE，Decision 会给出 `NO_VIOLATION`，同时 `decision_unresolved()` 又导出未解决的 mandatory 义务。应让账本和结果使用同一套 mandatory 判定。[AGENTS.md:114](AGENTS.md#L114)

- [P2] 保留未产生 invocation 的 Candidate 失败原因 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/decision/evaluation.py:171-174
  已配置 Candidate 且收到无 invocation、无 categories、带 `failure_code` 的结果时，此分支直接返回 `None`。后续消息既没有 prediction 也没有失败诊断，与根本没有请求 Candidate 的路径不可区分。可以继续 mandatory scan，也不应伪造 ModelInvocation，但应单独保留这次调用前失败的原因。[AGENTS.md:77](AGENTS.md#L77)