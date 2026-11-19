# WP-01C C2 规则声明交接

基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。C2 局部验收 **PASS**，证据标签为 **TEST_PASSED / RUNTIME_NOT_RUN**。整包评审、Schema 导出与全量回归由主控安排。

## 交付和契约

- `src/jimu_runtime/domain/rules/definitions.py` 提供版本显式的 `RuleDefinition`、`RuleSetDefinition`、`RuleContentRef` 和原样复用的 `RuleSetContentRef`。规则类别使用 Domain Model 的类别标识符。RuleSet 候选绑定可省略，提供时须使用带版本与 digest 的 ModelBinding 引用。
- Rule 的必需和可选 Fact 选择器必须各自无重复、彼此不相交，合计恰好等于条件中 Fact 选择器的集合。精确比较覆盖 scope、key、Schema、producer 和 path。表达式可在多处引用同一个已声明依赖。Rule 条件使用 B 的 AST 和 `rule_condition` 输入域。RulePolicy 和 ConflictPolicy 通过精确 Policy 引用关联，沿用 `all_mandatory`、`block_unavailable` 和 `collect_all` 约定。
- `src/jimu_runtime/domain/rules/operators.py` 提供固定版本的十个白名单描述符。每个签名固定 arity、输入约束、Boolean 输出和非 known 传播方式。描述符为只读数据。
- `src/jimu_runtime/domain/rules/type_results.py` 提供不持久化的 `TypeCheckResult`。成功分支记录输入域与推导出的 Boolean 类型，`invalid` 和 `incomplete` 分支保留至少一条有路径的诊断。
- `tests/{unit,contract,negative}/test_c_rule_declarations*.py` 和 `tests/fixtures/wp01c/rule_declarations/` 提供新契约正负例和 B AST 兼容用例。C5 可使用 `rule-valid.json`、`rule-invalid.json`、`rule-set-valid.json` 和 `rule-set-invalid.json` 作为两个持久根的对照数据。

## 验证

[verification.json](verification.json) 记录各命令、环境版本、UTC 时段、退出码、stdout 和 stderr。[JUnit](pytest.xml) 记录 198 项定向测试，包含 71 项 C2 测试和 127 项 B AST、绑定回归，零失败、零跳过。Ruff lint、格式检查与四个 C2 源文件的 mypy 均退出 0。[SHA256SUMS](SHA256SUMS) 封存源码、fixtures、测试与证据。

开发期间定向 pytest 初次退出 1（66 通过、2 失败），第二次退出 1（69 通过、2 失败）。两次使用 `PYTHONDONTWRITEBYTECODE=1 uv run --locked --no-sync python -m pytest -q tests/unit/test_c_rule_declarations.py tests/contract/test_c_rule_declarations_schema.py tests/negative/test_c_rule_declarations_invalid.py`。两个用例原本比对带默认值的 Pydantic 输出和省略这些值的 B 输入 JSON。修正后以原始 B AST 可直接解析、与共享 AST 推导数据相同、序列化结果可被 Schema 与 Pydantic 再读作为验证。修正后的同命令退出 0（71 通过）。

## 验收边界

本批次代码验收覆盖版本与引用形状、依赖分区、白名单签名、结果分支及 B AST 兼容性。互斥依赖和输入域属于 Pydantic 数据验证，其中部分约束超出生成的通用 AST JSON Schema 表达范围。Policy 子类型、Candidate ModelBinding capability、Field Schema 和 Producer 实际内容的匹配需要调用方提供有限环境或后续 Bundle 校验。测试不建立 mandatory producer closure、完整 Rule 扫描或 runtime 事实。未引入数据迁移、外部依赖、动态执行入口或部署变更。

下一批由 C3 使用声明契约进行静态类型检查，C5 负责生成器登记与整包验收。
