# WP-01B 最终工程验收报告

日期：2026-11-18（Asia/Shanghai）。唯一计划：PLAN-4-A；Goal：GOAL-002。

**工程结论：PASS。** 八个批准模块、CPU 自动化测试、Primary 直接代码审查及独立 Sol Challenge 已完成；无未解决 C0/C1/C2。用户最终验收仍为 **PENDING_USER_ACCEPTANCE**，不推定任何 Git 或后续 WP 授权。

| 验收轴 | 结论 | 证据范围 |
|---|---|---|
| Code Acceptance | PASS | 完整批准的 Definition & Fact Registry 和有限输入关联校验 |
| Test Acceptance | PASS / TEST_PASSED | 1907 passed，0 failed/error/skipped；规定静态与 Schema 门禁通过 |
| Runtime Acceptance | N/A / RUNTIME_NOT_RUN | 本包纯 CPU Domain 契约组件；无六节点服务、真实模型或工具运行 |
| Independent Review | PASS | Astra 直接审查实际源码、Diff、测试、日志、锁文件；独立 Sol 源码与证据 Challenge |
| Overall Engineering Acceptance | PASS | 包含已披露的非阻断工具限制，不代表运行时或生产准入 |
| User Acceptance | PENDING_USER_ACCEPTANCE | 等待用户最终验收 |

## 1. 基线、授权与执行状态

- 开发分支：`dev/wp-01b-definition-fact-registry`。
- 开发 HEAD：`c25bef668136acc651e7313ba608ec6fdefd39a5`，实施前后不变。
- 独立目录：`../JIMU_Inspection_Runtime_WP01B`。
- 原 main：`51a10416d882fe85e5a1cfc5440eac6498fc48f4`；170 个保护文件及文件集合最终核对一致。
- 当前变更全部留在开发工作区，未暂存、Commit、Push、创建 PR、Merge。此前 Git 授权不作为本次授权。
- Primary 实际 `gpt-6-astra / xhigh`；开发与 Challenge 为原生 `gpt-6.1-sol / medium`。具体任务写入目录显式指定，原生 session 的初始 cwd 不等于所有命令的工作目录。
- GOAL-002 原目标、已完成独立子图及历史证据保留；从全局阻塞改为依赖局部阻塞后持续推进，未重建 Goal、默认选择业务方案或缩减交付。

原始 Goal 授权、两项追加授权及原生 agent 元数据均有证据。用户明确批准 G1-D01 A 及 X-C01 A 后才实施条件分支。完整字段合同见 [CONTRACT_FIELDS.md](CONTRACT_FIELDS.md)。

## 2. 四项初始契约裁决

| 决策 | 冻结方案与实施结果 | 状态 |
|---|---|---|
| G1-D01 | 用户方案 A：共享条件 AST **数据**进入 B，16 种完整 Policy 及 Producer 条件使用同一封闭数据契约；不实现 RuleDefinition、TypeChecker、Evaluator | CLOSED / USER_APPROVED |
| G1-D02 | 规范化 TypeDefinition 投影 + 显式生成参数 → RFC 8785 JCS → 独立 value_schema 与 child digest；父 FactSchema 精确引用 child，不依赖父 digest；独立向量与边界验证 | CLOSED / VERIFIED |
| G1-D03 | N04 整个 ObservationBatch、N05 单个 ToolPlan Step 为显式权威选择单位；完整输出集合、Attempt/Result/Fact 身份及历史关联校验 | CLOSED / VERIFIED |
| G1-D04 | Classification、Context/Input/Output Mapping、Field Selector 均使用有限严格类型及实际 Schema/owner/path/source-target 检查 | CLOSED / VERIFIED |

追加 X-C01：用户批准仅 `tool_condition` 使用当前 Plan 的 `LocalToolFactSelector`；在使用处绑定完整 Plan、严格祖先 Step、映射输出、Fact Schema/Scope。它不反向携带同 Plan Digest，原精确 Selector 保持原 wire；拒绝未绑定/非祖先/错误输入域。Domain、Bundle 说明、ADR、总 WP-01 计划仅作该已授权的窄修订。

历史 D01–D15 已有用户批准，不重写历史 pending 记录，也不把本次 G1-D01–04 与其混淆。

## 3. 八个模块实际交付

下列路径均相对仓库；完整文件及前后摘要见最终变更清单。

