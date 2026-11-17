# WP-01B Planning Findings

基线：`main@51a10416d882fe85e5a1cfc5440eac6498fc48f4`。
本轮只有静态分析；不修改已批准规范/代码，所有建议均 **PROPOSED / PENDING_USER_APPROVAL**。
这里的 B-F 编号不复用 A 实施 Findings 或独立外审 F01/F02 的编号。
各阻塞项是当前 B 分期/字段完整性问题，不否定 A 已验收，也不撤销用户对 D01–D15 的批准。

## B-F01 — 完整 Policy / conditional Producer 与禁 AST 的依赖冲突

级别：实施阻塞。来源：Primary 自查及 Sol A/B/C/D 一致报告。

实际证据：

- Domain `specs/01_Runtime_Domain_Model_V1.0.md:1167` 的 ApplicabilityPolicy 必填 condition_ast。
- 同文件 `:1174` ToolSkipPolicy 的 tool_condition 或显式 literal true；`:1021` Producer activation_condition=always 或 typed Tool condition。
- `:1108–1117` 为不同 AST input_domain 的输入限制；`planning/WP-01_IMPLEMENTATION_PLAN.md:163` 将 AST 作为低层依赖。
- 当前用户明确禁止 B 实现 Rule AST、TypeChecker、Evaluator；当前 A 代码不含可复用 AST。

因此，仅增加 PolicyKind16个枚举不等于完整16个Policy模型；只接受true/always也不等于支持已批准的条件分支。Any、任意dict、字符串代码、空模型、未解析ForwardRef、把AST改名为新Condition对象，都不是有效解决方案。

可审查选项：

| 选项 | 内容 | 对原B验收/兼容的影响 |
|---|---|---|
| 1 保持当前禁止与完整目标 | B仍blocked，先完成有限契约/阶段调整审查再实施 | 不暗改范围；当前建议的安全停止点 |
| 2 显式调整前置依赖 | 单独批准将**已批准AST数据结构**提前为B前置，类型检查器/求值/RuleDefinition仍不提前；随后重审字段与B静态检查边界 | B范围变化，必须用户明示；最终wire无需新AST体系，但新增文件/测试要列清。不在当前PLAN实施范围 |
| 3 显式缩减B交付 | B只交完整非AST Policy子集及可独立类型；Applicability/ToolSkip完整分支、conditional Producer和相关语义后移 | 原“完整16Policy/全部Producer”门槛未满足，必须改阶段验收和后续依赖，不得原样标B PASS |

Primary建议：保留当前禁止，先获得分期裁定；若目标仍是一次完成原八模块，**选项2是需要审批的依赖调整候选**，不是现在的执行指令。若用户优先保持B无AST，则选择3并重写阶段验收。不会自动选择任何方案。
关闭条件：用户选定方案；PLAN/必要规格附录准确列出新增或后移模型与验证层；Sol再次Challenge，Primary复核后再请求该WP实施批准。

## B-F02 — value_schema 生成与 JCS / 内容摘要边界

级别：完整 F04 生成验收阻塞；单纯 FactSchemaDefinition 引用/输入契约可单独设计，但不能称F04完成。

证据：Domain `:940–1009` 指定 ValueSchemaGenerationSpec、JCS、独立生成输入、canonical bytes、由 input_sha256 派生 `$id` 与 child先于parent；Bundle spec `:7–8`/§4、§5把完整制品/Bundle hash与admission留WP-02。A `domain/base.py:24–31` 明确 stable_json 非JCS，`tests/contract/test_json_schema.py:1` 明确只有foundation API Schema。

Primary不把“完整Bundle摘要留WP-02”解释成一切局部SHA禁止，也不反向推定已授权B实现JCS。需要明确分配两个生成器责任：

