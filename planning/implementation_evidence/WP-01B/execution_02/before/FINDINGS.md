# WP-01B G1 Findings — Current Register

Revision G1-2，基线51a10416d882fe85e5a1cfc5440eac6498fc48f4。
最新实施授权已生效；旧“未获得任何实施授权”结论仅在before_planning/保留。
本轮尚未代码实施/安装/测试。STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN。
G1-D01–04不是历史架构D01–D15编号；后者已批准，不重复审批。

| Finding | 当前状态 | 处置/剩余门禁 |
|---|---|---|
| B-F01 / G1-D01 | C1 OPEN / USER_SCOPE_DECISION_REQUIRED | 完整Policy/conditional Producer需要condition_ast，最新禁止AST；四Sol+Primary均实际核对，不能技术裁决为兼容 |
| B-F02 / G1-D02 | 原范围阻塞CLOSED_BY_EXPLICIT_AUTHORIZATION；技术契约已列PLAN§3 | B局部JCS/生成/child digest已授权；有界canonicalizer审查、独立vectors、实现测试尚未做，不是功能PASS |
| B-F03 / G1-D03 | 原支持域授权阻塞CLOSED；C2具体字段补齐OPEN | 已补QuestionSelection锚点、arrival资格、完整输出/bytes绑定；FactSchema完整公共字段/metadata尚待技术冻结 |
| B-F04 / G1-D04 | C2字段补齐OPEN，非新增用户审批项 | SchemaDescriptor/source union/Review scope/verdict仍须具体字段化；依赖方向已改低层schema_types.py |
| B-F05 | UPDATED_SCOPE / IMPLEMENTATION_VERIFICATION_PENDING | B纳入纯ToolPlan DAG周期/可达性；RuleSet内容/完整Bundle闭包仍后续，缺资料不得报PASS |
| B-F06 | IMPLEMENTATION_VERIFICATION_PENDING | import测试需解析ImportFrom实际子模块/重导出边；未改A代码/测试 |

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
本Goal原文明确：“如果需要改变用户已批准的业务语义，暂停Goal并请求用户决策。”
本轮将保存G1证据后按该条件停止；不把Goal标complete，也不以第一轮技术冲突标blocked。

## 技术Challenge处置原则

PLAN-2定向复查原文保存于g1_reviews/。仍需Primary完成的技术工作：
1. 列FactSchemaDefinition全部公共字段、单位/坐标、证据/置信metadata约束；不只给模型名。
2. 给SchemaDescriptor精确discriminator、raw/typed分支字段与ToolResult envelope描述；
   TypeDefinition不能单靠raw标签证明DTO shape。
3. 给InputBinding五个来源分支的准确wire、Schema/owner/path基准与target obligation；
   保持optional source + explicit block，不误称mandatory producer完整可用。
4. 给ReviewMetadata/TransitionAssessment准确identity/scope/verdict/例外与fallback分支；
   不能发明可信审批主体或真实评测通过。
5. 给metadata namespace entry/Record根/tag semantic_domain与Prompt partial变量scope等细节。

这些技术缺口不是用户必须另作业务决定的事项，尚未完成即保留C2，不把草案写成已闭合。
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