| 模块 | 主要实现 | 验收要点 |
|---|---|---|
| Entity / Region | `domain/definitions/ontology.py`、`resources.py` | 严格不可变定义、版本引用、分类/Context/资源契约 |
| Profile / Facet / Question | `definitions/capabilities.py`、`prompts.py`、`validation/definitions.py` | Profile 正向权威关系；Facet Schema 精确交集；一个 Question 一个主 Fact；Prompt/Question/Schema 引用一致 |
| ModelBinding / Tool / ToolPlan | `definitions/models.py`、`tools.py`、`tool_mappings.py`、`tool_outputs.py`、`validation/tool_plans.py` | Remote Primary、本地例外/显式 fallback 数据；模型与工具分离；Plan 本地 DAG、输入/输出映射 |
| Policy Catalog | `policies.py`、`conditions.py`、`validation/policies.py`、`validation/conditions.py` | 完整 16 kind，封闭 union；条件结构、输入域、深度/数量/引用约束；不求值 |
| FactSchemaDefinition | `fact_schemas/models.py`、`projection.py`、`canonicalization.py` | 复用 A 七种值；有限 Record/List/Enum；独立投影/JCS/child digest，无父子环 |
| FactProducerDeclaration | `producers.py`、`validation/producer_inputs.py`、`validation/producers.py` | Question/ToolStep/System 三种 Producer，always/tool_condition 两种激活声明；Scope、Key、输出槽、版本、Schema 关联 |
| AuthoritativeResultSelection | `runtime/facts.py`、`authority.py`、`candidates.py`、`validation/authority*.py`、`producer_authority*.py` | 显式整组选择；历史保留；静态双向输出关联；非 last-write-wins |
| Fact Schema Registry | `fact_schemas/registry.py` + 上述纯校验组合 | 精确注册、冲突、冻结、查找、值/路径、child digest、Producer/Authority 关联；无服务或持久层 |

共享支持使用 `definition_support.py`、`definition_enums.py`、`schema_types.py`、`fact_selectors.py` 和 `validation/issues.py`；没有复制 WP-01A 引用或更改 A 的数值语义。

## 4. 依赖 DAG 与公开接口

```text
WP-01A → shared strict types / schema descriptors / diagnostics
        → Ontology + Model/Tool/Prompt + FactSchema/JCS/Registry core
        → condition data + 16 Policy definitions
        → finite condition-reference binding
        → Producer catalogue + declaration/Fact validation
        → complete finite Producer/Attempt/Result/Authority composition
        → 162 Schema export → stable-source full regression → independent review
```

跨定义通过精确 ContentRef；底层模型不反向导入校验器/Registry。X4 不导入 Producer，避免 X4↔X2 循环。没有全局可变注册表或隐式取回。

| 接口 | 作用及边界 |
|---|---|
| `validate_condition_data` / `condition_references` | 校验封闭 AST 数据与输入域，列出实际引用；不推导操作数语义或真值 |
| `validate_condition_references` | 结合显式有限目录校验 Context/Fact/Record path/局部祖先引用；返回已执行与延后检查 |
| `validate_definition_relations` | Definition/Policy/Prompt/Model/Schema/owner 的有限关联校验 |
| `validate_producer_catalogue` / `validate_fact_producer` | 声明目录及单个 Fact 对 Producer/Schema/Scope/来源的合规检查 |
| `FactSchemaRegistryBuilder` → frozen `FactSchemaRegistry` | 显式构建与冻结、精确引用及值/路径校验，无网络/数据库 |
| `validate_producer_authority_contracts` | 完整有限组合：注册目录、Execution pins、候选、结果、选择及历史检查；不认证外部来源真实性 |

局部 API 的 `deferred` 检查不被笼统删除。组合器只替换实际完成的三个局部延后项，保留错误、不完整项与明确属于后续包的检查。

## 5. Registry、Producer、Attempt 与 Authority 完整性

1. Schema 注册使用完整 kind/id/version/digest 与独立 child Schema；重复相同注册和冲突分别处理，冻结后不可变。普通 JSON 排序没有代替 RFC 8785。
2. Question Producer 对每个精确 Profile/Question 恰一个主 Fact；有效 Question 集合有独立 pinned 选择描述，不由 Fact 自证完整。`optional_source=all` 不能遗漏声明 Optional Question。
3. Tool Step 只生产明确映射/声明的 Fact；多个输出一对多，required/optional、emitted/absence 有独立类型。工具原始 JSON 不直接成为无约束 Fact。
4. System Producer 支持已声明 object/execution Scope；执行级 Fact 仍限 System。空输出也要求正确 Execution pins/来源描述。
5. Producer ID/version、Schema identity、Fact key、scope 和输出槽均校验；System 不同 Fact key 不能复用同输出槽绕过冲突。
6. 有限原始候选数据拒绝重复 JSON key、非法数字、extra、错误关联；不同 Attempt 候选不互相覆盖。所有提供的目录（含未使用目录）都预检；合并来源不设“后者优先”。
7. N04 不从不同 Attempt 拼接权威批次；N05 每个 Step 显式选择完整输出。selected 必须满足成功、到达资格、关联和完整性；有效但未选中的失败/晚到/部分输出保留为历史。
8. Result→Fact 与 Fact→Result 双向关联检查，缺失为 incomplete，矛盾为 invalid；零 Fact 的未选 Result 仍校验静态 owner/Attempt/NodeRun/Execution/Plan 身份。
9. 权威选择不因新 Fact 晚写而改变；替换需显式选择记录及一致的历史。动态 activation 的真假、生产环境历史是否完整、实际 Actor/Invocation/信任与 Release 准入继续明确延后。

