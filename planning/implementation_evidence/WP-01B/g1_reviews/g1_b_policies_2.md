`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`

PLAN-2-G1 改进有效：D01 如实 OPEN；metadata 不再无 Schema；raw/typed 表示层、optional→required 必须 block、ModelBinding 部署约束单一权威均已明确。没有发现这几项本身改变已批准业务语义。但 D04 还不能称为“字段冻结完成”，存在以下具体缺口。

1. **C2：共享类型位于 validation 层，与既定依赖方向冲突。**  
   PLAN `:207` 把 `FieldSelector/ResolvedFieldType` 放进 `validation/schema_descriptors.py`，`:174–177` 的 InputBinding 需要引用这些类型；而 `:50` 禁止底层模型反向导入 validation。建议把持久契约需要的 FieldSelector/表示层枚举/纯类型描述放进低层 `schema_types.py` 或 shared support；`validation/schema_descriptors.py` 仅保留有限目录校验。纯类型层只能依赖 A/support，不得依赖 Tool/Profile/Registry。这样可避免 Tool→validation→Tool 的潜在循环。当前没有代码，属于明确设计风险，未证明已有循环。

2. **C2：SchemaDescriptor 仍是要求描述，尚非闭合字段契约。**  
   PLAN `:169–173` 只写 raw DTO/typed Value、ToolIO envelope，并未给 discriminator、分支字段、schema kind 对应关系、payload/evidence/output_states 类型。实施者仍会自行发明。最小补充：
   - `representation=raw_dto|typed_value` 的准确字段和各分支；
   - exact SchemaRef、受限 TypeDefinition、是否允许空 whole-value selector；
   - ToolIO 的 payload Schema、evidence section、状态分支与 output_states 声明；
   - 不允许 descriptor 的结构与其表示层相矛盾。
   
   **特别注意：** A TypeDefinition 描述 payload 语义，不足以独自证明一个 raw ToolResult envelope 的完整 shape。不得让 `representation=raw_dto` 标签代替实际 envelope 描述。也不应要求 Fact 的 typed Value 直接映射到普通 raw Tool input；若无明确已批准的解包规则，应拒绝表示层不兼容，不能静默剥离 kind/value。

3. **C2：InputBinding 来源仍欠具体 wire，且未明确 required 的单一含义。**  
   PLAN `:174–180` 列出了来源，但没给闭合 source union 字段；“Object/image 引用”的 Schema/owner、“祖先 step 输出”的 value 基准尚不明确。建议冻结 context/object/image/fact/upstream_step 五分支的字段，source 与 target 都含准确 SchemaRef/路径，祖先 step 包含 step_id 并明确从 normalized payload 读取。`required` 应明确是 target obligation，并与目标 Schema 对照，避免同时维护矛盾可写标志。
   
   `required target + optional source + on_missing=block` **是可接受的技术裁决**：静态映射可以存在；运行时源缺失必须阻塞，不能当作完整已可执行。应加说明：这不证明 mandatory producer 可用性或全 Bundle conditional completeness，后两者仍 deferred。无 authority/unknown/error 不得落入普通 optional omit 路径。

4. **C2：ReviewMetadata/TransitionAssessment 的 scope/verdict 尚无类型。**  
   PLAN `:193–195` 只列语义标签，没有 scope、verdict 枚举或 from/to deployment identity 的封闭字段。需给准确模型，尤其：
   - source/target 的 provider_id、deployment_id、logical_model_id、deployment/model version；
   - capability、trigger、ReviewReportRef、UTC 窗；
   - approved scope 使用何种既有身份集合以及如何判定覆盖；
   - verdict 的有限值；
   - LocalPrimaryException 审查与 fallback transition 审查采用不同适用分支，不能硬塞不存在的 from/to。
   
   可以由 Astra 技术冻结辅助输入，但不能自造审批主体、可信根或质量阈值。缺可信输入返回 incomplete/deferred，现 PLAN 的这项边界正确。

5. **C3：metadata/Prompt 还需几条可实施细节。**  
   PLAN `:160–163` 的 metadata 应明确是一个/多个 namespace entry、namespace 唯一性、根必须 Record、attribute_schema_ref 必须 ATTRIBUTE_SCHEMA、RecordValue 按该 descriptor 双阶段校验。tags 的词表 semantic_domain 也需写明校验输入，不能把任意词表成员当合法 tag。  
   PLAN `:196–197` 已冻结占位符语法，但要明确未闭合/裸 `{}` 拒绝、注册未使用变量是否允许、重复占位符是否允许、partial 变量作用域。保留 Domain `:759–766` 已定义的 source 枚举及 required/on_missing 对应关系，不能误用 InputBinding 的更宽 source enum。

**已确认可保留的决定：**

- `:190–192`：ModelBinding `logical_model_id` 字段名与 ProviderPolicy 作为部署约束唯一权威合理；应补跨模型检查 DeploymentRequirement.logical_model_id 全部等于绑定值。不要在 ModelBinding 再增加第二份 deployment constraints。
- `:178–183`：optional 祖先传播、缺失必需输入 block、无数值 coercion 和可证明包含原则正确。
- `:184–189`：ToolFactOutputMapping 无 alias、本地 DAG 检查、非祖先输入拒绝正确。
- RemotePrimary 保持显式配置，没有新增默认策略；Tool worker placement 与 Model Gateway routing 分离正确。
- D01 继续 OPEN 正确，没有采用假占位或 14 分支冒充完整 Policy。

建议结论：**PLAN_STATIC_CONDITIONAL**。以上属于可由 Astra 在现授权内补充的 D04 技术细节，无需新增业务审批；D01 仍需用户范围裁定。补充完成后定向复核这些字段与依赖位置，再交实施者。此结论不构成代码验收或工作包批准。
