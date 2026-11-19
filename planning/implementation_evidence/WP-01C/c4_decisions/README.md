# WP-01C C4 决策与覆盖记录

授权范围为静态契约、有限记录关联和隔离 CPU 测试。交付包含
`runtime/decisions.py`、`runtime/coverage.py`、
`validation/decision_coverage.py`、`validation/coverage_sources.py` 及同名测试和预制 fixtures。

- 四个独立持久根为 CandidatePrediction、DecisionRecord、ExecutionCoverage 和 ProfileCoverage，均有显式版本、正反 JSON fixture 和离线 Draft 2020-12 检查。以 RuleSet/Rule 的精确路径记录义务，Object 保留 Profile 身份。ExecutionCoverage 的 N02 属性采用 `discovery_model_invocation_refs`，可引用一个选定 N02 Attempt 的多组绑定调用。这是 C 根首次冻结前的字段决定，已有 A/B 契约保持原格式。
- 预制结果分别储存适用性、Required Evidence Guard、谓词真值和 collect_all 追踪。已使用的 optional Fact 携带自身的精确 selector 与已选 authority revision。Runtime Authority 组合入口用已验证的完整 FactRecord 对照已消费记录，覆盖相同 ID 但内容变化的负例。
- 有限校验对照已选真实记录的 N01 Snapshot/Execution pin、N02 输入输出及分组调用、N03 Profile GroundingRecord/Object、N06 upstream Object/真实 Attempt。上下游证明不足时，空目标、未执行和 UNKNOWN 保持各自语义。仅核对调用者提供的有限记录和配置视图。
- 无持久数据迁移。运行集成、真实模型、外部来源真实性和未提供历史的完备性仍由后续工作包验证。

**TEST_PASSED**。定向 pytest 54 个通过，Ruff lint/format、四个源码文件 mypy、git diff 检查退出码均为 0。无跳过。命令、版本、退出码和文件摘要在 [verification.json](verification.json)，pytest 明细在 [pytest.xml](pytest.xml)。

**RUNTIME_NOT_RUN**。C4 本分工的静态门禁通过，WP-01C 总门禁由 C5 综合验收。
