`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。PLAN-2-G1 已解决原 DAG、optional Mapping、system absence 及最新授权范围问题；D01 保持 OPEN 正确。撤回我先前“SystemProducer 必须 execution-only”的额外限制，现有规范不足以支持该限制。

仍建议以下 **3 项必须修订**，均为技术可验证性补齐：

1. **迟到资格缺少明确输入字段。**  
   `PLAN.md:117` 的 Attempt/NodeRun descriptor 只明确 number/status/ownership，`:140–142` 却要求拒绝所有 late Attempt。仅 `succeeded` 不能证明结果没有迟到。应明确结果到达/终结关系的类型化输入，例如结果到达时间及可信 terminal/deadline 信息，或可信目录提供的封闭 `arrival_qualification`，并规定缺少资格信息返回 incomplete/error。加入“Attempt 为 succeeded 但结果被标 late”和“资格字段缺失”负例，不能仅用 timed_out fixture 覆盖 late。

2. **原始 bytes 入口必须明确拒绝 duplicate JSON keys。**  
   `PLAN.md:146–148` 写“严格解析”，`:102–103` 的 JCS 负例尚未明确 duplicate keys。普通 JSON decoder 会先覆盖重复键，随后 Pydantic/JCS 无法发现。应明确在任何字典化之前拒绝重复键，包括嵌套重复；规范依据 `specs/02_Runtime_Bundle_Specification_V1.0.md:110`。同时明确原始 bytes 去重入口拒绝非法 Unicode/非有限数字；不要只在重序列化阶段发现。

3. **生产端 required 输出与完整状态集合应消除歧义。**  
   `PLAN.md:135–139` 的“required必须完整”“optional absence”还应直接绑定到 Tool 声明及 Producer cardinality：
   - `required=true` / `exactly_one`：恰有一个候选 Fact，允许合法 non-known Fact；没有 Fact 必须拒绝正常权威结果。
   - optional / `zero_or_one`：候选 Fact 或显式 absence 二选一，禁止同时存在。
   - 每 mapped output 必须有唯一完整状态；该状态、FactStatus、payload 路径及 absence 原因必须一致。
   - Tool 声明 required、Producer cardinality、step mapping 的配置矛盾在有限目录校验时拒绝。  
   
   这可避免不同实施者把 required 理解成“必须 known”，或把 optional 理解成“不需要 output state”。依据 `specs/01_Runtime_Domain_Model_V1.0.md:1030`、`:1039–1059`。

另有一个**非阻塞措辞建议**：`:140` 的 `validated_success` 表示 Attempt/结果合同验证资格，应明确“不表示所有 Facts known、无业务缺失或可解除下游 required guard”，与 `:138` 保持一致。

D01 未获批前，当前计划没有授权任何共享条件 AST 实施；`:33`、`:57–74`、`:222` 的边界清楚。其余未发现必须新增业务审批的修改。
