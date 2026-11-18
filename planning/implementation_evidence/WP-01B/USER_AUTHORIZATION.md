## 一、最终目标

在 JIMU Inspection Runtime 项目中，完成 WP-01B：Definition & Fact Registry 的完整工程实现、自动化测试、独立代码审查、问题整改和最终验收。

你必须持续推进这个目标，直到：

1. WP-01B 所有批准模块完成。
2. 所有必需 CPU-only 测试通过。
3. 静态检查和 Schema 验证通过。
4. GPT-6 Astra 完成独立代码审查。
5. Sol 子智能体完成独立 Challenge。
6. 所有阻断性 Finding 得到修复和复核。
7. 生成完整、可追溯的验收证据。
8. 输出最终 PASS / CONDITIONAL / FAIL 报告。

不要在完成一个子阶段后仅汇报进度并等待用户重复要求继续。

在没有用户输入、没有真实阻塞且 Goal 仍处于活动状态的情况下，主动选择下一个依赖已满足的任务继续执行。

## 二、项目基线

Repository:
https://github.com/krisandchris/JIMU_Inspection_Runtime

Main Branch:
main

当前 Main HEAD：
51a10416d882fe85e5a1cfc5440eac6498fc48f4

WP-01A：
已通过验收并合并到 main。

WP-01B Planning：
已完成，但原始结论为 CONDITIONAL / BLOCKED。

规划材料：
- planning/active/WP-01B/PLAN.md
- planning/active/WP-01B/FINDINGS.md
- planning/active/WP-01B/REVIEW.md
- planning/active/WP-01B/SHA256SUMS

必须先检查这些文件的真实内容，不能根据摘要替代实际审查。

## 三、模型和 Agent 配置

### Primary Orchestrator

Model: gpt-6-astra
Reasoning: xhigh

负责：
- 唯一 Goal Owner。
- 架构裁决。
- 计划维护。
- 任务分配。
- 子智能体协调。
- 代码独立审查。
- 测试证据审查。
- Finding 管理。
- 最终验收。

### Development Subagents

Model: gpt-6.1-sol
Reasoning: medium

必须使用已经验证的原生 Codex Subagent 模型覆盖能力。

禁止静默将开发任务交给 Astra 自己执行，或在模型覆盖失效时假装使用了 Sol。

如果无法创建指定模型的子智能体，报告阻塞并等待处理。

### Subagent 分工

第一轮只读 Challenge：

- Sol-A：Ontology / Capability Contracts。
- Sol-B：ModelBinding / Tool / Policy Contracts。
- Sol-C：Fact Schema / Producer / Authority Contracts。
- Sol-D：WP-01B Test Strategy / Negative Cases。

第一轮完成后，由 Astra 统一裁决和冻结契约。

代码实施阶段：

- Sol-A：Entity / Region / Profile / Facet / Question。
- Sol-B：ModelBinding / Tool / ToolPlan / Policy Catalog。
- Sol-C：FactSchemaDefinition / FactProducerDeclaration / Authority。
- Sol-D：Fact Schema Registry / Contract Integration Tests。

任务必须按照依赖 DAG 调度。

允许独立模块并行，不允许多个子智能体同时写入同一文件。

Astra 负责公共接口、文件所有权、跨模块契约和最终集成。

## 四、执行前检查

首先：

1. 阅读 AGENTS.md。
2. 阅读全部四份架构规格。
3. 阅读 WP-01B PLAN / FINDINGS / REVIEW。
4. 检查 WP-01A 已合并代码。
5. 检查 main HEAD。
6. 检查本地工作区状态。
7. 检查已有验收和历史审计证据。

如果基线不是指定 Main HEAD，或者发现非预期的用户修改，停止并报告。

使用独立开发分支：

dev/wp-01b-definition-fact-registry

优先使用独立 Git Worktree。

不要覆盖现有 WP-01A 代码及历史证据。

## 五、阶段 G1：解决 WP-01B 四项契约阻塞

