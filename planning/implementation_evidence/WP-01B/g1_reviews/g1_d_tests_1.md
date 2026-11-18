`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。本轮只读，无文件写入、安装、测试或 Git mutation。实际基线为 `main@51a10416d882fe85e5a1cfc5440eac6498fc48f4`；工作区只有已知 planning untracked 目录。历史 324 passed 仅是 WP-01A 证据。

已实际读取最新授权附件、AGENTS、PLAN/FINDINGS/REVIEW、四规格相关条款、A 的基础实现/测试及历史验收记录。

**G1 审查结论：现有 PLAN 必须修订后才能作为本轮验收合同；最新授权已经消除了若干旧“未授权”阻塞，不能继续沿用历史结论要求重复授权。**

| 项目 | 证据及 Challenge | 收口要求 |
|---|---|---|
| D01 Policy/Producer | `specs/01_Runtime_Domain_Model_V1.0.md:1167` 要求 Applicability.condition_ast；`:1174` ToolSkip 支持 condition 或 literal true；`:1021` Producer 支持 always 或 typed condition。`FINDINGS.md:19` 明确仅 true/always 不代表旧完整条件能力。新授权要求完整 Policy 数据合同，但仅批准简单声明式能力，禁止 Rule AST。 | 冻结“完整16分支模型”与“允许的简单条件集合”两个独立清单；复杂表达式明确拒绝，不能重命名 AST、Any、空字典或自由字符串。若需要删减既有已批准条件语义，属于用户业务裁定，不能技术性宣布兼容。 |
| D02 JCS/child digest | `spec01:940–1010` 已定义 projection/input/child-first；`spec02:65–71` 禁父摘要入 child。新授权明确允许局部规范化与 digest。 | 旧 `FINDINGS.md:38–49` 的范围审批门禁应标历史、按本次授权收口。必须指定 canonicalizer、数值边界、独立 golden vectors；不得只测两次输出相同。 |
| D03 Authority | `spec01:1076–1087` 明确 whole batch / step attempt、revision、禁止 latest。`FINDINGS.md:59–72` 仍缺精确 system-output 映射及 eligibility/reason 关系。 | 冻结 typed batch/step/system-result 关联输入、Attempt ownership、可选权资格和完整输出状态集合。有限 synthetic catalogue 能验证一致性，不能证明真实调用、CAS 或持久历史。 |
| D04 Mapping | `FINDINGS.md:82–89` 缺 source/target/path/optional 环境；`spec01:1049–1055` 区分 payload/evidence、状态与值。 | 输入、输出、Context Mapping 都必须同时解析 source 与 target schema；叶类型相同不足以证明可映射，需保留 optional 祖先、范围、枚举、单位/语义约束。 |
| 新授权差异：DAG | `FINDINGS.md:99` 把完整 DAG acyclicity 后移 WP-02；最新授权 G3 明确 ToolPlan 是确定性 DAG。 | B 必须进行纯本地 cycle 校验，并验证 dependency/source step 先于消费者；完整 Bundle closure 仍留 WP-02。 |

**必须冻结的验证矩阵**

以下是可落入计划测试文件的验收断言；不是已执行结果。

| 测试组 | 必须接受 | 必须拒绝 / 回归 |
|---|---|---|
| 全部根/Policy | 每个持久根显式 schema_version；16 Policy 每分支最小完整 fixture；Python/JSON 往返；深不可变 | 每必填字段逐一缺失、额外字段、跨分支字段含 null、未知 Policy kind、未注册/错 subtype ref、隐式行为默认 |
| Policy phase | 已冻结简单声明式配置；RulePolicy 等纯策略数据 | 任意表达式、代码字符串、自由操作符、N06 Candidate/Decision 输入、伪装成新名字的 Rule AST；不执行策略求值 |
| 独立 JCS | 独立发布的固定 bytes/hex 预期向量；Unicode 属性排序；字符串控制字符；数字指数阈值、负零、极小/极大有限数 | NaN/Infinity、孤立 surrogate、非字符串 key、duplicate JSON key、不可无损数字；期望 bytes 不得由被测实现现场生成 |
| Projection | Record/enum 源顺序变化不变；父 ID/version/metadata 变化 child 不变；generation spec 改变可改变 child | 父 digest/$ref 入 child；任意 schema 注入；超深/递归/重复成员；生成输入省略上界与显式上界被混同 |
| Digest | input hash→`$id`；独立 child SHA-256；FactSchema 精确 child ref | 把 input hash 当 child hash、供给错误 child bytes/ref、父子自循环、父 hash/Bundle root 混入 B |
| 数值层次 | A 的 False/0/0.0 区分原样保留；Schema 与严格领域阶段分别报告 | 为 JCS 改弱 A；将 JSON Schema 接受 `integer:1.0` 或 `number:1` 当严格领域通过。既有回归依据 `tests/contract/test_json_schema.py:185–194` |
| Facet/Question | primary schema 精确在所有 Facet allowlist 交集；Profile 声明所有 Facet；Question 一个 primary | union-only、同 shape 异 digest、缺 Facet/context、scope/unit/semantic mismatch、反向 Profile membership |
| Mapping | source/target exact schema+静态 Record path；optional→optional 合法；约束兼容且单位相同 | source/target 未知、错 digest/kind、同叶类型但 optional 祖先不安全、宽 enum/范围→窄目标、多个 mapping 写同目标、父/子目标重叠、动态索引/JSONPath、禁止源域 |
| ToolPlan | 空/单步/分叉/汇合的已批准 DAG；合法 upstream Fact 输入 | self/两步/长环、重复 step/dependency、悬空 dependency、未来/非祖先 step output、N06 输入、未映射必需 input、输出 alias |
| Producer | Question 1:1；Tool step 1:N 唯一命名输出；system execution scope；不同 Profile 的 object key 合法重复 | Question 多 primary、Tool execution-scope Fact、相同 collision domain 多 producer、未声明输出、ID/version 取 Attempt 身份 |
| Fact/Scope | 五状态完整 fixtures；known typed value；non-known structured reason；object/execution scope 分支 | known 无值/null、non-known 含 value/null、跨 Object/Profile/Execution、未知 Schema/Producer、错 catalogue/Attempt、tool failure→false |
| N04 authority | 同一个被选 Attempt 的整个 effective Question 集；可信 envelope 下 error/unknown 与合法 siblings 同批保留 | 跨 Attempt cherry-pick；仅选 known sibling；重复/缺 Question；foreign NodeRun；错误 result kind；complete 但不可信 batch |
| N05 authority | 每 step 独立选 Attempt，映射输出状态完整；合法不同 step 选不同 Attempt | 同 step 混 Attempt、未声明 Fact、漏必需输出被标正常、缺 output state 被推断 unknown、grounding-origin 产 N05 Fact |
| Required/optional | optional Question 未入 effective set 可不产 Fact；已入 effective set 后仍恰有一个 primary | 把 optional Question 当有效批次可随意缺项。依据 `spec01:1029`、`spec03:229–235` |
| Attempts/late | 显式选择旧成功 Attempt；新候选不会改变 pinned revision；追加 revision 正确 supersede | 无 selection 默认 latest/最高 confidence；timeout/failed/cancelled 被自动标 validated_success；late result 改历史选择；manual_review 绕过 schema/trust/归属验证 |
| ID/history | 同 ID 同内容去重；不同 Attempt 保留；旧快照不随新注册/选择变化 | 同 fact_id 换 Execution/Attempt/key、同 candidate key 异 bytes、同 revision 异内容、断链/跨组 previous ref、覆盖或删除历史 |
| Registry | register/freeze/resolve/value/scope/producer/authority 全流程 synthetic 集成 | implicit latest、同 ID/version 异内容、freeze 后写、原始容器污染、缺关联输入仍报告全 PASS |
| 工程/证据 | 全 A+B pytest、Ruff、format、mypy、2020-12 schema、CPU lock/import boundary | skip/xfail/ignore 掩盖、Schema inventory 漏根、from-import 漏 cycle、未运行命令写 PASS |

Mapping 的安全条件建议使用**受限可证明兼容**：来源可能产生的全部值都在目标允许域内；无法证明就拒绝，而非自动 coercion。这是局部数据合同验证，不实施 Rule TypeChecker。optional 祖先必须来自 `ResolvedFieldType`，旧计划已识别该问题（`REVIEW.md:46`）。

**仍需业务裁定的最小事项**

1. **D01 简单策略能力边界**：现有规范并没有冻结一个“非 AST 简单条件”集合。Astra 可冻结字段、注册集合、错误码等技术细节；若实际选择只支持 literal/always 而暂不支持此前条件分支，必须明确这是经批准的分期限制。不能同时声称支持旧全部表达能力。
2. **选权资格若超出保守范围**：建议技术默认拒绝 failed/timed_out/cancelled/不可信结果作为 `validated_success`；`manual_review` 也不能豁免结构、归属与完整性。若要允许失败/迟到结果通过人工选权恢复业务结果，需要用户明确语义。
3. **system zero_or_one 无候选 Fact 的选权表示**：如使用既有 FACT result ref，空输出不能伪造 ref；可明确“无 Fact 时没有 selection，并保留 absence 输入”。若业务要求“无 Fact 仍有权威成功结果”，需定义现有结果种类与空输出语义，不能靠通用事件逃避合同。

生产 budgets、保留期限、部署 IDs、真实评测阈值不阻塞本 WP 的类型实现，可使用明确 synthetic fixtures；`spec01:1184–1190` 已允许这种测试，不能为它们另起重复审批。

后续完整验证命令应覆盖 PLAN 原列命令，并明确 Schema 生成器交付后才能运行 `--check`。记录源码/lock hash、cwd、UTC 起止、exit、stdout/stderr、JUnit 计数、skip 和 Schema inventory；最终 Runtime Acceptance 按新授权为 **N/A**。

本次结论：**G1 CONDITIONAL，验收合同修订与上述语义收口完成前不能宣布四项 closure PASS。等待主控裁决。**
