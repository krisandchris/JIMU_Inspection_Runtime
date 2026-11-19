# WP-02 Schema 类型视图验证

`TEST_PASSED`。本记录覆盖 B2.3 和 B2.5 共用的类型视图解析器，验收范围为从已校验的制品目录重建 WP-01 描述符。Git 基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，工作树包含 WP-01C 封存基线及并行 WP-02 实现。

- 产品实现位于 `src/jimu_runtime/bundle/schema_views.py`。入口为 `build_schema_descriptors(catalogue, *, schema_index=None)`。调用方可复用同一 Catalogue 构建的闭包索引。
- 正反例位于 `tests/{unit,contract,negative}/test_wp02_schema_views*.py`。构造器通过实际 JSON 字节、JCS 和清单定位建立 Catalogue，未传入任何描述符或编译器侧表。
- 测试素材位于 `tests/fixtures/wp02/schema_views/support.py`。使用现有 WP-01 ToolResult 生成 Schema 作为独立基线，显式特化 payload 和 evidence。

## 类型来源与范围

| 视图 | 来源与检查 |
|---|---|
| Raw DTO | 实际 Draft 2020-12 的封闭 Object、确定边界的 String/List、字符串枚举和带包容性上下界的数值。由 properties 和 required 复原字段路径及缺失语义。 |
| Typed Value | 对外表示采用明确的 kind.const 和 value。Record/List 的子节点逐个验证其包装和类型约束。精确的字段类型和表示冲突有具名诊断。 |
| Mixed Tool input | 外层闭合 Record 同时容纳 raw 与 tagged Typed Value 字段。字段名、是否 required、类型和表示取自实际内部 Schema。 |
| ToolResult | 以 WP-01 Pydantic 生成的三个状态分支为基线。Succeeded 和 Partial 的 payload 必须有相同的有限 typed shape，Failed 保留既有信封约束。局部 Schema 中证据数组用封闭 slot item 分支、name.const、minItems/maxItems 以及 contains/minContains/maxContains 锁定各槽位。输出名称、相对 payload 路径和 required 来自绑定的 Tool 定义，同一精确 Schema 的多个 Tool 定义必须一致。复用 WP-01 检查器验证 Tool/Fact 兼容。 |
| F04 Value Schema | 从每个已 pin 的 FactSchemaDefinition 重新计算类型、生成参数、派生引用和规范子 Schema 字节。共享类型的不同 Fact Schema 保留各自的语义身份。 |

仅在断言完全可投影时生成视图。通用 ToolResult payload、泛化 evidence 数组、无界 String/List、开放 Object、额外断言、递归投影、仅有生成形状而缺失 pin 的孤立 value_schema 和歧义的 Schema URI 均拒绝。Evidence slot 模板使用已批准的标准 Draft 2020-12 关键词，不增加 JSON 实例字段。若要求表达其他等价的 Schema 组合，需扩展并验证受限投影规则。直接导出的完整 ContextSnapshot 等生成 Schema 可能含 pattern、format 或复杂联合，必须使用与消费者匹配的可证明视图。类型投影只恢复语法和结构，单位和业务词表身份来自固定定义及使用点。ToolResult 信封中的跨字段运行状态约束继续由 WP-01 模型和 checker 执行。

## 实际验证

环境为 Linux x86_64，Python 3.12.3，uv 0.9.15，Pydantic 2.12.5，pydantic-core 2.41.5，rfc8785 0.1.4，jsonschema 4.25.1，referencing 0.37.0，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。

```sh
uv run --locked --no-sync python -m pytest -q tests/unit/test_wp02_schema_views.py tests/contract/test_wp02_schema_views_contract.py tests/negative/test_wp02_schema_views_invalid.py tests/unit/test_b_schema_descriptors.py tests/unit/test_b_projection.py tests/unit/test_b_tool_plans.py --junitxml=planning/implementation_evidence/WP-02/schema_views/pytest.xml
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/schema_views.py tests/unit/test_wp02_schema_views.py tests/contract/test_wp02_schema_views_contract.py tests/negative/test_wp02_schema_views_invalid.py tests/fixtures/wp02/schema_views/support.py
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/schema_views.py tests/unit/test_wp02_schema_views.py tests/contract/test_wp02_schema_views_contract.py tests/negative/test_wp02_schema_views_invalid.py tests/fixtures/wp02/schema_views/support.py
uv run --locked --no-sync mypy src/jimu_runtime/bundle/schema_views.py
```

上述命令退出码均为 0。pytest 140 项通过，零跳过，包括 40 项 WP-02 定向用例。完整 ToolResult 测试分别检查一项必需单引用及一项可选多引用 evidence slot，使用 Draft 2020-12 校验实例，再走 Pydantic ToolResult 解析和 ValueContract。已覆盖失败分支嵌入 payload、篡改 EvidenceRefs、泛化 evidence、伪造本地索引、重新计算摘要掩盖 F04 变化及外部 Schema 缺失等负例。

`RUNTIME_NOT_RUN`。本批无服务、数据库、GPU、真实模型或部署执行。整包 compile 和独立 admit 的字节交付验证分别归各入口。无旧 Domain 线格式变更，无迁移。