先让四个 Sol 子智能体分别只读审查：

### D01：Policy / Producer 与 Rule AST 分期

必须解决：

- WP-01B 实现完整的 Policy 数据契约。
- WP-01B 实现 FactProducerDeclaration。
- 不实现 Rule AST。
- 不实现 Rule TypeChecker。
- 不实现 Rule Evaluator。

Policy 必须使用封闭、强类型的 Pydantic 模型。

需要复杂表达式的能力不得使用 Any 或任意 dict 规避类型系统。

只有已批准的简单声明式策略可以在 WP-01B 实现。

Rule AST 及其求值语义保留给 WP-01C。

### D02：JCS 与 value_schema Digest

必须明确：

- RFC 8785 JCS 规范化。
- Value Schema 确定性投影。
- Child Schema Digest。
- FactSchemaDefinition 对 Child Schema 的精确引用。
- 不产生父子摘要循环。

WP-01B 可以实现 Value Schema 的规范化和局部 Digest。

WP-02 才实现完整 Bundle Root Digest 和签名。

使用独立测试向量验证，不得以普通 JSON key sorting 冒充完整 JCS。

### D03：Authority / Fact / Attempt

必须明确：

- N04 ObservationBatch 的权威结果选择粒度。
- N05 ToolPlan Step 的权威结果选择粒度。
- Fact 与 Attempt 的引用关系。
- Fact Scope。
- Producer Identity。
- Required / Optional Fact。
- Attempt 历史保留。
- Authoritative Result Selection。
- 缺失 Fact 和失败 Fact 的语义。

禁止 last-write-wins。

历史候选 Fact 不得被覆盖或删除。

必须显式指定权威 Attempt 和其完整输出集合。

### D04：分类字段及 Mapping

对以下内容建立严格字段契约：

- Classification Fields。
- Context Mapping。
- Tool Input Mapping。
- Tool Output Mapping。
- Field Selector。
- Schema Compatibility。
- Required / Optional Mapping。

不得使用无约束字符串和 dict 代替类型定义。

Mapping 必须能够验证来源 Schema 和目标 Schema 的兼容性。

### G1 完成条件

四项问题都有：
- 明确裁决。
- 关联 Pydantic Schema。
- 输入输出约束。
- 测试要求。
- WP-01B / WP-01C / WP-02 的职责边界。

更新：
- planning/active/WP-01B/PLAN.md
- planning/active/WP-01B/FINDINGS.md
- planning/active/WP-01B/REVIEW.md
- planning/ARCHITECTURE_DECISIONS.md（仅记录经批准决策）

Astra 必须完成契约 Challenge。

如果需要改变用户已批准的业务语义，暂停 Goal 并请求用户决策。

如果只是已批准语义范围内的实现细节，Astra 可以做有证据的技术裁决并继续。

## 六、阶段 G2：Ontology & Capability

Sol-A 实施：

- Entity
- Region
- Profile
- Facet
- Question

要求：

- 严格 Pydantic v2。
- ContentRef / IdentitySelector 使用 WP-01A 基础类型。
- 引用关系可验证。
- 版本和 Digest 语义一致。
- Facet / Question Schema 关系符合批准的精确交集约束。
- Question 一对一 Primary Fact Schema。
- Mandatory / Optional Question 语义明确。
- 不产生循环 Digest 依赖。

完成后执行模块级测试。

## 七、阶段 G3：Model / Tool / Policy

Sol-B 实施：

- ModelBinding
- Tool
- ToolPlan
- Policy Catalog
- Context Mapping
- Tool Input / Output Mapping

要求：

- Remote Primary 默认策略。
- Local Primary 必须有显式例外。
- Fallback 必须有明确策略。
- Tool 与 Model Provider 分离。
- ToolPlan 是确定性 DAG。
- ToolPlan 不依赖 N06 Decision。
- Tool Mapping 必须进行类型和 Schema 校验。
- 所有 Policy 使用封闭类型。
- 不实现模型调用和工具执行器。

完成后执行模块级测试。

