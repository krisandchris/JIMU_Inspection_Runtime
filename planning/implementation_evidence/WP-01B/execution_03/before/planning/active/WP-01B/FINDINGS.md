# WP-01B G1 Findings — Current Register

Revision G1-3，执行基线c25bef668136acc651e7313ba608ec6fdefd39a5。
最新调度修订：D01仅局部阻塞；独立技术收口/实现继续。
最新实施授权已生效；旧“未获得任何实施授权”结论仅在before_planning/保留。
历史G1尚未代码实施/安装/测试；本轮结果追加execution_02，不重写历史状态。
G1-D01–04不是历史架构D01–D15编号；后者已批准，不重复审批。

| Finding | 当前状态 | 处置/剩余门禁 |
|---|---|---|
| B-F01 / G1-D01 | C1 OPEN / USER_SCOPE_DECISION_REQUIRED | 完整Policy/conditional Producer需要condition_ast，最新禁止AST；四Sol+Primary均实际核对，不能技术裁决为兼容 |
| B-F02 / G1-D02 | CLOSED_TECHNICAL / LOCAL_IMPLEMENTATION_REVIEW_PASS | §5冻结；独立JCS向量/父子解耦/limits测试与真实代码审查完成，F1定向127 passed（含20 selector），不等于整包PASS |
| B-F03 / G1-D03 | CLOSED_TECHNICAL / IMPLEMENTATION_IN_PROGRESS | §5/7完整字段及A2纯历史算法已冻结并Challenge；A1已交付，A2正在测试；完整Producer准入单独D01阻塞 |
| B-F04 / G1-D04 | CLOSED_TECHNICAL / IMPLEMENTATION_REVIEW | §2–4/6/9字段冻结；S2/M1/P1已交付并整改，V1只读Challenge已处置、开始实现；不新增用户审批项 |
| B-F05 | IMPLEMENTED_LOCAL_CHECKS / FULL_CLOSURE_DEFERRED | ToolPlan本地DAG和类型映射已测试；RuleSet内容、完整Bundle闭包不属于本子图，不冒称PASS |
| B-F06 | IMPLEMENTED / VERIFIED | import图解析actual submodule/reexport、原纯度限制已实际通过；包含于最终1455 passed |

## B-F01 最小解锁决策

原文证据：
- Domain §14 / line1167：ApplicabilityPolicy.condition_ast必填。
- line1168：QuestionSelectionPolicy.context_rules指向ApplicabilityPolicy。
- line1174：ToolSkipPolicy为typed tool_condition或explicit literal true。
- §13.1 / line1021：Producer.activation_condition为always或typed Tool condition。
- §13.3 / lines1108–1117规定表达式输入域，但没有另一个AST-free简单条件语言。
- A源码只有基础值/引用，无现成AST；最新Goal明确禁止B实现Rule AST。

不存在可同时满足“全部既有条件能力”和“完全无AST”的封闭模型。
仅列16个kind、14个模型、未解析ForwardRef、任意dict/Any、opaque string、把AST改名、
删除必填condition或默认为true，均不关闭此问题。

已通过异步用户问题提交两个具体选项：
1. 推荐：明确授权既有共享条件AST的数据模型前移B，不包含RuleDefinition/TypeChecker/Evaluator。
   这会有限改变本轮禁AST范围，不能由Primary自行批准；批准后补模型/文件/测试并定向Challenge。
2. 保持完全无AST：明确批准B条件分支支持域及验收范围缩减，后移C。
   不能把缩减交付写成原完整目标PASS；不另发明简单表达式语言。

用户尚未答复，不把推荐或等待时间当批准。其他实施授权保持有效，无需逐文件再批。
最新用户明确修订为依赖驱动的局部阻塞。D01的A/B两方案继续待用户决策，
仅PLAN X1/X2/X3阻塞；S1/S2/F1/O1/M1/P1/R1/A1按自身前置完成后继续。
D03/D04技术字段由Primary自主收口，不等待D01，不新建/重置Goal，不把局部交付当整体完成。

## 技术Challenge处置原则

PLAN-2定向复查原文保存于g1_reviews/。以下原技术待办现已在CONTRACT_FIELDS §1–9逐字段关闭，保留追溯清单：
1. 列FactSchemaDefinition全部公共字段、单位/坐标、证据/置信metadata约束；不只给模型名。
2. 给SchemaDescriptor精确discriminator、raw/typed分支字段与ToolResult envelope描述；
   TypeDefinition不能单靠raw标签证明DTO shape。
3. 给InputBinding五个来源分支的准确wire、Schema/owner/path基准与target obligation；
   保持optional source + explicit block，不误称mandatory producer完整可用。
4. 给ReviewMetadata/TransitionAssessment准确identity/scope/verdict/例外与fallback分支；
   不能发明可信审批主体或真实评测通过。
