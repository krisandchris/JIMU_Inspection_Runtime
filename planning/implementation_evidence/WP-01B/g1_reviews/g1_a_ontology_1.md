`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`

只读核对基线为 `main@51a10416d882fe85e5a1cfc5440eac6498fc48f4`；没有写文件、运行测试或 Git mutation。

D04 可以主要通过技术裁决收口，不需要发明业务分类全集。D01 则仍存在“完整历史条件能力”与“禁止 AST”的实质边界，需要主控明确采用最新授权的能力子集解释，或取得一个最小用户裁决。

### D04 建议冻结

| 范围 | 可实行的严格契约与校验 | 依据与边界 |
|---|---|---|
| 分类字段 | `entity_type/region_type/scene_type` 使用 `VocabularyMemberRef(vocabulary_ref: ContentRef[vocabulary], member: bounded string)`；校验精确词表存在、member 存在、对应 semantic domain 正确。建议 member 1..128 字符，不 trim/coerce | Domain:60、75–76、714–723。选择版本化词表是字段实现裁决；不得从示例 `physical_object` 构造封闭枚举 |
| `lifecycle` | 同样用版本化词表成员引用；作为定义元数据，不引入状态迁移函数、激活权限或 Release lifecycle 枚举 | Domain:63；Domain:630–632 只提供不可变/弃用原则，没有 Entity 状态机。若要赋予生命周期状态实际行为，则属于新增业务语义 |
| 根模型 | `schema_version: Literal["1.0.0"]` 必填，无默认；既有 ID/Version/ContentRef 复用 A。文本与集合边界在附录集中冻结 | Domain:644–645；A `primitives.py:7–41`；`base.py:15–22` |
| Facet 可选字段 | `tags` 可使用有界、唯一的词表成员 tuple。首版不接受未定义的 `metadata`；明确记为“不支持该可选扩展”，不能静默丢弃输入 | Domain:113。若要求支持 metadata，必须先冻结命名空间、字段类型及校验目录，不能接受任意 JSON |
| Context selector | `source` 封闭为 `context_snapshot / region_metadata / profile_metadata`；保留 `schema_ref`、`field_path`。每个 selector 绑定明确的 source Schema 与有限类型环境，不在 Profile 偷增 `context_schema_ref` | Domain:210–212、928–936、1136；`PLAN.md:188–205` |
| provenance | `ContextProvenancePolicy.allowed_sources` 保留已有五种可信来源，不与 selector.source 共用枚举 | Domain:1181。一个是值的来源证明，一个是可读取的数据域 |
| FieldSelector | 非持久支持类型包含 `schema_ref + field_path`；path 为最多 8 段的精确字段名 tuple。沿用 A FieldName 的非空/128 上界，禁止 JSONPath、动态索引和跨 Object join | Domain:1040、1096–1106；`PLAN.md:219`。字段名 `"a.b"` 可以作为一个精确字段名，不应被拆分 |
| 空路径 | FactSelector/通用 FieldSelector 空路径表示整个值；Context allowlist 建议要求非空字段路径，避免一次授权整个 Snapshot | Domain:1096–1097 明确 FactSelector 空路径语义；Context 的非空限制应作为技术支持边界记录 |
| 路径结果 | `ResolvedFieldType(type_definition, field_path, optional_ancestor_paths)`；optional 祖先包含可选叶。缺路径返回 typed error，不能伪造 missing 值 | `PLAN.md:215–219`；`REVIEW.md:43` |
| Schema descriptor | 只接受显式注入、精确 SchemaRef 绑定的有限、封闭类型树；区分原始 DTO 结构与 A typed-Value 包装结构；不能通过裸 path→type 列表声称证明 Schema 真实结构 | `FINDINGS.md:81–86`；Domain:1045–1055；`PLAN.md:188–201` |
| descriptor 信任边界 | 目录仅证明调用者提供的类型描述与 selector/mapping 一致。若未核对生成 Schema 字节及 digest，不宣称 descriptor 已证明引用内容真实性 | `PLAN.md:188`、218。B 有限目录验证不能冒充 WP-02 admission |
| Mapping 类型兼容 | V1 保守接受相同规范化 TypeDefinition 与相同表示编码；不做 integer→number coercion、单位转换或 enum 字符串转换。精确 FactSchema 身份要求仍独立保留 | Domain:928–935；A `values.py:43–46`、typed-value validation。可进一步支持有证明的约束收窄，但不必为本 WP 引入广义兼容算法 |
| Required mapping | required target 必须有映射；若 source 经过 optional 祖先，必须显式 `on_missing=block`。optional target 可显式 `on_missing=omit`；禁止 null/false 合成 | Domain:769–773 的已批准 missing 原则；Tool policy Domain:1174；保持 missing 与负证据不同 |