## 八、阶段 G4：Fact Contracts

Sol-C 实施：

- FactSchemaDefinition
- FactProducerDeclaration
- AuthoritativeResultSelection
- Producer / Schema / Scope Validation

要求：

### Question Producer

一个 Question 产生一个 Primary Fact。

### Tool Producer

一个 ToolPlan Step 可以产生多个已声明 Tool Facts。

### Fact Scope

至少支持：
- object
- execution

### Authority

N04：
- ObservationBatch 作为权威选择的原子单位。
- 不允许从不同 Attempt 静默拼接权威 Fact。

N05：
- 每个 ToolPlan Step 独立选择权威 Attempt。
- 该 Attempt 声明的 Fact 输出必须完整验证。

### 历史记录

所有 Attempt 和候选结果均保留。

禁止隐式覆盖。

缺失必需 Fact 不得转换为正常结果。

完成后执行模块级测试。

## 九、阶段 G5：Fact Schema Registry

Sol-D 实施：

- Fact Schema 注册。
- Schema 身份与版本管理。
- Schema Digest 校验。
- Schema 查询。
- Producer / Schema 一致性检查。
- Fact Value 校验。
- Scope 校验。
- Fact Key 冲突检查。
- Authority Result 校验。

V1.0 Registry 为纯 Python、CPU-only 契约组件。

不实现 PostgreSQL、ORM、HTTP API 或持久化服务。

### Fact Registry 核心验收

必须能够验证：

1. Fact 是否引用已注册 Schema。
2. Producer 是否有权生产该 Fact。
3. Producer 输出是否符合 Schema。
4. Question 是否只产生一个 Primary Fact。
5. Tool Step 是否只产生已声明的 Facts。
6. 同一 Scope 下是否存在 Fact Key 冲突。
7. Fact Scope 是否正确。
8. Attempt 是否有效。
9. Authority 是否明确。
10. 非权威 Attempt 是否不会覆盖权威结果。

## 十、测试和质量验证

所有模块完成后执行完整 WP-01B 验收。

必须覆盖：

### Domain
- Entity / Region 关系。
- Profile / Facet / Question 关系。
- ContentRef / IdentitySelector。
- 版本和引用完整性。

### Policy
- 未注册 Policy。
- 非法字段。
- 不支持的枚举。
- 错误 Context Mapping。
- 错误 Tool Mapping。
- Schema 类型不匹配。

### Fact Schema
- Type Definition。
- Record / Enum / List。
- Schema Projection。
- JCS 确定性。
- Child Digest。
- 非法 Schema。

### Producer
- Question 一对一。
- Tool Step 一对多。
- Producer Scope。
- Fact Key 唯一性。
- Producer Version。

### Authority
- 多 Attempt。
- 失败 Attempt。
- 不完整输出。
- 不存在的 Attempt。
- 错误 Scope。
- 错误 Producer。
- 权威结果替换。
- 历史结果保留。
- 禁止 last-write-wins。

### Registry
- 注册。
- 查询。
- 冲突。
- 未知引用。
- 类型校验。
- Schema Digest。
- Producer 兼容性。

### 工程质量
- pytest。
- Ruff。
- Ruff format。
- mypy。
- JSON Schema Draft 2020-12。
- CPU-only Dependency Lock。
- Domain Import Boundary。

所有测试必须实际运行。

禁止通过跳过测试或放宽类型约束获得通过。

## 十一、Astra 独立 Review

Sol 完成开发后，Astra 必须独立读取：

- 实际 Git Diff。
- 全部核心实现。
- Pydantic Schema。
- 测试代码。
- 测试执行日志。
- 依赖锁文件。
- 证据清单。

不得只依赖 Sol 的总结。

Astra 应检查：
- 领域契约一致性。
- Schema 类型正确性。
- Fact Producer 关系。
- Authority 语义。
- Scope 和 Attempt。
- Mapping。
- 依赖边界。
- 测试质量。
- 是否越界实施 WP-01C。

