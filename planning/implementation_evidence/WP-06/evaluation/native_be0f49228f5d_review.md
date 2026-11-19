所给 diff 显式约束了同一 manifest 内的跨 split 去重、holdout 样本覆盖，以及同 Profile 内按 IoU 和 ID 确定的一对一匹配。但空复核能移除既有真值，错属投票能为多个样本生成标签，漏报工具尝试能改变成功率分母，因此报告可复现并不代表其统计有效。STATIC_REVIEW_ONLY：本地沙箱未能读取仓库，冻结基线未独立核验；未调用外部服务，TEST_NOT_RUN。

Full review comments:

- [P1] 禁止用空复核覆盖已裁决的真值 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/truth.py:299-301
  对已有违章真值的样本，新建一份指向 `previous.pin()`、但没有投票和裁决的 `ReviewHistory`，即可通过 `revise_truth()`。`reviewed_entry()` 会返回 `unlabeled`，第 300 行随后用它替换原条目；同一批预测重新评分时，该违章就从召回率和错误放行率的分母中消失。应将“开启复核”与“发布真值变更”分开；撤回已有标签也应有明确的人工决定。

- [P1] 将投票和裁决绑定到被复核的样本 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/truth.py:31-37
  `ReviewVote` 不含样本或复核历史的身份，`Adjudication` 也只通过 `selected_vote_id` 选票。同一张票和同一份裁决可以原样放入样本 A、B 的两份历史；两份历史各自计算正确的 `review_ref` 后，都能产生 `labeled` 条目。检查外层历史摘要只能证明两份封装各自未变，不能证明人分别复核过两张图。应在记录投票时绑定数据集、样本和复核身份，并让裁决引用该历史中的确切票据。