Schema descriptor 尤其不能把 A `TypeDefinition` 原样套给所有原始 DTO 后直接宣称路径匹配：A 的生成格式包含 `{"kind":...,"value":...}`；普通 Region/Profile DTO 的字段不是该格式。必须冻结表示层与显式解包基准，Tool value_path 则明确从 `ToolResult.payload` 开始。

上述 source 枚举、尺寸边界、descriptor 形状、路径及缺失策略是当前技术冻结建议，不是已经存在于规格中的逐字字段。主控可依据最新授权记录技术裁决，但应在字段附录及兼容说明中明确新增 wire。

### Ontology / Capability 必须保持的既有语义

- Profile 仅正向持有 Question/Facet/ToolPlan/RuleSet；Question、Facet、ToolPlan 不增加反向 membership。依据 Domain:622–627。
- Question 至少一个 Facet；primary Schema 必须属于**每个** Facet 的精确 allowlist。不能用并集、shape 相等或同 ID/version 异 digest 替代。依据 Domain:115–122、928–935。
- mandatory/optional 是 Profile 的 QuestionSelectionPolicy 角色，不是 Question 本体字段。依据 Domain:812–815；`PLAN.md:206`。
- Question/Facet 原字段使用 expected-kind ContentRef；不能无说明升级成增加必填 dialect 的 SchemaRef。依据 Domain:116、136；A `references.py:47–61`。
- `rule_set_refs` 在 B 中仍保留；Rule 内容未实现时，只能验证 kind/identity/已注入目录，Rule completeness 应明确 deferred。依据 Domain:96；Bundle:157–163。

### D01：完整 16 Policy 与禁止 AST

直接证据：

- ApplicabilityPolicy 必填 `condition_ast`：Domain:1167。
- ToolSkipPolicy 明确支持 `condition_ast` **或 literal true**：Domain:1174。
- Producer activation 支持 `always` 或 typed Tool condition：Domain:1021–1022。
- 旧计划已明确“true/always-only 不能冒充完整条件分支”：`FINDINGS.md:19`。
- 最新用户要求完整 Policy 数据契约，但仅允许“已批准的简单声明式策略”，Rule AST/TypeChecker/Evaluator 留 C。

可以安全实现且无需新增业务语义的部分：

1. 完整 16 个 policy_kind 与各自所有非表达式字段。
2. ToolSkip 的显式 literal true。
3. Producer 的显式 always。
4. RulePolicy/Conflict/Coverage 的闭合常量契约；这不意味着实现 Rule AST 或执行器。
5. 对不支持的复杂条件 fail closed，既不解析执行，也不静默退化为 true。

不能自动宣称解决的部分：

- ApplicabilityPolicy 的简单 AST-free wire 没有既有定义。
- 新增 `context_equals`、`all_of_conditions` 或 `condition_rules` 本质上仍是在创造表达式语言；改名不能消除 AST 分期问题。
- identity_constraints 已有 OR/AND 语义，但它们与 `condition_ast` 是合取关系，不能删除后者并宣称完全等价。

**可接受的主控裁决路径：**若主控认定最新“只允许简单策略”明确授权了 B 的严格能力子集，可冻结 literal-only/always-only 的 B 支持域，并清楚声明“16 种 Policy 全部具备闭合模型；历史复杂条件分支尚不支持”。这避免新增业务求值语义，但不满足旧计划的“完整历史条件分支”兼容目标，验收措辞与 C 依赖必须同步改写。

**若用户要求的完整仍指旧条件能力，最小用户决策只有一个：**

> WP-01B 是否接受 16 种 Policy 的简单声明子集，复杂条件配置一律拒绝并留 WP-01C；还是允许把既有 AST 数据结构前移到 B，但 TypeChecker/Evaluator 留 C？

我不建议在未澄清这点时把 D01 标成无条件关闭。

### 建议的必需测试

全部是设计建议，未运行：

- 正常分类 member；未知词表/版本/digest/member；错误 semantic domain；示例值不能自动进入注册词表。
- selector source 与 provenance source 互换拒绝；Profile 不增加 context schema 字段。
- descriptor 缺失、错 digest/Schema kind、重复身份异内容；typed-value 包装路径与原始 DTO 路径混用拒绝。
- 空/超深路径、未知字段、动态 index/JSONPath 拒绝；`("a.b",)` 按一个精确字段解析。
- optional 父 Record 中 required 叶仍可能缺失；required mapping 无 block 策略拒绝。
- integer/number、不同 enum members、单位、范围及 typed/raw 编码不匹配。
- Facet union-only、同形异 digest、Profile 缺 Facet/context、反向 membership 拒绝。
- 16 Policy 模型都能生成严格 Schema；复杂条件拒绝不能靠 Any、自由 dict、opaque JSON 或未解析 ForwardRef 获得通过。

D04 无需新增业务分类成员或生命周期行为即可推进；D01 的剩余问题是交付能力与既有契约兼容边界，不能通过重命名类型消除。
