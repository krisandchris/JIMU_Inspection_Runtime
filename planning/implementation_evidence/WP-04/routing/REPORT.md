# W4.1 模型路由与单次派发审计

主控在 WP-03 完成独立验收后放行 W4.1。上游冻结清单 SHA-256 为 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`。本批交付纯路由、Transport 协议和单次派发审计投影，依照 [实施计划](../../../active/WP-04/PLAN.md) 和 [复核意见](../../../active/WP-04/PLAN_REVIEW.md)。

| 文件 | 交付 |
|---|---|
| `src/jimu_runtime/gateway/__init__.py`、`routing.py` | 读取已准入 Execution 的精确 Binding、Provider、Fallback、Resource 和部署 pins。按当前可信注册、撤销、能力和独立评测授权检查远程 primary、本地例外、同部署重试及显式 fallback。 |
| `src/jimu_runtime/gateway/invocation.py` | 一次物理派发的接口和审计投影。保留准确的请求与完整响应摘要、可观测物理边界和错误。按供应方回复或固定部署权属凭证记录真实身份，联合响应 schema 保留原证据引用。 |
| `src/jimu_runtime/domain/validation/invocations.py` | 主控批准的关系校验补丁。已解析执行 pins 可作为版本评测端点见证，兼容原调用方。 |
| `tests/fixtures/wp04/routing_support.py`、`tests/unit/test_wp04_gateway_routing.py`、`tests/unit/test_wp04_gateway_invocation.py` | 固定合成时间、部署、独立评测和传输结果，验证正向、失效及恶意错配形态。 |

## 合同与设计决定

- `RoutePolicy.from_execution` 从已复核的历史配置读取精确版本，可信宿主提供历史权属。派发时 `CurrentRoutingState` 只能验证相同部署版本、模型版本、权属人、端点描述和能力，并核对当前撤销。已固定执行不会自动换用新注册部署。
- `plan_primary`、`plan_retry` 和 `plan_fallback` 由 Worker 分别调用。fallback 使用最近一次原始失败的物理部署和准确 `InvocationError.error_class`，需同一逻辑输入、联合响应合同和有序历史，沿一条已声明、已独立评测的边进入新 Attempt。可逐轮沿多个合法边前进。已到访部署与 primary 回流被拒绝。多条当前合法候选会记录歧义错误。传输层每次调用对应一个物理尝试。
- `project_dispatch` 可表示派发前拒绝、派发已开始但结果不明确、收到供应方确认。实际版本来源为可信物理请求 ID 下的回复字段，或权属证明固定的独立注册凭证。可确认模型 ID 但版本未核实的调用会生成 `UnavailableProviderIdentity` 失败记录。缺少必填实际模型身份或观测身份与执行 pin 冲突时，保留独立派发诊断与已知摘要，业务输出保持不可用。
- 关系校验补丁处理本地例外及 fallback 源和目标允许多个版本的情况。它核验所有已解析部署与配置及已观测身份一致，使用 pins 关联评测，实际版本仍只认可信供应方报告或权属证明。
- Domain Schema、数据库迁移和依赖不变。配置、权属和结果结构只传输必要审计字段。可信宿主解析端点和凭据。记录完整回包摘要需要可信传输确认完整读取，截断时只保留实际观测计数。派发投影不落库 HTTP 响应正文。传输可按经授权的证据保留策略交付经过保护的完整响应字节，省略时支持结构化原因。
- 路由验证在收到已认证的快照和 Attempt 历史后执行。工作流桥负责持续来源、派发前当前验证、预算的持久计算、发送意图与追加落库、租约和权威结果 fence。

## 静态和定向测试

环境为 Linux x86_64，内核 6.8.0-149-generic，glibc 2.39，Python 3.12.3，uv 0.9.15，Pydantic 2.12.5，rfc8785 0.1.4，jsonschema 4.25.1，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。以下均在独立 WP 工作树执行。

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q tests/unit/test_wp04_gateway_routing.py tests/unit/test_wp04_gateway_invocation.py tests/unit/test_c_invocation_relations.py tests/contract/test_c_invocation_schema.py tests/negative/test_c_invocation_invalid.py` | 0 | 134 passed in 2.76s，0 skipped。2026-11-18 04:22:22 至 04:22:25 UTC。 |
| `.venv/bin/ruff check src/jimu_runtime/{domain/validation/invocations,gateway/{__init__,routing,invocation}}.py tests/fixtures/wp04/routing_support.py tests/unit/test_wp04_gateway_{routing,invocation}.py` | 0 | All checks passed! |
| `.venv/bin/ruff format --check src/jimu_runtime/{domain/validation/invocations,gateway/{__init__,routing,invocation}}.py tests/fixtures/wp04/routing_support.py tests/unit/test_wp04_gateway_{routing,invocation}.py` | 0 | 7 files already formatted。 |
| `.venv/bin/mypy src/jimu_runtime/{domain/validation/invocations,gateway/{__init__,routing,invocation}}.py` | 0 | Success，4 个源文件。 |

首次使用直接 `.venv/bin/pytest` 执行新增准入集成用例时，启动方式未把工作树根加入 Python 搜索路径，结果为 28 passed、1 failed，错误为 `ModuleNotFoundError: No module named 'tests'`。使用上述 `.venv/bin/python -m pytest` 命令重跑后，该用例与其余门禁均通过。

`TEST_PASSED`。W4.1 局部工程验收 **PASS**。用例包含实际本地编译、签名、准入及激活合成 Bundle 后读取 Execution pins。传输和实际版本证明均为合成输入。`RUNTIME_NOT_RUN`。真实 Provider、真实业务输入和 GPU 仍待授权及实测。HTTP 实际边界由 W4.2 验证，工作流和 PostgreSQL 审计由 W4.5 验证。若本批回退，可移除隔离的 Gateway/测试文件并撤除 W4.5 接线，保持已持久化审计历史。