- [P1] 按标注时的 Profile 目录解释完整阴性标签 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/contracts.py:174-177
  `present_profiles=()` 被定义为“目录内所有 Profile 均不存在”，但 `TruthSnapshot` 没有固定这份标注所覆盖的目录。`_check_inputs()` 只把协议的 Bundle 与预测比对，因此旧真值可以与新增了 Profile B 的协议及预测一起评分；`_sample_score()` 仍会给出 `target_present=False`，把未标注的 B 当成已确认不存在，并将样本纳入有真值的 `NO_HITS` 分母。应固定标注目录的身份或覆盖范围，而不必禁止仅调整 IoU 阈值的重评。参见 [AGENTS.md:79](./AGENTS.md#L79)。

- [P1] 拒绝没有执行依据的肯定违章判定 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/contracts.py:306-311
  这里仅对 `NO_VIOLATION` 检查发现、定位和完成状态。`discovery=NOT_EVALUATED`、`profiles=()`、`decision=VIOLATION`、`auto_cleared=False` 的组合不会被这些检查拒绝；若任务真值为阳性，`_summarize()` 仍将它计为真阳性。应校验肯定判定与实际 Decision、对象及 Rule 评估的归属，不能让未执行发现的样本提高召回率。参见 [AGENTS.md:62](./AGENTS.md#L62)。

- [P1] 逐样本核对 Execution 实际使用的输入摘要 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/contracts.py:274-278
  `execution_source()` 连接 Task 与 Execution 时只比较 Task ID、Release ID 和 Bundle digest；后续图像检查只验证 `task.media_ref.sha256`，而 `ExecutionCohort.from_execution()` 明确排除了 `input_digest`。因此同一 Task 和 Release 下，Execution 所声明的输入摘要与样本预期输入不符时，评分路径只是把该摘要抄进报告，未发现错配。应按运行时输入契约重算或独立固定每个样本的预期输入摘要，而不是将它等同于图像 SHA。

- [P1] 按所属 Profile 校验对象的 Question、几何和 Rule — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:341-345
  `_Catalogue.check_object()` 只查协议级全集，不接收对象所属的 Profile。若协议含 A、B 两个 Profile，B 专属的 Question 可以同时出现在 A 对象的标注和预测中；只要值相同，报告就会为这个不适用于 A 的 Question 增加一次 `correct`。应按固定 Bundle 中各 Profile 的有效绑定校验，而不是只检查引用在全局目录中出现。参见 [AGENTS.md:58–61](./AGENTS.md#L58-L61)。

- [P1] 用冻结的工具尝试清单校验成功率分母 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/contracts.py:373-377
  工具记录只逐条检查 `execution_id` 和 `tool_ref`；少报一条记录不会触发任何检查，空集合也直接通过。一次成功、一次失败的两条已分派记录，若漏掉失败行，`tool_success` 就从 `1/2` 变成 `1/1`，而 Task、Execution 和 cohort 均可不变。应与独立固定的 ToolExecution 清单对账；若无法证明完整性，不能把已提供行的成功率作为完整试验结果。参见 [AGENTS.md:61](./AGENTS.md#L61)。

- [P2] 对 ModelInvocation 清单做双向核对 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/contracts.py:326-329
  `<=` 只保证 Execution 引用的调用都在快照里。额外行只要 ID 不重复、Execution 与配置检查通过，也会进入 `SampleScore.models`，并可增加 `model_version_unavailable`。若 `model_invocation_refs` 是完整清单，应检查集合相等；若它只列部分尝试，应另用完整尝试清单核对，而不是把所有附加行默认为本次统计成员。参见 [AGENTS.md:93](./AGENTS.md#L93)。

- [P2] 保留违章类别以区分类别误判与正确命中 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:594-596
  这里的 TP 只要求“任务真值为阳性且预测了任意违章”。当真值是类别 A、预测是类别 B 时，二元的“是否存在违章”确实命中，但当前契约无法同时记录 A 的漏判和 B 的误报；按 `RulePath` 的比较也没有提供任务类别真值。应保留现有二元指标，并用固定的类别引用分别统计分类结果。参见 [AGENTS.md:62](./AGENTS.md#L62)。

- [P2] 区分已证实漏检的下界与有真值的漏检率 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:660-664
  若所有 `NO_HITS` 样本都没有目标真值，`no_hits_target_miss` 会报告 `0/n = 0.0`，而紧邻的 `no_hits_labeled_target_miss` 才返回不可计算的 `0/0`。前者只能表示“全体无命中样本中已证实漏检的比例下界”，不能解释为零漏检。应在契约中将其明确命名为下界，或仅把有真值的比值作为漏检率输出；保留无真值计数。参见 [AGENTS.md:79](./AGENTS.md#L79)。

- [P2] 在匹配报告中保留未匹配的预测对象 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:550-553
  `_matches()` 确实实现了一对一匹配，但报告只遍历真值对象。一个真值框对应两个重叠预测框时，第二个预测不会重复算 TP，却也不会作为未匹配预测留下；在已确认无目标的样本上，错误预测框的数量和 ID 同样丢失。应保留未匹配预测的 ID 和计数，仅在标注确认完整时将它们计为误检，避免把无真值也当成误检。

- [P2] 在比较前按固定 Fact Schema 验证值 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:392-394
  `_fact_check()` 对 `KNOWN` 只比较两个通用 `Value` 是否相等。若布尔 Question 的标签和预测都携带同一个合法的字符串型 `Value`，全局引用检查会通过，相等比较还会将其计为 `correct`，尽管两边都不满足该 Question 的值契约。应在进入准确率统计前，分别按对应的固定 Schema 验证标签和预测。参见 [AGENTS.md:60](./AGENTS.md#L60)。

- [P2] 核对归因证据是否属于所指样本 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/review.py:65-69
  `validate_source()` 验证报告 pin、scope 和样本存在性，但不核对 `self.evidence` 的归属。指向样本 A 的归因即使带入样本 B 的 EvidenceRef 和真实摘要，也会通过，`revise_attribution()` 还会接受该归因的后续修订。应把证据引用和摘要与所指样本的 Execution 证据清单核对；独立的人工附件应有单独、明确的来源类型。

- [P2] 防止修订链复用已有版本号 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/truth.py:61-64
  这里只排除了与直接前驱版本相等的情况，因此 `append_vote()` 可以生成 `1.0.0 → 2.0.0 → 1.0.0`，让同一 `(record_id, version)` 表示两份不同内容。完整 digest 仍可区分它们，但按版本查询不再唯一；`TruthSnapshot.prior` 和 `BadCaseAttribution.supersedes` 也使用同样的“不相等”检查。应统一拒绝版本回退或通过修订登记保证历史版本不被复用。

- [P2] 反序列化报告时核对明细唯一性和摘要计数 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/evaluation/scoring.py:289-290
  `score()` 生成报告时会从明细计算摘要，但公开的 `EvaluationReport` 将 `samples` 与 `summary` 作为互不校验的输入。复制一条 `SampleScore` 而保留原摘要，或只修改 `summary.samples`，仍可形成字段类型合法的报告并获得新的 pin；归因端的 `EvaluationReport.model_validate(report)` 不能发现这种矛盾。应在报告读入时至少验证样本、Task、Execution 的唯一性及摘要与明细的一致性。