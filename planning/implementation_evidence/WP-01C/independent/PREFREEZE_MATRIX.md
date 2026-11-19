# WP-01C 冻结前独立核验表

`STATIC_REVIEW_ONLY` / `TEST_NOT_RUN` / `RUNTIME_NOT_RUN`。工作树为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。检查期间计划、产品、测试及证据目录均有并行改动。本表供分派与冻结复核。

| 门禁 | 已定位实物 | 独立复核条件 |
|---|---|---|
| G0 授权 | 当前授权表、C PLAN/PLAN_REVIEW、B 用户验收 | WP-01A 验收 README 第 11 行记录用户在 827fa77 批准 D01–D15。B PLAN 第 25 行及最终报告第 39 行重申历史授权，B 最终报告第 32–37 行记录经批准的 AST 与本地 selector 细化。C PLAN 摘要与当前授权表一致。真实例外和外部操作仍按各自授权记录核验。 |
| G1 清单完整 | CONTRACT_INVENTORY、生成器 WP01B_INVENTORY 与 WP01C_INVENTORY、test_b_schema_inventory 独立 AST 扫描 | C1-03 列明同义、嵌套、有限输入的归属。AST 差集检验已声明模型是否导出，还需 §5 语义清单和每根正负 fixture。 |
| G2 严格验证 | tests/unit、tests/contract、tests/negative 及 WP-01B/C fixtures | 冻结后全量运行，保留 JUnit 实际分组、计数、失败、错误和跳过。 |
| G3 Fact/AST | fact_schemas、conditions、rules、validation/rule_types，B registry/projection/conditions 与 C rule tests | 检验 10 个签名、三输入域、互异单位/枚举、Record 路径、AST 大小和深度、必需及可选 selector、Tool 祖先输出。 |
| G4 可重复 Schema | generate_schemas render/validate/export、test_b_schema_inventory、test_b_projection | 两次独立目录导出和字节比对，仓库生成物 --check，Draft 2020-12 本地引用和双验证。B 的 162 份文件逐个按合并树比字节，记录 C index 的有意变化。另证明 F04 child-first value_schema。 |
| G5 依赖边界 | pyproject.toml、uv.lock、test_dependency_boundaries、test_b_dependency_graph、CPU setup 原始日志 | 记录冻结环境、解释器、锁摘要和安装版本。执行 Ruff、format、mypy 和导入边界。可复建引用同锁的实际安装命令与状态。 |
| G6 兼容与安全 | B merge ref、C strict 与有限 records、独立 digest/projection | B fixture 与 Schema 字节，兼容性说明、摘要方向、版本、迁移、已批准形状和外部信任限制。 |
| G7 报告与停止 | C PLAN 最终命令、AGENTS §9/§13 | 保留冻结 SHA 和产品逐文件摘要、逐命令日志、JUnit、结论。服务、真实模型、GPU 或生产运行均标 RUNTIME_NOT_RUN。 |
| G8 Coverage/Identity | runtime/coverage、runtime/perception、validation/decision_coverage、N02/N03 | I-01/I-02/I-03。空 scope、全排除、父 U、真实 no_hits、grounding no_target/failed/partial、有效命中保留、同目标跨 Profile 的独立 Objects/Facts。 |
| G9 Rule/生命周期 | executions、decisions、transitions、decision_coverage、N06、policies，C execution 矩阵 | 查 guard/predicate 分离及实际 source/authority revision（I-01）；mandatory 全扫描、collect_all、终态无出边、未执行无假 Attempt。 |
| G10 R2 | B 枚举/策略/能力/Prompt/Facet/producer validators、C RuleType 和记录绑定 | 25 ArtifactKind、16 policies、forward authority、Question 一对一、Tool Step 一对多、跨 Attempt 拒绝、B wire 保留。C1-02 对应 I-01。 |
| G11 R2.1 | Applicability roots、invocations/selection/tool_executions 与 C invocation tests、B projection tests | F01–F05 正负例。核对已选审查凭据、shared/extra/fallback、unavailable actual version、grounding/step origin 和 F04 两阶段。 |
| G12 Applicability | runtime/applicability 六类记录、validation/applicability、Coverage/Decision 与 C applicability tests | 核对 H01–H07、A01–A13。I-01/I-02/I-03 涉及实际 N01/N02/N03/N06 记录、ancestor projection 与执行依赖的区分。 |

