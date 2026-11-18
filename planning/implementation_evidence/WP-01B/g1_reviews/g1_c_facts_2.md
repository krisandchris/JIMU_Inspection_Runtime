`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。已读取指定 worktree 的 PLAN-2-G1；无写入或测试。

D02 的 child inventory identity、input-derived `$id`、child digest 和 parent ref 顺序正确；D03 没有把 SystemProducer 缩为 execution-only，已有 `EXECUTION_EVENT` 的 emitted/absence 描述也可覆盖 zero_or_one。未发现必须新增 RuntimeRecordKind 的理由。

建议实施前补以下定向条款：

| 行号 | 问题及最小修订 |
|---|---|
| **118、121、134** | N04 descriptor 自带 effective Questions，尚缺外部权威集合锚点。增加有限 `QuestionSelectionDescriptor`：selection ref、execution/profile、exact effective_question_refs，以及已通过选择契约验证的资格。ObservationResult 必须与该 pinned 集合逐项一致，不能拿自己声明的集合证明自己完整。无需提前完整 N02 Runtime。 |
| **117–121、125** | “同 ref 单一 descriptor”目前只写给 System event。扩大到所有 Object/NodeRun/Attempt/Result/Fact/Selection：同 scoped runtime ref 不得绑定不同内容；重复不同内容必须报 conflict，不得构造 dict 时覆盖。Object 仍 Execution-local；Fact 保持第146行有限目录身份约束。 |
| **135–136、119** | 明确 Tool optional absence 与五态非 known 不同。结果校验 descriptor 应有 `emitted(fact_ref)` / `optional_absence(reason)`；absent 只允许 zero_or_one，禁止 Fact/value。已产生 unknown/error Fact 属于 emitted，必须进入完整 selected_fact_refs，不能当 absent 丢弃。不要新增 FactStatus，也不要把该 descriptor union 冒称原始 ToolOutputState wire。 |
| **140–142** | “全部理由只能选 succeeded”是合理的 B fail-closed 支持边界，但原规范未完整定义 manual_review/explicit_retry_resolution 对失败结果的资格。加一句：**这是 B 的保守验证资格，不宣称已批准所有后续人工恢复语义；任何扩大需显式业务政策。** 当前负例可以验证，无须因此请求新增权限。 |
| **146–148** | 原始 bytes 还必须与提交的 Fact 模型绑定：严格解析 bytes（拒绝重复对象键、非法数字/Unicode、额外字段），验证其完整 typed content 等于该 candidate，再进行 bytes 去重。否则可提交相同 bytes 搭配不同模型，伪造“完全相同交付”。明确 bytes 与模型不一致拒绝。 |
| **94–98、102** | 说明 `float(huge_int)` 溢出也在 projection 入口按路径拒绝，不抛失控异常；加入 `2^53−1 / 2^53 / 2^53+1 / 2^60 / 极大整数` 测试。若库仅安全整数范围，`2^53/2^60` 可按披露支持域拒绝，但不能说它们不能精确 binary64 表示。 |

另有两项交付前应冻结的接口细节，当前不能只靠类名实施：

- **78–79、115、212**：FactSchemaDefinition 仍应列出完整公共字段和 known/non-known、单位/坐标、证据及置信 metadata policy 的封闭类型。原 Domain §12 和总计划 §6 已要求这些；不是新增业务，但缺具体 schema 会导致 C 自行选字段。
- **146、149、230**：明确 Registry 的 conflict 验证范围是同一不可变输入目录/快照所包含的候选；新快照必须显式带入前序历史。纯有限组件不能证明调用者没有漏传历史，不能据此宣称跨所有外部存储实现全局去重。

D01 第57–74行准确保留真实范围门禁；没有以 policy ref 或 true-only 分支掩盖 AST 依赖。以上 D02/D03 补项均是授权范围内技术澄清，可由主控裁决。
