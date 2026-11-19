# WP-01C C4 执行契约验收证据

**C4 执行切片 PASS** · `TEST_PASSED` · `RUNTIME_NOT_RUN`

## 范围和基线

主控依据已复核的 WP-01C 计划放行 C4 执行记录、配置 pins、事件和纯状态校验。工作树从 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 开始。启动检查时已有计划、授权和实施证据目录的变动，本切片在指定新文件中工作。基线来源见 [计划](../../../active/WP-01C/PLAN.md) 和 [执行状态](../../../active/WP-01C/EXECUTION_STATUS.md)。

## 交付和设计

- [executions.py](../../../../src/jimu_runtime/domain/runtime/executions.py) 提供独立版本 1.0.0 的 `ExecutionRecord`、`NodeRun`、`Attempt`、`ExecutionEvent`。配置 pins 在执行建立时固定 Release、Bundle digest、输入 digest、Workflow/Runtime 版本、模型部署约束和 Tool 实现版本。Context Snapshot pin 在运行时只能附加一次。
- 实际模型身份沿用网关负责的 `ActualProviderIdentity`，映射清单中的 `ActualDeploymentIdentity`。Execution 仅追加实际调用记录引用。迟到响应事件附带已启动的终态 Attempt 关联，追加后保留原有状态和时间。
- [transitions.py](../../../../src/jimu_runtime/domain/validation/transitions.py) 按规格 03 §9 分开校验四类状态域。NodeRun 第一次进入运行时须同时给出首个已启动 Attempt。预启动 blocked/skipped 有结构化原因且没有启动时间或 Attempt。后续重试保留 NodeRun running，新增 Attempt 编号严格增加。对终态 NodeRun 发起重试或重放时，须给出显式关联的新 NodeRun 身份。
- B 的 `AttemptOwner`、运行态引用和状态枚举保持原 wire 并直接复用。NodeId 提供 N01–N06 的执行域类型。模型使用有界不可变集合和 UTC 时间。NodeRun 的 config_ref 引用固定的执行策略；上游运行记录引用同时携带 digest。
- [fixture](../../../../tests/fixtures/wp01c/executions/contracts.json) 提供四类独立根的确定性输入。测试包含[全矩阵与顺序](../../../../tests/unit/test_c_execution.py)、[Draft 2020-12 结构与往返](../../../../tests/contract/test_c_execution_schema.py)和[负向边界](../../../../tests/negative/test_c_execution_invalid.py)。

## 验证

锁定 CPU 环境为 Linux x86_64、Python 3.12.3、uv 0.9.15、Pydantic 2.12.5、pytest 8.4.2、jsonschema 4.25.1、Ruff 0.14.14、mypy 1.18.2。锁摘要、完整命令、临时路径、时戳、输出及退出码见 [commands.json](commands.json)。

| 最终检查 | 退出码 | 结果 |
|---|---:|---|
| C4 执行测试和 B 相邻归因/Fact 回归 | 0 | 291 passed，0 skipped。[JUnit](pytest.xml) |
| Domain 导入和 CPU 依赖边界复查 | 0 | 2 passed，0 skipped。[命令](boundary_recheck_commands.json) [JUnit](boundary_recheck_pytest.xml) |
| Ruff lint | 0 | All checks passed |
| Ruff format | 0 | 6 files already formatted |
| mypy | 0 | 2 个目标源文件无问题 |

早期定向检查曾因三份测试使用同名模块而收集失败，已改为不同文件名。Ruff 发现的相邻编号校验问题已改用 `pairwise`。后续仓库级边界测试在其他 C4 模块同步期间因 `InvocationContractSnapshot` 未解析失败，保留该次[命令](integration_boundary_commands.json)和 [JUnit](integration_boundary_pytest.xml)。相关模块提供符号后，针对相同边界用例复查通过。先前通过的完整定向检查见 [preflight_commands.json](preflight_commands.json)。最终交付[文件摘要](sha256.json)独立保存。

## 安全、迁移与后续门槛

本切片为新增数据与校验，不涉及数据迁移。状态 guard 表达调用者提供的资格，真实 lease、fencing、持久化、生产者权威资格和执行由相应工作包验证。单记录的 Draft 2020-12 schema 检查字面版本和结构；跨记录的 pin 关联、时间顺序、状态约束由 Pydantic 和纯验证函数检查。运行时与基础设施验证状态为 `RUNTIME_NOT_RUN`。本切片复核通过，WP-01C 整包生成器注册、生成产物和全量冻结快照验证由 C5 负责。