## 冻结前发现

- **I-01。有限视图与持久记录。** runtime/observations.py 的 ObservationBatch.to_authority_descriptor 将批次、选择、调用及各 Question Fact 投影到 B 类型。抽查时其余 B 有限描述符尚无完整的实际记录绑定入口。DecisionCoverageRecords 与 ApplicabilityScope/ApplicabilityRecordSet 直接接收 NodeRun、Attempt 和 Object 描述符。check_proof 校验 Known Fact、selector、revision、Attempt 与现有来源引用，但输入不含实际 ObservationBatch/ToolExecution，无法证明该来源存在且返回了此 Fact。按 C1-02 逐项核验 Object、NodeRun、Attempt、QuestionSelection、ObservationResult、ToolStepResult 和 ExecutionDefinitionPin 的来源、可信调用方边界和拒绝样本，同时保留 B wire。
- **I-02。Coverage 缺少实际 N02/N03 关联。** check_scope/check_dependency 验证 Profile/对象描述符、调用状态和上游 NodeRun 描述符。DecisionCoverageRecords 没有被选 N02 输出和 N03 GroundingRecord，Coverage 上的 HITS/NO_HITS/PARTIAL 与 hit 集合、N03 outcome 和 Objects 均无法从生成依据反查。DECISION_NOT_EVALUATED 检查 N06 Execution 和终态，未比对 blocker 的 Object。验收需要结果交换、对象污染、外 Object blocker 的拒绝样本。
- **I-03。N02 多调用不能贯穿 Coverage。** DiscoveryAttemptOutput.check_records 支持按 Profile 的 discovery_binding_ref 分组，完整发现检查使用成功 Profile 的合并范围。ExecutionCoverage.discovery_model_invocation_ref 只接一个调用，check_scope 要求其 profile_refs 恰等于整个 eligible 集合。增加两种绑定、多个调用、完整命中或 no_hits 的样本，以及遗漏调用和伪造附加调用的样本。

## 节点交接锚点

| 节点 | 静态锚点 | 冻结后检查 |
|---|---|---|
| N01 | nodes/context 的 check_snapshot 与 FailedContextBuild | N01 只产 Snapshot/验证报告，失败无 Snapshot/Gate，成功与实际 NodeRun、Attempt、Snapshot 绑定。 |
| N02 | nodes/discovery 的 check_gate、check_records 与 SkippedDiscoveryOutput，runtime/selection | eligible 为空、真实 no_hits、多绑定、不可用 Profile、越界字段、附加调用或预算，与实际 Coverage 同源。 |
| N03 | nodes/grounding 的 check_discovery/check_records | 继承选择，有多个独立 Object，拒绝重复 ID、错 Profile/实体；空/失败/部分与 Tool 依据进入 Coverage。 |
| N04 | nodes/observation 的 check_batch、runtime/observations | 一 Object 一次 joint 调用、Question 顺序、独立主 Fact、全包故障零 Facts、整批重试、禁止跨 Attempt 拼装。 |
| N05 | nodes/tool_execution 的 check_plan/check_observation_facts/check_steps | 固定 Profile Plan；N03 grounding 与 N05 Step 分开；验证已选 N04 Fact、实际 Tool 输出、Object/Attempt；拒绝候选影响或未声明映射。 |
| N06 | nodes/decision 的 check_records、validation/decision_coverage | 无 Object 无 Decision；候选遗漏不掩盖 mandatory；三层记录选定 revision、真实 NodeRun/Attempt、独立 guard 和 T/F/U、可保留部分结果。 |
