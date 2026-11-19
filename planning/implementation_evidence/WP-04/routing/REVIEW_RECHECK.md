# W4.1 路由独立复核整改

本次变更覆盖 [路由复核](../primary/native_gateway_routes_review.md) 和主控批准的 N05 子步骤回退关系。改动位于 `gateway/routing.py`、`gateway/invocation.py` 和 `domain/validation/invocations.py`，并补充定向用例。Domain 的持久 Schema 和 ModelInvocation wire 无变化。

| 复核项 | 整改和验收证据 |
|---|---|
| 固定响应契约 | HTTP 只在严格校验已注册的响应契约后设置实际 Schema SHA。派发投影要求该 SHA 和命令的固定响应合同完全相等，证据没有或错版时不发布输出。N04 的可选逐题诊断须在固定有效 Question 范围内。 |
| 凭据和未验证内容 | 传输契约拒绝凭据回显、未验证或截断标记与解析内容并存。派发投影失败时清除解析内容和相应确认信息。HTTP 的凭据过滤和严格解析由回环用例验证。 |
| 物理派发前复核 | 路由带最早已知失效时间，来自当前宿主快照、选定注册、注册撤销、权属和适用评测。越期的物理观察保留边界及摘要，不能产生成功或授权重试输出。实际发送前由工作流桥通过 Transport 的受信回调重核当前路由和派发 fence。 |
| 最新历史 | 路由检查完整历史的相同操作 pins、顺序、唯一 Attempt、触发来源和不可回访路径。普通调用按本 NodeRun 的 Attempt 编号，嵌套调用按实际父 Attempt 编号。工作流桥是持久日志历史和当前链头的权威来源，负责拒绝截断及比最后已证明失败更新的未决派发。 |
| HTTP 状态、边界及字节 | 非 2xx 的回包要求规范化错误。构造请求时验证固定输入字节上限。保留的完整字节必须与观测计数完全相同。确认来自所选服务端的部分响应字节可作为 acknowledged 的依据，截断响应无完整摘要。 |
| N05 工具嵌套回退 | 独立的子 NodeRun 允许通过只读关联见证连接至随后一个父 Attempt 下的同一步骤。比较原始父 N05、两份真实父和子 Attempt、ToolPlan/step/Tool、Profile/Object、绑定、冻结输入和调用合同。路由和 Domain 只对 N05 `vision_auxiliary` 路径接受该见证。见证由工作流桥从持久 StepCompletion 和当时有权的 journal receipt 取得。 |

## 证据

环境与 [W4.1 原报告](REPORT.md) 一致。实际执行命令和输出如下。

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/contract/test_wp04_http_contract.py tests/negative/test_wp04_http_negative.py tests/unit/test_wp04_http_wire.py tests/unit/test_wp04_gateway_{nested,routing,invocation}.py tests/unit/test_c_invocation_relations.py tests/contract/test_c_invocation_schema.py tests/negative/test_c_invocation_invalid.py` | 0 | 217 passed in 4.29s，0 skipped。UTC 2026-11-18 04:44:17 至 04:44:21。包含真实本机 HTTP 回环及纯路由与兼容契约。 |
| `.venv/bin/ruff check src/jimu_runtime/{domain/validation/invocations,gateway/{routing,invocation}}.py tests/fixtures/wp04/routing_support.py tests/unit/test_wp04_gateway_{nested,routing,invocation}.py` | 0 | All checks passed! |
| `.venv/bin/ruff format --check src/jimu_runtime/{domain/validation/invocations,gateway/{routing,invocation}}.py tests/fixtures/wp04/routing_support.py tests/unit/test_wp04_gateway_{nested,routing,invocation}.py` | 0 | 7 files already formatted。 |
| `.venv/bin/mypy src/jimu_runtime/{domain/validation/invocations,gateway/{routing,invocation}}.py` | 0 | Success，3 个源文件。 |

`TEST_PASSED`。跨子 NodeRun 的纯领域关系和审计输出门禁已验证。持久派发、晚到和当前 fence 的完整行为由 W4.5 的 PostgreSQL 验收负责。真实 Provider、业务数据和 GPU 为 `RUNTIME_NOT_RUN`。
