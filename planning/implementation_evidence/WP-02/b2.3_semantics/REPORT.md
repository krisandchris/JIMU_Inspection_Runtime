# WP-02 B2.3 语义输入重建与离线校验

工程状态 **PASS** · **TEST_PASSED** · **RUNTIME_NOT_RUN**

## 工程证据

- `bundle/semantic_inputs.py` 从已解析制品目录、闭包和 Schema 投影重建 WP-01B Definition 关系视图。用 Profile 的正向引用、Region 的 Schema pin、ExecutionPolicy 引用的 ContextProvenancePolicy 和实际使用点建立绑定。未以模板或结构相似性代替引用身份。
- `bundle/semantics.py` 是编译与独立准入共用的本地检查入口。它重新建立 Fact Schema Registry 和类型视图，调用 B 的关系、映射与 Policy 检查，再核对来源完整性、受信能力和协议快照、Workflow 版本、Question 主输出兼容性、几何语义和静态资产。它调用 Producer/Rule 校验独立复算目录并检查作用域和必需生产方。
- `domain/validation/{definitions,tool_plans}.py` 对纯校验入口作有限调整。现有 `MappingTypeContext` wire 仍要求两个 Schema pin，旧调用保留严格重验。Bundle 从真实 Tool step 重建的 `ToolMappingUse` 可省略未使用的 Object/Image 视图。实际读取对应源时仍强制存在并精确匹配 pin。调整不增加持久模型、外部访问、依赖或状态写入。
- 校验报告区分 host 注册和真实 Task 行为。粒度包括条件操作数、使用位置、RuleSet、生产者目录、必需依赖、Closure、协议、Workflow、审核记录和外部能力。未能走到 Producer/Rule 检验的报告将相关项标 incomplete。编译期没有 Task Context Snapshot、实际 Fact 或可信物理观测，运行时条件求值、真实上下文来源与独立签名检查分别保留。选择策略缺少 V1 可验证的独立选题审核时只保留配置的保守回退，并记录审核缺失。

## 测试与环境

本地 Linux、CPython 3.12.3、uv 0.9.15、Pydantic 2.12.5、pytest 8.4.2、Ruff 0.14.14、mypy 1.18.2、jsonschema 4.25.1、rfc8785 0.1.4。测试输入为本地合成 Bundle、显式可信快照和 pytest 临时目录。

| 命令 | 退出码及结果 |
|---|---|
| `uv run --locked --no-sync python -m pytest tests/unit/test_wp02_semantics.py tests/negative/test_wp02_semantics_invalid.py tests/contract/test_wp02_semantics_contract.py -q` | 0，15 passed in 10.15s |
| `uv run --locked --no-sync python -m pytest tests/unit/test_b_tool_plans.py tests/unit/test_b_definition_relations.py tests/contract/test_b_mapping_contracts.py tests/negative/test_b_definition_relations_invalid.py tests/negative/test_b_definition_conditions_invalid.py -q` | 0，124 passed in 13.59s |
| `uv run --locked --no-sync python scripts/generate_schemas.py --check` | 0，Schema export matches |
| `uv run --locked --no-sync ruff check src/jimu_runtime/bundle/{semantics,semantic_inputs}.py src/jimu_runtime/domain/validation/{definitions,tool_plans}.py tests/{unit,negative,contract}/test_wp02_semantics*.py tests/fixtures/wp02/semantics` | 0，All checks passed |
| `uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/{semantics,semantic_inputs}.py src/jimu_runtime/domain/validation/{definitions,tool_plans}.py tests/{unit,negative,contract}/test_wp02_semantics*.py tests/fixtures/wp02/semantics` | 0，8 files already formatted |
| `uv run --locked --no-sync mypy src/jimu_runtime/bundle/{semantics,semantic_inputs}.py src/jimu_runtime/domain/validation/{definitions,tool_plans}.py` | 0，Success: no issues found in 4 source files |

依据 WP-01C 冻结的 `product_sha256.json`，由 Python `hashlib.sha256` 逐一比较 `schemas/generated/` 下全部 348 个历史文件，其中 347 个 Schema 和一个目录索引，差异 0。上述定向 pytest 无跳过项。中途的 fixture 重跑修正了编译入口加入的来源 ID 绑定，以及同一 Schema 被 Region 和 Snapshot 同时声明时的测试输入歧义。表中记录的是修正后的结果。

## 覆盖与边界

- 真正从编译输出字节重建语义，验证 Context-only 映射无需伪造 schema pin，跨 Region 使用位置全量检查，精确身份门可收敛作用域，Region Prompt 按 Question allowlist 解析。
- 负例覆盖撤销协议、相同 Runtime 版本但不支持的 Workflow、合法但类型不匹配的 Question 响应、缺少 ContextProvenance 字段、缺少 Region owner、同形但不同 pin 的 Schema、未授权 Prompt 字段、Geometry 单位词表域不匹配。
- Runtime 证据 **RUNTIME_NOT_RUN**。快照中已注册的协议表示部署侧声明支持的确切版本和 Schema 对，不能证明真实模型响应质量、实地几何、资源容量或 Task 的元数据来源。该分支不产生数据迁移，不改变已发布 Domain Schema。整包冻结后的独立验收覆盖 compile 和独立 admit 的一起运行。
