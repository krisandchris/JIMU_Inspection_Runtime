# Sol Medium read-only review A

Source: native task `/root/wp01b_a_ontology`.
Captured by Primary from the actual first final response; no claim of code acceptance.

`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`

任务 A 已完成，只读；未修改文件、未安装依赖、未执行测试或 Git mutation、未再委派。此报告仅供主控编制唯一 PLAN，不批准契约或工作包。

**基线**

实际执行 `git status --short --branch`、`git rev-parse HEAD`、`git status --short --untracked-files=all`，均 exit 0：

- 分支 `main`，HEAD=`51a10416d882fe85e5a1cfc5440eac6498fc48f4`。
- tracked 工作树无修改；恰有已知 11 个 untracked acceptance/transition 文件，未触碰。
- domain 现有六文件：`__init__.py/base.py/enums.py/primitives.py/references.py/values.py`。
- 已阅读指定 AGENTS、README、四 specs、四 planning、WP-01_IMPLEMENTATION_PLAN 及四 examples。旧 pending 表述按主控提供的用户审批历史理解，没有据此重开 D01–D15。
- 当前可见上下文没有可安全独立核对实际 model/effort 的 `turn_context` 字段，因此实际配置标记 `UNVERIFIED`，不从代理名称推测。

**可直接落实到 PLAN 的模型和引用清单**

| 模型 | 必要字段与已有依据 |
|---|---|
| EntityDefinition | `schema_version/entity_id/version/name/entity_type/description/attribute_schema_refs/lifecycle`；DM:56–63、644–645。属性引用严格限 `attribute_schema`，不含 Object/BBox/Decision |
| RegionDefinition | `schema_version/region_id/version/name/region_type/scene_type/asset_refs/context_schema_ref`、可缺省 `geometry_ref`；DM:71–81。分别限 `static_asset/context_schema/geometry_resource`，asset 目标 DM:724–727 |
| ProfileDefinition | DM:87–98 全字段；正向持有 Entity/Facet/Question/ToolPlan/RuleSet refs、两 ModelBinding refs、grounding union、两 policy refs |
| FacetDefinition | `schema_version/facet_id/version/name/description/fact_schema_refs`；DM:109–122。Schema allowlist 非空；可选 metadata/tags 必须先冻结封闭形状 |
| QuestionDefinition | DM:128–139 全字段；`facet_refs` 非空；一个 primary key/schema；prompt、evidence、unknown、validation refs；无 Profile 反向 membership |
| ModelGroundingBinding / ToolGroundingBinding | `kind=model/tool` 与对应精确 ref；DM:744–750。功能/输出约束须通过注入定义验证，不能由 ref kind 单独推断 |
| ContextFieldSelector | `source/field_path/精确 context Schema ref`；DM:1136–1139。路径为有限已声明字段路径，不是自由 JSONPath |
| 有限定义校验入口 | 对注入的明确类型目录做引用身份、kind、Facet intersection、Profile→Question Facet/context 和 producer scope 检查；DM:928–936。不得变成 Bundle closure/compiler |

所有独立持久根要有显式 `schema_version`，而非仅继承一个默认版本（DM:644–645；PLAN:173）。定义内不存自身 digest 或 Bundle root digest（DM:625）。

**精确 Facet 交集算法**

1. 用 `(artifact_kind, artifact_id, version, digest)` 作为不可变引用身份；解析每个 Question 的所有 `facet_refs`。
2. 每个 Facet 的 allowlist 均须包含 Question 的同一个精确 `primary_fact_schema_ref`，即 `∀ facet: primary ∈ facet.allowlist`。
3. 每个 Profile 必须声明其 Question 的全部 Facets；精确解析同一 FactSchema 的语义、单位/坐标系、scope 和允许 producer kinds。
4. Profile 不得改变 Question 的 key/schema/meaning/unit/scope；需要变化则新建 Question/FactSchema identity 或 version。
5. shape equality、相同 ID/version 异 digest、只在 allowlist 并集中出现，都不能通过。

依据：DM:115–122、928–936；TEST_STRATEGY:48。共享精确 Schema 不代表新增 Fact、producer 或跨 Profile Fact 共享。

**WP-01A 基础类型复用边界**

- `ContractModel` 已有 strict/frozen/extra forbid/revalidate instances/validate default/nonfinite 禁止（`base.py:15–22`）；继续复用，嵌套集合用 tuple/不可变映射。`stable_json()` 明说不是 JCS/制品摘要实现（`:24–31`）。
- 继续使用 `ArtifactId/RecordId/Version/Digest`（`primitives.py:7–41`），不要再定义一套宽松 ID/SemVer。
- `ContentRef` 已冻结 wire 字段为 `artifact_kind/artifact_id/version/digest`（`references.py:21–24`），文档里的 `kind/id` 简写不得改变其 wire shape。
- `SchemaRef` 是 ContentRef 子类，但额外**必填** `schema_dialect`，并允许九种 schema kind（`references.py:47–61`）。字段仍需进一步限具体 kind。
- DM:136 明确 Question primary schema 是 ContentRef；DM:116 的 Facet allowlist 是 exact refs。不要未经说明把这两个字段改成含额外 dialect 字段的 SchemaRef。建议保留批准 ContentRef wire shape，另用具体 kind 约束；若要统一使用 SchemaRef，先列兼容差异请主控定案。
- 精确集合比较建议先归一到上述四元身份，避免 ContentRef 与 SchemaRef 的 Python 模型类/额外 dialect 字段干扰引用等价判断；仍须验证所有 dialect 约束，不能忽略不兼容字段。
- `IdentitySelector` 仅 profile/region、使用 `version_selector`，没有 digest（`references.py:27–32`），仅用于 ApplicabilityPolicy（DM:698–701），不能用于普通 membership 或执行内容解析。
- `RuntimeRecordRef` 仅 kind/id，没有天然 Execution 校验；跨记录一致性由后续所属根负责（`references.py:35–44`）。
- Fact payload 必须调用已有 `validate_typed_value()` 或 ValueContract；裸 EnumValue/RecordValue 仅证明形状（`values.py:294–320`）。其 type tree/闭合 Record/严格数值可复用，不能重新发明 Fact payload 代数。

