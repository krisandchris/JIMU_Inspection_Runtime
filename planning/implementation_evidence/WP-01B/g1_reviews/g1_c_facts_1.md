`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。只读检查完成，无文件修改、测试、安装或 Git mutation。

基线核对：`main@51a10416d882fe85e5a1cfc5440eac6498fc48f4`；只看到既有 `planning/{acceptance_evidence,active,transition_evidence}/` 未跟踪目录。实际读取最新授权、AGENTS、PLAN/FINDINGS/REVIEW、Domain §4.4/12/13/14、N04/N05/Attempt 章节、A foundation 源码及示例。

### 1. G1-D02：可以闭合，不再存在范围授权阻塞

最新授权明确允许 B 实施 RFC 8785 JCS、确定性 projection 和局部 digest；完整 Bundle root/signature 留 WP-02。建议冻结：

- `GlobalTypeLimits`：四个显式正整数，分别不超过 A 的 `4096/256/64/8`。
- `ValueSchemaGenerationSpec`：`projection_profile_version/generator_id/generator_version/schema_dialect/canonicalization_id/global_limits`，固定注册 profile、完整 dialect URI、`JCS`；不接收任意生成选项。
- `TypeDefinitionProjection`：复用 A 七分支类型代数；仅规范化 payload 约束，不复制另一套自由类型系统。
- `ValueSchemaGenerationInput`：仅 `projection + generation_spec`。
- 非持久 `GeneratedValueSchema`：`generation_input/canonical_input_bytes/input_sha256/canonical_schema_bytes/child_digest/child_ref`；bytes 是生成结果，不是 JSON Schema 的自由输入对象。
- `FactSchemaDefinition.payload_schema_ref` 精确指向该 `value_schema`，以四元组比较身份，dialect 单独检查。

确定性顺序：规范化输入 → JCS input hash → input-derived `$id` → payload schema JCS → child digest → parent 引用。B 不必实现 parent digest，绝不把 parent identity/metadata/digest 放入 child。

**数值资格必须分开：**

1. A `StrictInt` 接受任意 Python 整数，保持不变。
2. JCS 数字必须具有 binary64 可表达性及规范 ECMAScript 序列化；不能直接拿 Python `json.dumps`。
3. `±(2^53−1)` 是常见安全整数范围，不等于 binary64 全部可精确整数；例如 `2^53`、`2^60` 可精确表示。若 canonicalizer 仅支持安全整数，应在**projection 入口**显式拒绝并说明库支持域，不能宣称所有超范围数字均 RFC 不合法。
4. A 的整数 bounds 若采用转换，要求有限且 `int(float(n)) == n`，不满足拒绝；也要确保 canonicalizer 自身支持该精确整数。不可先转 float 再判定丢失。
5. Number 的已接受 finite Python float 保留其 binary64 语义；不用字符串十进制再猜原始精度。
6. `-0.0 → 0`、数值表示变化不改变 mathematical bound；A integer/number **严格表示差异仍由 Registry 验证**，不能由 payload JSON Schema 代替。

结构默认值建议冻结两个入口：原始输入先按显式 generation limits 补省略值，再 A 验证；已经构造的 A TypeDefinition 视为有效约束，超过更小 generation limit 则报错，不能截断。

JCS 要求 UTF-16 属性排序；Domain 的 Record/member 规范化要求 UTF-8 排序。这是两个不同步骤。测试必须包括非 BMP 排序、负零、指数格式、转义、孤立 surrogate、非有限数及独立 RFC Appendix B 向量。[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)

这些属于授权范围内技术裁决。

### 2. G1-D03：建议精确最小支持契约

所有持久根显式 `schema_version`，全部 strict/frozen/extra-forbid；所有数组有界、唯一。不要新增 RuntimeRecordKind。

| 类型 | 最小字段与约束 |
|---|---|
| `ObjectFactScope` | `scope=object, execution_id, profile_ref, object_ref`；实际 Object 必须归该 Execution/Profile |
| `ExecutionFactScope` | `scope=execution, execution_id`；禁止 object/profile 字段 |
| `ProducerIdentity` | Question：question ref；Tool：tool_plan ref、step_id、tool ref；System：注册 requirement；各带 output_name，冗余 id/version 必须一致 |
| `FactRecord` 五分支 | 公共：schema_version、fact_id、scope、fact_key、fact_schema_ref、catalogue_ref、declaration_id、producer identity、attempt_ref、source、evidence_refs、created_at、trace关联；known 必填 Value，其余分支完全不允许 value（包括 null）并必填 structured reason |
| `QuestionFactSource` | question_ref、observation_batch_ref、model_invocation_ref；N04 精确关联 |
| `ToolFactSource` | tool_execution_ref；仅 N05 tool_plan_step 能产 Tool Facts；实际 implementation/version 单独记录 |
| `SystemFactSource` | typed registered capability identity、system output result ref；不得伪造 ModelInvocation |
| `AttemptDescriptor` | attempt_ref、execution_id、node_run_ref、owner、attempt_number、AttemptStatus；owner 为 N04/N05/system 的封闭 union，禁止虚构 N07 |
| `ObservationResultDescriptor` | result_ref=`OBSERVATION_BATCH`、scope、attempt_ref、question_selection_ref、effective_question_refs、model_invocation_ref、envelope/association validity、完整 candidate_fact_refs、结构化 diagnostics |
| `ToolStepResultDescriptor` | result_ref=`TOOL_EXECUTION`、scope、attempt_ref、node=N05、origin=tool_plan_step、tool_plan_ref、step_id、tool_ref、完整 mapped outputs、每项 output state、candidate_fact_refs |
| `SystemOutputResultDescriptor` | 下述 system-output event 形状；允许有记录的 optional absence |
| `AuthorityValidationInput` | 所有相关不可变 descriptors、definitions/declarations、Facts、Object/NodeRun/Attempt 关系、已选 revision 历史；是有限纯校验输入，不能变成持久服务或任意 map |
| `AuthoritativeResultSelection` | 规范 §13.2 公共字段及三分支 group identity；actor 为已规定 service/human closed identity |

Fact reason 可用闭合 `reason_code + bounded message + related refs`；消息不得承载替代结构化字段。原规范未限定 code 全集，可由主控冻结技术错误码；不要新增“业务 unknown enum”。

**group identity 和结果引用：**

- N04：`(execution, profile, object)`，result kind **OBSERVATION_BATCH**。
- N05：`(execution, profile, object, exact tool_plan_ref, step_id)`，result kind **TOOL_EXECUTION**。
- system：`(execution, scope, object/profile if object-scoped, exact registered producer identity, output_name)`。

**System result kind 推荐：已有 `EXECUTION_EVENT`。** 在 B 只定义封闭的有限校验 descriptor：event ref、execution/scope、attempt ref、exact producer identity、output_name、`emitted(fact_ref)` 或 `optional_absence(reason)` union。这不是新增 kind、完整 event 生命周期或事件存储服务。

理由：现有 SystemProducer 允许 zero_or_one；单个 FACT 无法代表“已完成但明确无输出”。FACT 只适用于永远有且仅有一个 Fact 的选权结果，不能覆盖规范允许的 optional absence；更不能拿 FACT 冒充 N04 整批。使用已有 EXECUTION_EVENT 作为明确 system-output evidence 是可由主控裁决的表示技术，不改变输出基数。

### 3. 验证资格及完整性

- 每个 effective N04 Question 必须恰有一个 primary Fact；mandatory/optional Question 是**选择资格**，不能成为 batch 内省略已选 Question 的许可。
- `unknown/error/not_applicable/not_evaluated` 可是结构合法 Fact；完整性不等于所有 Fact 都 known。
- transport/untrusted envelope/ambiguous association：无可选整批，不能制造 per-Question Fact 修复。
- N05 检查的是该 **Step mapped outputs 全集**，不是 ToolDefinition 全部未使用 outputs。required 输出必须完整；optional 缺失必须有显式 absence reason，不能默认为 unknown/false。
- `selected_fact_refs` 必须与选定 result descriptor 的完整输出集合完全相等；每个 Fact 具有同一 selected Attempt、同一 group、合法 declaration/schema/key/source。
- 未输出的 optional output 以结果 descriptor 的 absence 状态证明；不能靠 selected_fact_refs 为空推断成功。
- `validated_success` 至少要求真实关联 Attempt `succeeded`、可信 envelope/association、完整输出及全部局部 validation 合法。合法 error Fact 不自动等于 schema 验证失败或 known false。
- 所有 reason 均不得救活 timed_out/cancelled/superseded/late Attempt，不改终态。
- `explicit_retry_resolution/manual_review` 仍必须满足结果关联和完整性；manual review 不是允许合并多 Attempt 的特权。若需允许失败/超时 Attempt 作为权威，应另有批准语义，当前保守拒绝。
- 首 revision=1 无 predecessor；后续严格同 group、revision+1、精确 predecessor。fork、缺前序、跨组 supersession 拒绝。consumer 固定 revision；新候选不会改 selection。
- 纯 Registry 返回 typed validation report/validated view，不自动建立 authority，不把“最新”作为 fallback。

**Identity 和冲突范围：**

- Object 按现有规范 Execution-local。
- Fact 规范说 fact_id 识别 immutable candidate，却未明确全局唯一。推荐 B 校验目录内将 `fact_id → immutable whole Fact` 强制单值，跨 Execution/Attempt/key 重绑同 ID 也拒绝；外部存储全局唯一性仍未验证。
- 同时检查规范 candidate composite key，不能只检查 fact_id。
- 原始重复交付由输入保留 `candidate_bytes` 并比较完整 bytes；不同 bytes 同 candidate key 拒绝。strict 模型相等/stable_json 相等不等于原始 bytes 相同。
- Registry 内已有模型对象可另提供“规范 typed content 等价”检查，但不得把它标为原始 transport dedup 的证明。
- 所有历史 candidates/Attempt/selection 均 append-only；B 测试以输入前后快照验证不会隐式覆盖，不宣称 DB CAS 已实施。

以上可作为 B-F03 技术冻结，系统 producer 的实际调度/权限/存储仍留后续。

### 4. G1-D01：条件 Producer 仍有真实分期矛盾

具体证据：

- Domain §13.1 `activation_condition = always or typed Tool condition`。
- Domain §13.3 定义 `tool_condition` 可读 Context + 该 step 前可用 validated Facts。
- Domain §14 ApplicabilityPolicy 必填 `condition_ast`；ToolSkipPolicy 允许 `tool_condition` 或显式 literal true。
- 最新授权禁止 Rule AST/TypeChecker/Evaluator，且仅允许“已批准的简单声明式策略”。

当前没有已批准的简单**条件表达式**语法、操作符或缺失值语义可直接复用。`always`/ToolSkip literal true 已明确获批，可实施；但把整个 Producer 缩成 always，或把 Applicability 改成 identity-only，并不能满足“完整 Policy + conditional Producer”的旧批准语义。

不建议用 `activation_condition=policy_ref` 来关闭：它只把未建模 AST 转移到被引用 Policy，既不能验证该 Policy，也不能恢复表达能力。

**最小用户决策应二选一：**

1. B 仅实现已批准 always/literal true 分支；明确将条件 Applicability/ToolSkip/Producer 分支及相应完整性验收留 C，调整 B 验收口径。
2. 明确批准把既有 AST 的**数据结构**提前 B，TypeChecker/Evaluator 仍 C；这与最新“不实现 Rule AST”禁令存在直接范围差异，不能由主控自己宣布解决。

若主控选择另建 bounded simple condition，必须先证明它是已批准语法的子集及其兼容/迁移规则；当前材料没有这项证据。该项属于真实业务/阶段语义决策，不能仅凭“实现细节”裁决后标整体 PASS。

最终建议：D02 可关闭，D03 可按上述严格技术附录关闭；D01 conditional 分支继续阻塞相关工作，不阻碍独立 FactSchema/JCS/静态 scope/identity 模块。
