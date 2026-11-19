# WP-01C C4 调用审计验证

范围为 F02 模型路由、F03 N02 问题选择、F05 Tool 执行记录，以及 N04 调用合同快照和 N05 结果权威选择的输入证据。基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。工作树包含主控前置计划变更及其他 C 批次的并发修改。

产品代码位于 `runtime/{invocations,selection,tool_executions}.py` 和 `validation/invocations.py`。运行根使用显式 `schema_version`，复用 B 阶段的指纹引用、策略、Attempt 和权威结果有限描述符。ToolResult 的命名状态使用既有 FactStatus，结构与持久化 ToolExecution 分层。N05 审计可保留未派发或实际版本不可用的失败，生成 Tool Facts 及纳入权威选择需要解析实际版本。

`tests/fixtures/wp01c/invocations/contracts.json` 包含正例和待解析的有限关联材料。`tests/{unit,contract,negative}/test_c_invocation*.py` 覆盖 JSON 回读、结构拒绝、错作用域、确切审查证据、调用预算、分支归属、命名输出和 Fact 来源。

- TEST_PASSED。Python 3.12.3、uv 0.9.15，锁定环境中的 80 项定向用例通过，Ruff lint 和 format 以及 4 个源码模块的严格 mypy 通过。命令、环境、退出码及前后摘要见 [results.json](results.json)，pytest 逐例结果见 [pytest.xml](pytest.xml)。
- RUNTIME_NOT_RUN。使用离线、合成的 Definition、Attempt、记录和审查资料。路由验证使用调用方提供的信任域与资料，重试预算验证使用调用方提供的完整有限清单。运行时真实性、审查资料鉴权、并发准入、真实 Provider/Tool 调度与结果写入继续由后续工作包承担。
- C4 切片工程验收建议 PASS。C5 的 Schema 索引、双次生成、全量回归及持久根覆盖由主控和独立 tester 决定。此切片撤回时可移除新增源码、测试、夹具及证据。