## 6. 实际测试与静态检查

最终冻结源回归：**1907 passed / 0 failures / 0 errors / 0 skipped**，173.63 秒。按测试目录计：unit **552**、contract **305**、negative **1050**；这是目录分类，不把 unit 数伪称全部正向，也不把各模块重叠次数相加。

覆盖正向、负向、边界和整改回归：引用/owner/版本、精确 Facet 交集、全部 Policy、条件输入域及有界解析、独立 JCS 向量/数值边界、Record/List/Enum、Producer 基数和冲突、Scope/Attempt/缺失输出、显式 Authority、历史保留、无隐式覆盖、双阶段 Schema/语义校验、Domain import graph 和全部 X-I 修复。

实际 cwd 为开发 worktree；最终命令原样记录如下（每条 exit 0）：

```text
uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q --junitxml=/mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01B/planning/implementation_evidence/WP-01B/execution_03/logs/integration/run_20261117T181535_729429Z_c92df4e0/final_pytest.xml
uv run --locked --no-sync ruff check .
uv run --locked --no-sync ruff format --check src tests scripts
uv run --locked --no-sync mypy src/jimu_runtime
uv run --locked --no-sync python scripts/generate_schemas.py --check
```

| 检查 | 实际结果 |
|---|---|
| Ruff `check .` | PASS |
| Ruff format `--check src tests scripts` | PASS，124 文件 |
| mypy `src/jimu_runtime` | PASS，48 源码文件 |
| Schema `--check` | PASS，与当前源码生成内容一致 |
| 文件稳定性 | 每条命令和全程前后快照一致；当前 305 个产品/测试/Schema/配置文件与测试快照相同 |

原始 stdout/stderr、argv、cwd、时间、exit、JUnit 和各步快照保存在唯一最终 run 目录；之前失败与整改日志没有覆盖。测试期间未调用真实 Runtime、GPU 或外部模型。

## 7. Schema 验证与依赖

生成 **162 份 Pydantic JSON Schema + 1 index**，完整来源符号与 SHA-256 在 `schemas/generated/index.json`。Draft 2020-12 离线校验、local `$ref` 闭包、严格属性、命名 union、冻结源重复生成/只读 `--check` 由真实 CLI/Schema 测试覆盖；E2 最终 41 项模块测试通过。工程 Schema export 不是 WP-02 Bundle Compiler，也不是父/Bundle 摘要实现。

Pydantic 是模型权威。JSON Schema 数学 integer/number 与 Python 严格 int/float/bool、UTC 日历与词法、构造/复制实例污染、跨引用语义存在已披露边界；保留两阶段校验，未为表面一致放宽 WP-01A。

实际环境：Python 3.12.3、uv 0.9.15、Pydantic 2.12.5、pytest 8.4.2、jsonschema 4.25.1、Ruff 0.14.14、mypy 1.18.2、rfc8785 0.1.4、uv_build 0.9.15。依赖范围有界；B 仅增加经批准的局部 JCS CPU 库，A 原有锁定版本保留；execution_03 无新安装。无 GPU、模型 SDK、服务依赖。完整22项安装版本清单保存为证据。

`uv.lock` SHA-256：`10d7ad828a27a9ea35326ad95f73becedfdc9237bde8fc746a119833191062eb`。

## 8. Finding、整改与限制

初始 G1-D01–04、B-F05/06 工程范围已闭合。execution_02 的 I01–I15 已有整改/明确处置，历史失误和失败原样保留。execution_03 X-I01–X-I07 全部关闭：AST 超限预遍历、Producer 激活 Scope、已执行引用检查报告、Context owner 重复、exporter 调用参数、全结果静态关联/Optional 完整性、输出反向关联。Primary 直接复核最终代码与日志；独立 Sol 完成再审，最终全量回归覆盖当前字节。

保留以下非阻断限制，不伪装成成功：