同时委派 Sol 进行一次独立 Challenge，但最终 Review 结论由 Astra 负责。

### Finding 整改循环

如果发现问题：

1. Astra 生成带文件路径、具体问题、影响和验收条件的 Finding。
2. 分配给对应 Sol 子智能体。
3. Sol 修改指定范围。
4. Sol 运行对应测试。
5. Astra 重新读取真实 Diff。
6. Astra 验证 Finding 是否真正解决。

持续执行，直到没有阻断性 Finding。

不得仅凭开发子智能体声称修复完成就关闭 Finding。

## 十二、Git 和权限边界

允许：
- 本地 Worktree。
- 本地开发分支。
- 授权范围内文件修改。
- CPU-only 依赖安装。
- CPU-only 测试。
- 静态检查。
- 生成审计证据。

禁止：
- Commit。
- Push。
- PR 创建。
- PR Merge。
- 修改 main。
- GPU / CUDA / vLLM。
- 真实模型 API。
- WP-01C。
- WP-02。
- 需求平台代码修改。

如果需要修改已批准的架构语义，暂停 Goal 并请求用户确认。

## 十三、最终交付

生成：

planning/active/WP-01B/FINAL_ACCEPTANCE_REPORT.md

planning/implementation_evidence/WP-01B/

至少包含：

- README.md
- baseline.json
- environment.json
- changed_files.json
- dependency_versions.json
- test_commands.txt
- test_results.txt
- static_checks.txt
- schema_validation.json
- findings.json
- after_sha256.json

最终报告必须包括：

1. 四项初始契约问题的裁决。
2. 八个模块的实施结果。
3. 模块间依赖和接口。
4. Fact Schema Registry 完整性。
5. Producer / Attempt / Authority 验证。
6. 正向、负向、边界测试结果。
7. Ruff / Mypy 结果。
8. Schema 验证结果。
9. Finding 清单及整改状态。
10. 代码变更范围。
11. SHA-256 证据。
12. 已知限制。
13. WP-01B 验收结论。
14. WP-01C 推荐启动计划。

验收状态分别报告：

- Code Acceptance
- Test Acceptance
- Runtime Acceptance
- Independent Review
- Overall Acceptance

## 十四、Goal 完成条件

只有满足以下所有条件，才能将 GOAL-002 标记为完成：

- 四项契约问题完成收口。
- 八个模块全部实现。
- 所有必需测试通过。
- 静态检查通过。
- Schema 验证通过。
- 没有未解决 C0/C1/C2 Finding。
- 没有未授权的架构变化。
- 没有超范围代码。
- Astra 独立 Review 通过。
- Sol Challenge 完成。
- 验收证据完整。
- 最终报告明确记录所有限制。

Runtime Acceptance 在本 WP 中为 N/A，不得伪称运行时通过。

## 十五、阻塞处理

如果遇到以下情况，必须停止相关工作并明确报告：

- 需要用户决定新的业务语义。
- 需要扩大授权范围。
- 基线或工作区异常。
- 无法获得指定 GPT-6.1 Sol 子智能体。
- 无法满足 CPU-only 依赖约束。
- 核心契约无法在已批准语义下实现。
- 关键测试无法验证且没有可靠替代方案。

允许继续执行与阻塞项无依赖的其他工作，但不能绕过阻塞条件完成最终验收。

如果连续尝试没有带来新的证据或进展，停止无效循环并给出最小解锁条件。

## 十六、Goal 最终停止条件

WP-01B PASS 后：

1. 保存完整验收证据。
2. 输出最终报告。
3. 更新 WORK_PACKAGE_STATUS。
4. 记录 WP-01C 的依赖与实施建议。
5. 停止代码开发。
6. 等待用户验收和后续 Git 操作授权。

不得自动进入 WP-01C。

不得 Commit / Push / Merge。

**本 Goal 唯一最终目标：让 WP-01B 成为一个经过实际代码、自动化测试和 Astra 独立审查验证的 Definition & Fact Registry 工程交付物。**