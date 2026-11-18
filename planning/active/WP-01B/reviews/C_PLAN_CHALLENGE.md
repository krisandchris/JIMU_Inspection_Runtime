# Sol Medium PLAN-1 Challenge C

Reviewed PLAN-1 SHA-256: `fe633537098c658c2fa7701fff045916ec076f928cd9e2886a817fcb6ea62417`.
Reviewed FINDINGS SHA-256: `e4306de5231e15d145d1021b55db071034eaaac161f0c5d2fd9ce3203672ca6b`.
Actual second final response, preserved by Primary; dispositions in REVIEW.md.

只读 Challenge 结论：`CONDITIONAL_STATIC_PLAN`。没有发现偷渡实施、默许AST前置或误称16 Policy已完整；B-F01选项明确待批。建议修正以下3项：

1. **中：实施顺序与模块DAG矛盾。**  
   `PLAN.md:94`规定 `producers → Tool/ToolPlan`，但`:121`先执行B.3，`:123`才执行拥有 `producers.py` 的B.5b；B.5a又未交付ToolFactOutputDeclaration/Mapping。  
   **修订：**把独立的Tool输出声明/映射及Producer数据模型明确拆成B.5a前置，B.5b仅做跨定义验证/Authority；或把输出声明/映射移到无反向依赖的共享模块。同步更新DAG、任务顺序、文件owner。不能依靠临时ForwardRef或重复模型跨过阶段。

2. **中：Provider评测元数据缺冻结入口。**  
   `PLAN.md:193、197`承诺注入 `review_metadata` 校验Provider/Fallback；`FINDINGS.md:86`只列ModelBinding/Deployment字段，尚未列typed review metadata和transition覆盖关联。Domain `specs/01_Runtime_Domain_Model_V1.0.md:390`要求每次transition的source/target/capability/scope独立评测；`:371`还要求实际trigger被显式允许。  
   **修订：**B-F04新增一行，冻结有限ReviewMetadata/TransitionAssessment输入：准确版本化deployment/model identities、capability、approved scope、所覆盖transition/trigger、ReviewReportRef和传入校验时刻。明确这些是非持久校验输入，不能修改Policy wire或冒充可信审批。若不纳入，Provider验收只能承诺ref形状并标具体semantic check为deferred。增加“report存在但不覆盖该transition/trigger/scope”的负例。

3. **低：Registry路径接口会丢失其承诺的信息。**  
   `PLAN.md:211`的 `describe_type(...) -> TypeDefinition` 无法表达`:215`要求保留的“路径optional性”：同一BooleanTypeDefinition不能说明经过哪个可选Record字段。  
   **修订：**将返回值标为待B-F04冻结的闭合 `ResolvedFieldType`（含type_definition、路径存在性/required链等必要数据），或分离显式路径描述接口；补“相同叶类型但必填/可选祖先不同”的正负fixture。无需RuleAST或TypeChecker。

其余范围门禁、Source与通用Tool mapping缺口、Schema descriptor权威、完整closure分期、原始证据与代码/运行证据区分均已明确。未写文件、未测试、未修改Git；实际Sol/medium元数据确认沿用Primary提供证据。本报告不批准实施。