5. 给metadata namespace entry/Record根/tag semantic_domain与Prompt partial变量scope等细节。

这些技术缺口由Primary完成技术冻结；实现和验证缺陷另列下表，不能把字段冻结当代码验收。
已补入PLAN：低层共享类型位置；所有scoped ref目录冲突；N04外部selection集合锚点；
可信arrival资格；Tool required/cardinality/output state/emitted与absence关系；raw bytes与模型绑定；
duplicate keys在字典化前拒绝；有限历史检查边界；JCS大整数溢出/独立边界用例。

- JCS只处理生成input/child；A stable_json及payload数值语义保持不变。
- 准入父制品真实性与本地child再生成分别报告；不能用ContentRef格式通过冒充hash校验。
- System输出用已有EXECUTION_EVENT的具体闭合descriptor，非通用event或新kind；空输出
  有明确zero_or_one和absence原因，不能合成Fact或负证据。
- System scope按已声明scope；不从“execution Facts只能来自system”倒推出“system不能object”。
- Optional Question一旦进入effective batch，仍恰一个primary；optional Tool absence不同nonknown Fact。
- Optional source可以映射required target，但on_missing=block必须显式；schema路径不存在仍拒绝。
- 本地DAG检测是新Goal明示B工作，不再援引旧PLAN后移它。
- 原始Challenge不改写。Primary不因Sol完成而验收，后续必须直接读代码/diff/logs与实际测试。

生产分类成员、预算、评测门槛、部署值仍由配置审批，synthetic fixtures不代表生产批准；
它们不构成本轮类型工程的重复审批要求。

## execution_02 实施审查登记（当前，取代上文历史阶段状态）

| 编号 | Finding | 当前处置 |
|---|---|---|
| I01 | OptionalContract重验证丢弃注入extra | S1修复，9回归；133 targeted passed，Primary已直接复核 |
| I02 | S2初次9个新文件误写原main worktree | Sol仅移动自己文件，170保护文件byte-identical无新增；原始失败及纠正证据保留，不能声称从未写入 |
| I03 | descriptor diagnostic prefix上限/Region owner异digest冲突 | S2定向整改与独立复核完成；最新88 targeted passed |
| I04 | JSON UTC嵌套模式/数字字符串被当epoch | 共用UTC lexical+strictdatetime修复，300 targeted passed（重叠S1/A1）；Primary读实际source/tests |
| I05 | ToolPlan required输出被强制全映射、遗漏完整plan_ref | 依批准声明子集语义修复，required只约束mapped输出；禁止伪造步骤 |
| I06 | P1 Prompt重复目录顺序依赖、unknown窄Review遮蔽已知覆盖 | 先18失败后140 passed，独立Sol代码/日志/hash复核关闭 |
| I07 | M1 unused/空Plan context绕过owner/step检查 | 先8失败后43 passed，独立Sol probes/hash复核关闭 |
| I08 | M1缺失FactSchema被一律报告invalid | Primary提出；typed report修复后50 targeted passed，独立复核关闭，保留code/path/message/refs |
| I09 | V1模型Prompt缺Question owner、嵌套catalogue、缺validation_at | 独立Challenge采纳，§9冻结；V1定向测试/审查中，未标整包PASS |
| I10 | P1 Prompt/Model/Vocabulary/Review异hash被误当缺失 | Primary提出，6失败复现；156 targeted passed及独立code/probe/hash复核关闭 |
| I11 | V1独立response pin、source digest先检及诊断overflow保留语义 | Primary提出并整改；V1最终76 targeted passed，已实际读最终代码/回归 |
| I12 | Question/partial/孤立Prompt响应Schema漏解析 | 独立Sol复现；逐Prompt exact descriptor解析修复，6负向回归及独立hash/日志复核关闭 |
| I13 | 全mypy诊断path annotation跨模块不一致 | R1改共享IssuePath，51 targeted及全量40源码mypy通过；最终1455 passed，旧R1seal保留 |
| I14 | 全目录format命中3份封存audit脚本 | 原失败保留，历史字节不改；维护代码格式范围src/tests/scripts，Ruff lint仍全目录，不能写全目录format PASS |
| I15 | E1/首轮整合测试期间其他writer变更导致快照不稳定 | 保留并发污染与首轮source-change记录；串行静止窗口E1最终35 passed，整合最终1455 passed且240文件无差异，不将旧日志充当最终字节证明 |

以上都是局部技术整改，不改变D01业务范围选择、不批准任何AST。模块测试计数存在交集，
不得相加冒充全量测试数。最终完整已实现A+B回归1455 passed/0 skipped；137份Schema索引验证通过，240测试文件摘要稳定。
当前未解决范围问题仅G1-D01，X1/X2/X3尚未实施；完整WP验收仍未通过。