| 候选 | B交付 | 后续与限制 |
|---|---|---|
| 1 受限局部生成（推荐待批） | 仅 TypeDefinitionProjection/GenerationInput 的规范化、既定profile的value_schema内容及其JCS字节、input hash/child hash；不生成父FactSchema或Bundle摘要，不做签名/closure/admission | 明确属于F04 CPU契约生成；需审查 canonicalizer 来源、数值精度、固定向量及依赖。新增依赖须另列有界版本审批，不默认安装 |
| 2 仅类型/结构 | B交 projection/input/Schema结构与重复性；canonical bytes、input-derived `$id`、digest全部延后 | 必须缩减B的F04验收，不能在正式V1产物中省略必需$id后冒充符合原规范；延后完整产物/相关根验收 |

选项1仍需冻结的工程字段：projection_profile_version、generator_id/version、允许global_limits、初始根版本与URN规则；固定可无损表达数值的拒绝规则及Golden vectors。不能把`json.dumps(sort_keys=True)`当JCS，也不能为canonicalizer方便收紧已验收A所有整数/number语义。A接受的TypeDefinition可在**生成边界**因不可无损表示而拒绝，须保留有路径的原因与显式测试。
Source-order Record/member规范化按Domain UTF-8顺序；JCS对象属性规范化另遵循其实际规范，不能混为一个排序函数。语义同一树在固定generation spec下产物相同，改变parent ID/version/metadata不能改变child。

A TypeDefinition已填充string/list的结构默认上界；若generation global_limits更小，必须在批准的projection适配入口明确“原始省略”与“已显式4096/256”的处理，不能丢掉字段存在性后静默截断约束。推荐通过显式 generation输入规范化后再做A类型验证，并为已构造A模型输入保留显式约束语义；确切入口签名在关闭附录冻结。
关闭条件：范围选择、字段/数值/入口规则、canonicalizer来源和测试向量均可审查；未完成前不选库、不装包、不写摘要实现。

## B-F03 — Authority 结果目标与最小 Fact / Attempt 支持范围

级别：完整 AuthoritativeResultSelection / Fact Registry.validate_fact 验收阻塞。

证据：Domain `:1076–1092` 给了 observation_batch/tool_step/system_output、group identity、selected_attempt_id/selected_result_ref/selected_fact_refs/revision等，但未给group的精确联合字段及selected_result_ref三组kind映射。A `domain/enums.py:60–86` 有 OBSERVATION_BATCH/TOOL_EXECUTION/FACT/ATTEMPT，没有system_output结果kind。Domain `:462–483`、`:1073–1088` 要求Fact/provenance/Attempt关联。B用户明确要求Authority/候选历史，但未单列完整ObservationBatch/ToolExecution等运行模型。

推荐保持Authority在B目标中，不接受把它整体后移后仍标八项完成。具体候选：

- observation_batch：group identity=(execution,profile,object)，selected_result_ref限制已有 OBSERVATION_BATCH；selected Attempt 与整个有效Question集合的primary Facts一致。
- tool_step：group identity=(execution,profile,object,tool_plan_ref,step_id)，result ref限制已有 TOOL_EXECUTION，且源为N05 tool_plan_step；选定该Attempt的全部mapped outputs。
- system_output：建议以(execution,固定SystemProducer identity/output)为组；若选择已有 FACT 作为selected_result_ref，必须明确它就是单个declared output候选、selected_fact_refs恰含该同一Fact，不能扩展为整批通用结果。zero_or_one“无Fact”是否允许形成selection、如何引用结果，**仍OPEN**；另一选择为现有EXECUTION_EVENT，但必须先定义其可信payload及关系，不能靠通用事件逃避契约。

以上是**待批准映射**，不是已批准业务语义；不会自行新增RuntimeRecordKind，也不会用泛化Ref接收所有kind。

支持范围候选（推荐一次冻结）：