**Blocking Findings，须在 PLAN 冻结，不能实现时自行猜测**

1. **B 禁 AST 与必填 Policy AST 冲突。** ApplicabilityPolicy 必有 `condition_ast`（DM:1167），ToolSkipPolicy 同样需要 AST 或 explicit literal true（:1174）；而 B 禁 Rule AST。若 B 要交付完整 16 分支，就不能避开此契约依赖。应明确哪些 Policy 分支在 B 可完整实现、哪些仅保留精确 ref 并后续解析，或提请用户批准调整边界。不得用 `Any`、自由字典、字符串表达式填空，不得悄悄加 AST。literal-true 单一分支也不能冒充完整 ToolSkipPolicy。
2. **业务词表和结构字段未冻结。** `entity_type/lifecycle/region_type/scene_type` 无封闭值域，Facet 的 metadata/tags 无封闭形状（DM:60、63、75–76、113）。PLAN:236 已要求有限初始值或受约束标识+版本化词表治理，但未给最终选择。需逐字段列 wire type、空值规则、长度/数量、允许 enum/词表值；不从 illustrative 的 `physical_object` 推定正式 enum。不建议顺便新增字段来绑词表，除非列为契约变化。
3. **ContextFieldSelector 的 source/path 契约不完整。** DM:1136 给组成但没有 source 的明确值域、字段路径空路径/长度限制、optional-context 兼容规则。不能把 ContextProvenancePolicy 的可信来源枚举（DM:1181）直接当成 selector source：两者一个是来源证明，一个可能是引用域。Profile 自身也没有 context_schema_ref；检查应通过显式注入的 Region/Context 类型环境，不应给 Profile 私加字段。
4. **schema root 初始版本和 ref 形状应书面固定。** PLAN:693–698、236 要求首次冻结，WP-01A 的 `Version` 只约束格式，没有规定 B 新根接受哪个 `schema_version`、有无默认。应显式批准值/字面量及 schema root 名称，不能从 artifact version 推导。
5. **Profile→RuleSet 的跨阶段检查不得虚报完成。** Profile 必有 `rule_set_refs`（DM:96），但 B 不实现 RuleSet/Rule AST。可验证 ref shape、kind、duplicates，并通过明确有限目录确认 identity；不能声称已验证 Rule mandatory producer completeness、applicability 输入域或 RuleSet semantic closure。DM:849–855、Bundle:160–169 需要真实 Rule/producer graph 才能验证。PLAN 应逐项标 `DEFERRED_TO_WP01C/WP02`；不要构造带 Any 的伪 RuleSet 或默认空 RuleSet 来绕开要求。

**建议有限纯接口**

- `reference_identity(ref) -> immutable four-tuple`。
- `validate_question_facets(question, facets, fact_schemas) -> ValidationIssues`。
- `validate_profile_questions(profile, questions, facets, context_types, fact_schemas) -> ValidationIssues`。
- 明确注入映射或只读 Protocol，不加载磁盘、网络、模型，不导入 Bundle/Workflow。
- 若需错误模型，冻结 `code/path/related_refs/message` 等有限字段；区分 `missing_reference`、`wrong_kind`、`digest_mismatch`、`schema_not_in_facet_intersection`、`profile_missing_question_facet`、`undeclared_context_path`、`producer_scope_mismatch`，这些仅是建议错误码，尚未批准。

**正负测试建议（全部未运行）**

正例：五定义最小完整 round-trip；同 exact Schema 被多 Facet/Question 重用；新 Profile version 重用未变 Question；两个 Profile 的上下文环境都满足同 Question；model/tool 两 grounding 分支；全部根版本显式；tuple 与嵌套映射不可变。

负例：

- 缺根 schema_version、未知字段、定义夹带 Object/BBox/Decision。
- Attribute/context/primary/payload schema wrong kind；缺 digest/range/latest；SchemaRef 缺 dialect；互换 ContentRef/IdentitySelector。
- Facet 空 allowlist、Question 无 Facet、union-only membership、empty intersection、same shape/different digest。
- Profile 缺少 Question Facet、未声明 context path、单位/semantic_key/scope/producer kind 不兼容。
- Question 反向 Profile 字段、Facet Question backrefs、Profile 自 digest/Bundle digest。
- 重复 refs 或同 kind/ID/version 不同 digest 的有限目录冲突。
- 文档旧 examples 缺字段/占位摘要应拒绝；不能把 examples 修成生产正例。
- Pydantic 与 generated JSON Schema 双验证；跨引用交集/context 语义另由第二阶段 validator 验证，不声称 JSON Schema 单独足够。

主要风险是把“字段严格”误报为“全语义闭包已验证”，或为绕过 B 的禁 AST 范围偷渡占位契约。建议 PLAN 对每项检查列明 `IMPLEMENT_IN_B / SHAPE_ONLY / DEFERRED`，阻塞的字段/枚举/Policy 边界由主控提交用户裁定。