- **X-N01 / C3**：额外命令 `uv run --locked --no-sync mypy scripts/generate_schemas.py` 实际失败 7 项（jsonschema stubs、installed package 的 py.typed 导入及既有 bootstrap annotation）。规定 gate 始终为 `mypy src/jimu_runtime`，实际通过；未添加 ignore/stub/依赖掩盖额外失败。exporter 已有41项 CLI/Schema 测试及 Ruff/format/直接审查，其额外单文件完整 typing 未验证。
- 历史 `ruff format --check .` 命中3份封存审计脚本，实际失败。保留原字节；维护代码格式 gate 通过124文件，不宣称全目录 format 通过。全目录 Ruff **lint** 通过。
- execution_02 Sol 曾将9个新文件误写到原 main 工作目录，随后只迁回自己的文件；最终170保护文件/集合与原 HEAD 一致。没有伪造迁移前 hash，不声称整段工作从未触碰原目录。
- 早期个别模块命令仅保留原始 transcript 而无逐命令 JSON；最终整包所有门禁具有完整 JSON/raw/JUnit/hash。早期并发源变化快照未当作最终字节证明。
- 合成 CPU fixture 不证明真实模型质量、GPU容量、生产历史完整性或运行时授权。有限检查报告不能单独作为真实 Release/Bundle/证据准入证书。

无未解决 C0/C1/C2，无待决业务范围。完整登记见 [FINDINGS.md](FINDINGS.md) 及最终 `findings.json`。

## 9. 变更、兼容性、安全及证据

相对开发 HEAD：41 个新 Domain 源文件，83 个新增/修改测试及 fixture 文件，1 个工程 Schema 脚本，163 个生成文件；另含 pyproject/lock 的局部 JCS 依赖变更、4项批准的架构/总计划文档窄修订、当前规划/验收文档与审计证据。完整逐文件清单包括 untracked，不以普通 `git diff` 漏掉新代码。没有删除受保护历史。

兼容性：WP-01A 公开类型、原精确 Selector wire 和状态语义保持；Catalogue ref 只提取为唯一类型并原路径 re-export；B 新类型增加 Schema，非 A 破坏性升级。新增局部 Selector 必须通过新 kind/use-site 识别，旧消费者不能静默接受；将来 Bundle/Release 版本准入由 WP-02 实施。无现有数据库或用户数据迁移。

安全：Domain 无服务/GPU/模型 SDK；不执行条件数据，不接受动态代码；严格额外字段、有限输入预算、显式引用、无隐式 provider 替换。没有真实凭据、模型调用、部署、需求平台变更。没有修改 AGENTS 或原始 PACKAGE_MANIFEST。实际入口及 import boundary 已纳入测试，非生产安全认证。

[最终证据目录](../../implementation_evidence/WP-01B/execution_03/README.md) 包含 baseline/before、环境与锁、授权、原生 agent 公开结论、逐模块失败和成功日志、Primary Review、最终全量 run、变更清单、SHA-256 与 patch。旧根目录 G1、execution_02、WP-00 各轮、WP-01A 证据全部保留。

关键摘要：

| 证据 | SHA-256 |
|---|---|
| 最终 run `summary.json` | `67fc382ffbf81868587b532019312f466a6c50c1e1280b41fad01065616dacea` |
| 最终 `final_pytest.xml` | `85f72005eb4b68b16e7e106b2f4404d91471460f6a14f904a5059d6cfec72ae8` |
| 162 Schema index | `ce0bc5b36eb5b8a3dac04adc2f3d97c558f0a10c4487678f018cbafcd7a476b0` |

最终 `after_sha256.json` 记录交付文件摘要；`SEAL.json` 另封存 manifest/patch/清单，明确排除自摘要，避免摘要循环。`changes.patch` 包含 HEAD→当前源码/测试/生成物/配置/相关文档（包括未跟踪文件），证据目录以逐文件摘要审计，不把复制的历史证据展成递归 patch。审计文件校验是静态证据核对，不计为功能测试。

## 10. 停止门禁与 WP-01C 建议

WP-01B 工程 PASS 后停止开发，等待用户最终验收及后续独立 Git 授权；没有启动 WP-01C。若用户决定发布/合并，重新获取实际 main HEAD，不能继续使用 A 或本次旧基线推定 C 的基线。

后续只建议先做 C 的有界计划与独立 Challenge：复用已批准条件数据，规划 RuleDefinition/RuleSet、静态 TypeChecker、FactSelector/类型/单位约束及 mandatory/collect_all/三值/适用性契约的测试边界；Evaluator、Workflow 与 Bundle 执行仍按各自工作包授权。具体 C 计划经用户批准后才能实施，不把 B 的通过视为 C 的授权。

最终标签：**TEST_PASSED / STATIC_REVIEW_ONLY（独立审查方式）/ RUNTIME_NOT_RUN（Runtime N/A）**。