1. B纳入必要FactRecord五分支，复用Value/FactStatus，显式schema/scope/producer/catalogue/Attempt/source/evidence/reason字段。FactSchema metadata/置信字段形状见B-F04。
2. 采用非持久、纯校验输入提供有限actual-result关联：result ref、execution/profile/object、node/origin、Attempt→NodeRun→Execution归属、Attempt状态及结果可选权资格、可信envelope/association状态、声明的有效Question/step输出集合、完整candidate Fact集合、已pin selection revision链。Exact字段/联合需在附录逐项冻结，不得先落Any或新运行制品。完整Question成员集合不足以证明批次可信；各selection reason的资格必须显式制定，超时/late结果不得自动标validated_success或改Attempt终态，manual_review也不由实现猜测。
3. 输入由可信调用者提供，在fixture中是明确synthetic；B只验证声称记录之间的一致性，不证明调用发生/审批可信。缺result membership/Attempt关系时返回incomplete/error，不只校验ref字符串。
4. 不实现完整ObservationBatch/ToolExecution生命周期、事务CAS、worker fence、持久event、consumer自动选权。没有selection就不解析成“最新”，已pin revision不会随新candidate自动变。

关闭条件：三组字段/kind、system无输出边界、最小Fact/provenance/reason字段、关联输入及不可变candidate冲突比较规则获批，正负fixtures可明确列出。明确fact_id的唯一域与不可变candidate映射，不能通过换Execution/Attempt/key复用同ID绕过冲突，也不能从Object局部ID约定推定Fact的同名规则；同内容判重如何对照原始bytes/版本化内容表示必须冻结，stable_json不是原始bytes或JCS证明。不存在“实现时自行选一个kind”。

## B-F04 — 字段/输入描述尚不足以冻结为严格模型

级别：对应模型/语义检查阻塞。区分业务配置值OPEN（可用合成值测类型）与字段类型未定义（不能编码猜测）。

| 缺口 / 证据 | 需要冻结的有限决定 | 推荐候选与禁止项 |
|---|---|---|
| Entity.entity_type/lifecycle；Region.region_type/scene_type，Domain:56–81 | wire type、允许值/词表权威、长度、生命周期是否状态 | 可选受约束标识+版本化词表治理或明确有限枚举；不能从示例physical_object推成全集，不能从Release lifecycle照搬Entity |
| Facet optional metadata/tags，:113 | 是否首版支持、封闭键/类型、边界 | 推荐未冻结metadata暂不支持并明确兼容范围，tags若支持用有界唯一标签；不是Any/开放扩展。需批准缩减可选字段，不静默删除 |
| ContextFieldSelector，:1136；Profile无context_schema_ref | source枚举、schema_ref wire、field_path规则、Region/Context类型输入来源 | 推荐显式注入Context/Region/Profile metadata类型环境；provenance.allowed_sources与selector source不同概念，不能直接套同枚举；不给Profile暗加schema字段 |
| ToolStep/InputBinding/OutputBinding/StepDependency，:175–179；grounding mappings:744–747 | 所有输入来源分支、字段名、目标路径基准、required/optional、条件policy所在位置；step_id/依赖唯一性 | Fact输出映射已明确定义，通用mapping未定义。候选来源须逐分支冻结；拒绝自由JSONPath、shell、Candidate/Decision、未声明上游输出 |
| Tool输出路径 :1039–1055；Schema资源 :690–696 | ToolResult.payload及evidence/response/context的有限权威类型描述、字段可选性、出错分支解包 | 推荐只读typed descriptor，来源登记到exact SchemaRef；不导入任意外部JSON Schema、不用裸路径列表证明类型。descriptor不是新增ArtifactKind |
| PromptTemplate :756–779 | named placeholder语法、重复/未声明变量、partial变量scope、template边界 | 建模已列purpose/source/on_missing；renderer/selected-set compiler不在B。静态占位符验证须先有语法，不能借运行模板库猜测 |
| ModelBinding:349–359/DeploymentRequirement:1133–1135 | logical_model_id规范字段名、constraints唯一来源、与ProviderPolicy的关联 | exact constraints复用既有VersionConstraint，避免两套矛盾可写数据；候选field placement须冻结 |
| Fallback逐transition评测，Domain:371/390/715/1177 | 有限ReviewMetadata/TransitionAssessment输入：准确source/target deployment/model版本、capability、approved scope、覆盖transition/trigger、ReviewReportRef及显式校验时刻 | 非持久校验输入，不改Policy wire、不自建审批服务；report存在但覆盖不符拒绝。未提供此输入只能报告ref形状已检，具体semantic check为deferred，不能声称真实可信审批/质量通过 |
| FactSchemaDefinition，:924–945、总计划:255 | schema_id/version等完整字段，known/non-known/证据/置信元数据政策、单位/坐标ref形状、适用时必填条件 | 推荐共用已批准Unknown/Evidence等明确契约，不能自造多套宽松policy；数值confidence不是概率校准 |
| Registry字段路径，Domain:1095–1106；A RecordFieldDefinition.required | ResolvedFieldType返回type_definition/field_path/optional祖先链的闭合字段 | 不只返回叶TypeDefinition；required叶在optional父内仍可缺失。未声明路径报错，不伪造missing值/TruthValue |
| 初始持久根与辅助类型，Domain:644、总计划:173/236 | schema_version、长度/集合上限、错误码/路径、UTC序列化、null/absent | 推荐根显式1.0.0（待批）、错误路径为有界字段名/索引tuple；有分支禁止字段时null也拒绝；不从包版本推导 |

