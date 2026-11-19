# WP-04 工具步骤子轮次

**范围验收 PASS · TEST_PASSED · RUNTIME_VERIFIED（隔离 PostgreSQL）。** 基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 的共享工作树，含已复核 WP-03 产品和未提交 WP-04 修改。本报告验证子步骤持久化、父子 fence 与历史回读。模型适配器的嵌套 fallback 联测由主控汇总。

## 代码证据

- `tools/step_attempts.py` 持久登记 N05 子 NodeRun 和独立 Attempt 后才允许发送。输入摘要覆盖同一父业务运行、原始上游引用、Object、Plan、Tool、step 和解析输入。 PostgreSQL 在同个事务中写下完整子轮次产物、状态和父输出引用。产物归属实际生成它们的子 NodeRun 和 Attempt。
- `workflow/migrations/004_tool_step_attempts.sql` 添加父子关系、子轮次完结和父输出绑定。引用带上 project、client、Execution、父子运行和对应 fence。旧队列行沿用默认业务作业。在保留证据的前提下，可以由部署配置停用 WP-04 工具适配并保留已安装迁移及读回支持。
- `workflow/jobs.py` 将子作业排除出通用领取路径。父取消、过期、终态或进入重试时清理尚未结束的子作业。子自己的截止时间已过但父子仍属当前所有者时，只有已验证的归一化工具 timeout 可写入非权威 `timed_out`。迟到成功、丢失父 lease 或取消均不能写入成功结果。
- `domain/nodes/tool_execution.py` 接受实际 PG 回读的内部 step 历史并逐步比较原始 ToolExecution、Fact、作用域和 Attempt。没有历史的调用继续校验同轮身份。公开 JSON Schema 保持兼容。已被选定的成功步骤要求相同输入才能复用。有意更改已选步骤输入需要新的明确运行身份。
- `tools/step_attempts.py` 提供嵌套模型路由读取所需的真实父子顺序。只有历史对应父子终态为 failed 或 timed_out、输入一致且实际子产物原件完整时返回此前结果。已开始但只有模糊发送结果、失权或缺少完整记录时拒绝生成路由证明。Gateway 仍需结合原始非迟到 Dispatch、固定绑定和当前有效评测验证具体 fallback 边。

## 验证

Linux x86_64，Python 3.12.3，Pydantic 2.12.5，pydantic-core 2.41.5，SQLAlchemy 2.0.52，psycopg 3.3.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2，Docker 29.9.3。PostgreSQL 16.14 使用本地镜像 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`，测试数据库各自位于独立的 loopback 租约并由 fixture 清理。

| 实际命令 | 退出码和结果 |
|---|---|
| `.venv/bin/python -m pytest -q tests/unit/test_c_node_relations.py tests/unit/test_wp03_jobs_scheduling.py tests/negative/test_wp03_jobs_scheduling_rejections.py tests/unit/test_wp04_tool_step_attempts_postgres.py` | 0 · 28 passed in 18.04s |
| `.venv/bin/python -m pytest -q tests/unit/test_wp03_worker_postgres.py tests/contract/test_wp04_workflow_upgrade.py` | 0 · 7 passed in 32.24s |
| `.venv/bin/python scripts/generate_schemas.py --check` | 0 · Schema export matches |
| `.venv/bin/ruff check src/jimu_runtime/domain/nodes/tool_execution.py src/jimu_runtime/workflow/jobs.py src/jimu_runtime/tools/step_attempts.py tests/unit/test_wp04_tool_step_attempts_postgres.py` | 0 · All checks passed |
| `.venv/bin/ruff format --check src/jimu_runtime/domain/nodes/tool_execution.py src/jimu_runtime/workflow/jobs.py src/jimu_runtime/tools/step_attempts.py tests/unit/test_wp04_tool_step_attempts_postgres.py` | 0 · 4 files already formatted |
| `.venv/bin/mypy src/jimu_runtime/domain/nodes/tool_execution.py src/jimu_runtime/workflow/jobs.py src/jimu_runtime/tools/step_attempts.py` | 0 · Success: no issues found in 3 source files |

上述最终命令没有跳过项。定向 PostgreSQL 用例观察一轮 A 成功、B 失败，第二个 Worker 复用 A 的原始 ToolExecution、Fact、Evidence、选择记录，只重新创建 B。覆盖工具独立截止、取消、父 lease 超时、旧子直传、Evidence 摘要篡改、Fact 内容篡改、已胜出步骤输入变更、父轮回溯和单步骤预算。旧 002 行的增量升级保留原件。正式模型提供方、生产数据库及 GPU 为 **RUNTIME_NOT_RUN**。

## 原生 diff review P2 修复

父子 fence 的最后一次 PostgreSQL 读同时检查子步骤 deadline。子步骤仍在期限内、工具完成时间未超过数据库观测时间时，才确认 `on_time`。截止后可提交归一化 `timed_out` 以保留失败审计和重试链。该路径保留明确观测到的 `late`，其他到达时间保留为 `unresolved`。迟到成功继续拒绝。公开契约和迁移均无需调整。

| 实际命令 | 退出码和结果 |
|---|---|
| `.venv/bin/python -m pytest -q tests/unit/test_wp04_tool_step_attempts_postgres.py tests/unit/test_wp04_gateway_nested.py tests/contract/test_wp04_nested_dispatch_journal.py` | 0 · 23 passed in 49.07s。其中 PostgreSQL 用例确认超时轮次可读回，同一子轮次的回包时间标记按原证据处理。路由用例验证 `TIMED_OUT` 来源仍符合独立模型调用证据门槛。 |
| `PYTHONPATH=. .venv/bin/python planning/implementation_evidence/WP-04/independent/probe_tool.py physical-timeout` | 0 · 隔离 CPU 和 PostgreSQL 运行记录 `step-a, step-b, step-b`，旧 B 为 `timed_out`，N06 终结且 coverage 为 complete。 |
| `PYTHONPATH=. .venv/bin/python planning/implementation_evidence/WP-04/independent/probe_nested.py` | 0 · 隔离 PostgreSQL 和 loopback HTTP 各观测一次 primary 及 fallback POST。嵌套 HTTP 超时保留为触发原因，外层 Tool 的源子轮次为 `failed`，后续运行完成终结。 |
| `.venv/bin/python -m pytest -q tests/unit/test_wp04_tool_step_attempts_postgres.py -k cross_round` | 0 · 1 passed, 6 deselected in 5.73s。仅复测后补的同轮迟到拒绝和按时成功断言。 |
| `.venv/bin/ruff format src/jimu_runtime/tools/step_attempts.py tests/unit/test_wp04_tool_step_attempts_postgres.py && .venv/bin/ruff check src/jimu_runtime/tools/step_attempts.py tests/unit/test_wp04_tool_step_attempts_postgres.py && .venv/bin/mypy src/jimu_runtime/tools/step_attempts.py` | 0 · 格式化、Ruff 检查、mypy 通过。 |
