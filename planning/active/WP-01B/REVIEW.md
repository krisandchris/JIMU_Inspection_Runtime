# WP-01B G1 — Primary Review

基线main=51a10416d882fe85e5a1cfc5440eac6498fc48f4。
当前为G1契约收口审查，不是最终代码验收。
STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN。

## 实际独立核查

Primary重新读取用户Goal原文、AGENTS、实际PLAN/FINDINGS/REVIEW与摘要封套、
相关四规格、A基础代码/接口及CPU配置，直接对照必填condition、projection、producer、
authority与Mapping条款；没有仅接受Sol结论。

初始170个文件（143 tracked+27已知untracked）逐字节与工作前manifest一致，
PLAN-1.1的15个SHA256SUMS条目匹配。main仍为指定HEAD，无tracked/staged改动。
新建已获授权的独立worktree/分支，按manifest复制已知规划材料；原main不写入。
新开发分支HEAD仍为基线，没有Commit/Push。

原生四新Sol只读Challenge全部完成。实际会话turn_context核对：
Primary=gpt-6-astra/xhigh；Sol-A/B/C/D=gpt-6.1-sol/medium。
共享filesystem由实际路径读取确认；worktree隔离由Git worktree创建实际结果确认，
不再把仅接口存在写成创建验证。未改Codex配置/安装框架。

## 首轮Challenge

原文在 implementation_evidence/WP-01B/g1_reviews/。
A=Ontology/Capability，B=Model/Tool/Policy，C=Fact/Producer/Authority，D=tests。
四者均指出D01条件表达支持域不能由“完整16种kind”代替。
Primary直接核对Domain:1021、1167、1168、1174与最新禁止AST条款，接受其为真实分期冲突。
已提交最小二选一，未擅自批准范围例外。

其他意见按新授权技术细化：
- D02局部JCS/child digest范围已明确，不重复要授权。
- D03具体三种result kind、wholebatch/step、最小Fact/Attempt描述、system optional absence；
  D定向复查接受具体闭合EXECUTION_EVENT descriptor，并明确不是通用event兜底。
- D04 strict有限SchemaDescriptor与raw/typed表示、optional祖先、source-target兼容和missing策略。
- D指出旧PLAN把ToolPlan cycle后移与新G3冲突；PLAN-2改为B本地DAG检测。
- 不接受子报告中“SystemProducer仅execution”无充分依据的进一步收窄；
  保持声明scope，execution Fact仅system与N05 object-only原约束。

PLAN-2另交B/C/D定向只读Challenge，原文继续追加保存；
整改和最终摘要写入后再记录最终核查，不以草稿审查冒充最终文件通过。

## 当前验收轴

| 轴 | 状态 |
|---|---|
| G1 Contract Review | CONDITIONAL：D01用户范围裁定未取得 |
| Code Acceptance | NOT_IMPLEMENTED |
| Test Acceptance | TEST_NOT_RUN；324passed仅A历史 |
| Runtime Acceptance | N/A，本包纯Domain；RUNTIME_NOT_RUN |
| Independent Review | 已完成首轮静态Challenge；不是代码审查PASS |
| Overall WP-01B | INCOMPLETE，不能标PASS/Goal complete |
| 实施授权 | 已授权B；具体AST范围例外仍待用户 |

已执行的操作是文件/Git只读核验、原生工程子任务、授权worktree/branch创建、文档复制和修订、
RFC资料查阅及审计哈希。没有运行pytest/Ruff/mypy/项目Schema验证、安装依赖、编写B代码或调用Runtime。
首次审计命令python不存在（exit127），改python3成功；一次apply_patch因同path多操作被拒绝，
未改文件；随后一次会话文本提取未匹配（exit1）也未写文件，修正后写入PLAN。
这些是文档工具操作结果，不是功能测试失败或通过。

待用户给D01答案后，按既有整体实施授权继续冻结相关数据模型及定向Challenge，然后交Sol实现；
不需要用户重复批准每个文件。未答复按Goal的条件暂停指令保存证据并停止。

## PLAN-2 定向Challenge的实际结论

B/C/D均直接读取worktree内PLAN-2，原回复追加保存，未改写此前报告。
Primary已对照具体条款修订：

| 发现 | 处置 | 状态 |
|---|---|---|
| 共享纯类型放validation层可导致反向依赖 | 改schema_types.py低层，validation仅目录校验 | RESOLVED_IN_PLAN，代码未实施 |
| N04自带effective集合可能自证完整 | 加QuestionSelectionDescriptor作为独立pinned锚点 | RESOLVED_IN_PLAN |
| 只用succeeded不能辨别late | 显式arrival_qualification；late拒选、unresolved/missing不通过 | RESOLVED_IN_PLAN，可信输入非实际时序证明 |
| Tool optional absence与nonknown混淆 | emitted/absence descriptor、required/cardinality绑定、五态保持 | RESOLVED_IN_PLAN |
| bytes未绑定模型/重复键覆盖 | 解析前拒重复键、非法数/Unicode；完整typed content绑定 | RESOLVED_IN_PLAN |
| runtime refs构dict覆盖；历史输入漏项 | 所有scoped refs先查唯一内容；明确有限目录信任边界 | RESOLVED_IN_PLAN |
| JCS数值溢出及安全整数≠binary64全集 | 路径化错误与具体边界测试补齐 | RESOLVED_IN_PLAN |
| SchemaDescriptor/source union/Review scope尚无完整wire | 不把要求描述当字段冻结，保留B-F04 C2 | OPEN_TECHNICAL，不需新增业务审批 |
| FactSchema metadata字段/Prompt/metadata细节 | 保留B-F03/04 C2，后续主控技术补齐后再委派代码 | OPEN_TECHNICAL |

D定向承认并撤回system execution-only额外限制，原文保留。
当前最终PLAN并非无条件静态PASS；上述已处置项由Primary直接核对，未声称Sol对最终
新字节又全量复核通过。D01待用户范围裁定；其余C2由Primary负责，不转嫁用户。
尚无B代码、测试、Schema生成、依赖安装、Commit/Push；原A和WP-00历史保持原样。
