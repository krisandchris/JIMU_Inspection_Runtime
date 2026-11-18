# Sol Medium read-only review D

Source: native task `/root/wp01b_d_tests`.
Captured by Primary from the actual first final response; no claim of code acceptance.

`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。全程只读，无写文件、测试、安装或 Git 变更。

已核对：`main`，HEAD=`51a10416d882fe85e5a1cfc5440eac6498fc48f4`；tracked 文件无修改，未跟踪项为已知 `planning/acceptance_evidence/`、`planning/transition_evidence/`。A 的 324 passed 是历史证据，本轮未重跑。

**必须先冻结的范围冲突**

| 项目 | 依据 | B 门禁建议 |
|---|---|---|
| 16 Policy 包含 AST 字段，但 B 禁 RuleAST/TypeChecker | `specs/01…:1167` Applicability.condition_ast；`:1174` ToolSkip.condition_ast；`:1021` producer.activation_condition | 阻塞：须确定 B 是明确延后这些 Policy，还是另批最小共享表达式契约；不能以 Any、opaque dict、自由字符串、假 AST stub 填满16种 |
| Fact Registry.validate_fact 所需完整 Fact | `specs/01…:462` Fact；`:1073` producer/Attempt/provenance关联 | 阻塞：若 B 不含完整 Fact envelope，只能交付 payload/type/ref 验证；不得声称 Fact 已接受。若 B 含 Fact，须明确批准字段清单、状态分支、scope/ref及authority输入 |
| AuthoritativeResultSelection 引用整批/step结果，但对应运行记录可能在 C | `specs/01…:1076–1088` | 可用明确类型的有限测试目录校验已给记录；不能只检查 ID 格式就宣称整个 batch/step 一致，不得为测试偷偷实现 ObservationBatch/ToolExecution/runtime |
| ToolPlan DAG 与完整映射不是 B 的运行交付 | `planning/WP-01_IMPLEMENTATION_PLAN.md:346`；Bundle spec §5 | 本阶段仅本地字段、命名输出、声明路径及有限引用类型一致性；完整闭包、可达性、条件生产者完备性、DAG编译为 WP-02。需在 B 单独澄清是否包含“本地 DAG 纯检查”，否则不做 |
| value_schema生成与JCS | `specs/01…:923–1010`；`src/…/base.py:24` | `stable_json()`明确不是JCS。若 B 承接 F04，须冻结投影/profile/generator、规范数值表示与canonicalization实现来源，不能用基础快照冒充 |

**双阶段验证及 F01 承接**

A 的既有界限必须保留：

- `tests/contract/test_json_schema.py:163`：结构通过但跨字段 enum/closed Record 语义失败。
- 同文件 `:185`：`IntegerValue(1.0)`、`NumberValue(1)`在JSON Schema可通过，严格Pydantic拒绝。
- `src/jimu_runtime/domain/values.py:294–320`：裸 Value 不赋予 Fact authority；TypeDefinition 必须来自受信配置。
- `planning/implementation_evidence/WP-01A/README.md:179–196`：34份是foundation API审计快照，不是Fact payload生成器。
- 外审 F01 位于 `planning/acceptance_evidence/WP-01A/FINAL_ACCEPTANCE_REPORT.md:34–65`，不同于实施 findings.json 的 Enum Literal F01。

建议每个用例记录 `S=JSON Schema结构`、`D=严格Domain/TypeDefinition/有限refs语义`，authority用例另记录 `A=显式选权关联`。最终接受必须满足所需全部层。预期 `S=PASS,D=REJECT` 不是失败，也不能通过放宽严格类型或跳过负例消除。

**建议测试矩阵**

| 建议模块/文件 | 正向 | 必须拒绝的负例 | 依赖/证据界限 |
|---|---|---|---|
| `contract/test_definitions.py` | Entity/Region/Profile/Facet/Question/Tool/ToolPlan/ModelBinding各最小完整根，Python dict/JSON roundtrip，版本显式 | 缺schema_version/必需字段；额外code/shell/secret/反向Profile字段；错kind ref；跨枚举传入 | 仅A基础类型；不新增API/DB |
| `contract/test_policy_catalog.py` | 每个获批policy分支显式字段；16项枚举清单；各资源/provider/selection分支 | 默认填值、required遗漏、分支外字段、unknown kind；预算0/负数/bool/2^63溢出；required证据非allowed子集 | AST字段冲突冻结前不得将本文件标完整 |
| `contract/test_model_tool_bindings.py` | discovery/observation/grounding用途兼容；remote primary；显式local exception/fallback预制配置 | wrong capability；缺primary/多重ID；remote模式local primary；错误有效期/naive time；无review refs；disabled带transition；未声明fallback端点 | 只静态配置，不证明审批可信、路由或质量 |
| `contract/test_facet_schema.py` | 同一exact Schema属多个Facet；Question主Schema在全部allowlist交集；Profile包含全部Facet与context | 只在union内；空交集；同shape不同digest；不同semantic_key/单位/scope；Profile漏Facet/不兼容context | `specs/01…:914`；有限目录，不Bundle闭包 |
| `unit/test_fact_registry.py` | build→freeze；exact resolve；旧新版本共存；隔离snapshot；known False/0/0.0分别保留 | 相同ID/version不同digest；缺失/错kind/错digest；freeze后register；输入容器后改污染快照；隐式latest替代 | 注册API形状须冻结；不假称hash已验证 |
| `contract/test_fact_schema.py` | schema/type/scope/producer policy完整；每种type与嵌套Record/list | foreign schema、业务enum unknown冒充状态、非法producer kind、任意导入schema/remote `$ref` | JSON Schema结构 + Domain语义分别断言 |
| `contract/test_fact_validation.py`（须批准完整Fact） | known有合法值；其他四态有结构reason且无value；object/execution分支 | known缺值/null；非known带value/null；缺/多object_id；schema/key/scope/profile/execution/producer不一致；bool-as-number；混淆missing与unknown | 不允许只提交 `{kind,value}` 当Fact |
| `contract/test_producer_catalog.py` | Question primary exactly_one；ToolStep多命名输出；System execution输出；合法Record路径 | 两producer同collision domain；同schema也冲突；tool execution scope Fact；producer版本拿Attempt/实际版本替代；target key alias；未声明/非法JSONPath | `specs/01…:1016–1075`；不自动生成catalogue |
| `contract/test_authority.py` | 一Object observation整批；一step Attempt全部映射输出；显式revision和previous ref；历史候选不变 | 无selection自动选最近/最高confidence；跨Attempt cherry-pick；错batch/step/Object/Profile/Execution；重复revision/断链；模型actor；late result改旧revision | `specs/01…:1076–1092`；预制记录校验≠CAS/fencing |
| `negative/test_fact_history.py` | 相同候选键同bytes可判dedup；不同Attempt历史同时存在 | 同candidate key/Attempt不同bytes；把一个Attempt新结果覆盖旧Fact；整批authority挑不同Attempt各Question | 冲突“报告并拒绝”可测；持久冲突event/并发不能声称已验证 |
| `contract/test_schema_generation.py` | 每获批持久根索引；2020-12；离线refs；两次生成一致 | 缺schema/漂移/多余文件；remote ref；子schema带parent id/digest；Record/enum顺序影响；ambient limits | F04完整生成输入未冻结时为BLOCKED，不伪造 |
| `contract/test_dependency_boundaries.py` | 模块层次允许边；只stdlib/Pydantic；纯Registry注入；无运行服务 | domain→SDK/ORM/API/GPU；循环import；Any/eval/exec/dynamic import；隐式I/O | 当前测试需加强，见下 |
| `negative/test_illustrative_examples.py` | 原示例保持illustrative | 原示例缺digest/schema_version、enum unknown、旧简写不被静默“升级接受” | 不改原示例获得绿灯 |

**import DAG 当前测试盲区**

`tests/contract/test_dependency_boundaries.py:22–37` 对 `from jimu_runtime.domain import producer` 仅记录 `jimu_runtime.domain`，漏实际子模块依赖，B模块变多后可能漏环。建议解析本地`ImportFrom`导入目标或要求绝对子模块导入并验证；`__init__`重导出也应计边。这里检查Python源码 AST，不是实现被禁止的 Rule AST。

A已有保障仍须全量回归：严格不可变基类 `base.py:15–21`；ref类型不互换 `negative/test_references.py:122`；外来enum拒绝 `unit/test_foundation.py:127`；数值、闭合Record、深度与不可变容器已有测试，不宜复制成只镜像实现的重复测试。

**未来准确命令建议（本轮均未执行）**

沿用已锁CPU环境，避免`uv run`隐式安装：

```bash
uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q --junitxml=planning/implementation_evidence/WP-01B/pytest_results.xml
uv run --locked --no-sync ruff check src tests scripts
uv run --locked --no-sync ruff format --check src tests scripts
uv run --locked --no-sync mypy src/jimu_runtime
uv run --locked --no-sync python scripts/generate_schemas.py --check
```

最后一项及`scripts`路径仅在B计划确实批准并交付生成器后适用，当前仓库没有该脚本，不能记录为现有可执行命令。缺已锁依赖应停止，不自动sync/relock；不建议B无变更需要却重建/升级环境。

历史CPU版本：Python3.12.3、uv0.9.15、Pydantic2.12.5、pytest8.4.2、jsonschema4.25.1、Ruff0.14.14、mypy1.18.2；来自A证据，本轮未探测实际安装状态。

建议新增独立B证据：`environment.json`、`command_results.jsonl`、`pytest_results.xml`、`validation_stages.json`、`schema_inventory.json`与两次生成比较、`import_graph.json`、`verification_summary.json`、变更前后摘要；不改A证据。每条记录exact command、cwd、起止UTC、exit、counts/skips、输出路径、基线SHA/dirty状态、S/D/A预期差异及无真实API/GPU/DB。

| 验收门禁 | 当前结论 |
|---|---|
| A历史行为/F01保存与回归设计 | PASS（静态设计） |
| B全部根/分支/字段冻结、尤其AST与Fact/authority边界 | CONDITIONAL，需主控计划解决 |
| 双阶段与有限引用正负矩阵 | PASS（测试提案，尚未实施） |
| 测试、生成schema、lint/typecheck | TEST_NOT_RUN |
| CAS/fencing/DB、provider/Tool、N04整批真实调用/Rule求值 | RUNTIME_NOT_RUN，后续WP |
| B可开始实施 | 仍需计划Challenge及用户明确批准 |

自身model/effort没有从任何非白名单运行环境字段探测或推断；无可信可见字段可供报告。等待主控 PLAN 后可继续只读 Challenge。