这些字段集中在一个冻结附录进行技术复核与用户批准，不逐文件重复提问。保留所有原规范；若补充需要增加规范wire字段、删减可选能力或收紧支持域，必须列差异/兼容性并获有限修订授权。没有任何“按最佳猜测已冻结”的条目。

## B-F05 — 有限语义检查与完整 closure 的边界

状态：计划已声明验证范围，未来需由实际测试证据确认。来源：Primary +四只读审查。

Profile.rule_set_refs必须保留（Domain:96），但RuleSet/AST/typechecker不属当前B。B可检查shape/kind、明确外部pinned inventory条目是否唯一，不能声称Rule body/mandatory producer完整性已验证。无目标内容时应报告外部依赖未完成，不造空RuleSet让全部绿灯。
ToolPlan的step局部字段/重复/悬空dependency ID可按批准形状检查；总计划:346将完整DAG/可达性/映射闭包归WP-02。若希望B增加纯本地cycle检查也须在最终清单注明，不暗中建compiler。
Policy的规则/coverage/execution常量可建模，不等于Rule/Workflow/Gate实现。Provider/Fallback元数据可检一致，不等于真实独立评测或可信审批解析。
完整AST input_domain检查归C；全Bundle conditional-producer completeness归WP-02。禁止因B缺Ast上下文而把这些结果写PASS。

## B-F06 — import graph 验证需覆盖实际子模块边

状态：B测试改进提案，非本轮已修复；不改已通过A业务代码。
证据：`tests/contract/test_dependency_boundaries.py:22–37` 仅记ImportFrom.module；`from jimu_runtime.domain import producers`会漏掉实际submodule边，扩展B目录后可能漏循环。
计划：显式绝对子模块导入、解析本地模块from-import目标与__init__重导出边；用小型合成文件图验证可发现环。检查的是Python源码import AST，不是受禁的Runtime Rule AST。保留原依赖/Any/动态执行检测，不以删断言修复。

## 关闭与审批记录

当前 B-F01–04 尚无用户选择/字段冻结授权；未修改任何已批准契约。PLAN可供审查，但不能据此开始代码。B-F05/06已有具体计划措施，未执行验证。
用户需要一次性裁定三个边界（AST分期、局部Schema/JCS职责、Authority支持范围）并允许相关字段附录闭合；随后由Primary产出明确PLAN修订、Sol只读Challenge，再取得具体实施批准。D01–D15不重复审批，真实部署/预算/质量阈值仍按各自业务门禁。
