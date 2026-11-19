# W4.2 HTTP 适配器验收证据

验收结论 **PASS**。隔离回环 HTTP 为 `RUNTIME_VERIFIED`，真实 Provider、远程 TLS 联调、真实模型质量及 GPU 为 `RUNTIME_NOT_RUN`。范围由主控在 WP-03 正式验收后放行。基线是在 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 上保存的 735 文件冻结快照，清单 SHA-256 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`。本包本地工作树有未发布的实施增量。[WP-03 冻结依据](../WP-03/FINAL_ACCEPTANCE_REPORT.md)

## 代码和接口

- `gateway/http.py` 从可信宿主匹配固定 endpoint、部署、协议、模型 ID 和 descriptor pin。HTTPS 校验证书，非回环连接需显式启用。明文仅允许显式启用的字面回环地址。每次派发最多尝试一个 POST。环境代理、重定向、HTTP 重试和内容解压均禁用。
- `gateway/http_wire.py` 准备确切请求字节，复核静态输入与输出 SchemaRef、当前调用派生 Schema 的 SHA-256 及请求正文。介质入口接收上游已验证的原始 JPEG 或 PNG 字节，再核对摘要、类型标记和长度。供应方 JSON 拒绝重复键、非有限数、无效 Unicode、过深嵌套，以及不相符的响应协议和结果模型。N04 的逐项业务 Fact 校验仍由调用方执行。
- 请求和响应按宿主与 Binding 的较小限制处理。总时限采用单调时钟，包含等待本地并发槽和 IO。未完整读取的响应只记录已观察到的状态和字节，完整读取才产生摘要。429、408、5xx 等按稳定代码映射，`Retry-After` 接受秒数或 GMT 时间并截断到宿主上限。取消传播至审计层，套接字和并发槽可回收。
- 凭据通过宿主引用解析，只出现在外发认证头。HTTP 返回安全摘要与筛选后的实际身份元数据，业务适配器可读取通过 Schema 校验的内容。经宿主在 endpoint descriptor 中显式允许时，完整、受限并通过验证的无凭据回显应答可附带精确原始正文供后续受限 Evidence sink 消费，默认按策略省略原件。截断、超限、凭据回显和其他未验证正文携带固定省略原因。回归验证原始 JSON 外层摘要与校验后内层摘要不同。日志、结果表示及普通 PostgreSQL 诊断禁止原始正文、认证头与错误正文。请求独立的 trace hook 在冻结版 HTTPcore 格式化 DEBUG 日志前清除敏感终结事件的数据，已验证响应头、JSON 转义及传输异常回显。
- 变更包含上述两个模块、四个 HTTP fixture/测试文件、依赖边界校验、`pyproject.toml` 及 `uv.lock`。公共 Domain Schema、API wire 和数据库迁移在 W4.2 无变更。回退可还原本批源码、测试和锁定依赖增量，无需回滚历史记录。

## 依赖与运行环境

CPython 3.12.3，Linux 6.8.0-149-generic x86_64，uv 0.9.15。直接依赖为 `httpx>=0.28.1,<0.29`。官方 PyPI 锁解析为 HTTPX 0.28.1、httpcore 1.0.9、h11 0.16.0、AnyIO 4.16.0、certifi 2026.10.23、IDNA 3.20。httpcore 1.0.9 对 h11 的最低要求为 0.16。逐条比对 WP-03 `product.zip` 中的原件，28 个已锁定的依赖条目全部相同，仅项目条目增加 HTTPX 边并新增 6 个包。原锁 SHA-256 `ad02707953db83eec12468dd88617ab4799299e9851c3449e13e04a195129577`，新锁 SHA-256 `c4201d23bac4804662c8a05db697a9bc5c233983c455b7d9af5b61599fb4d5c8`。[HTTPX 发行](https://pypi.org/project/httpx/0.28.1/) · [HTTPcore 发行](https://pypi.org/project/httpcore/1.0.9/) · [h11 发行](https://pypi.org/project/h11/0.16.0/) · [AnyIO 发行](https://pypi.org/project/anyio/4.16.0/) · [certifi 发行](https://pypi.org/project/certifi/2026.10.23/) · [IDNA 发行](https://pypi.org/project/idna/3.20/)

Pydantic 2.12.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。测试仅连接套件在 127.0.0.1 启动并登记的临时监听端口，未使用实际 Provider 凭据。实际验证包含 remote-primary 合成部署、本地兼容协议、媒体序列化、N04 路由到 HTTP 再到 Invocation、无长度、截断、慢分块、超限、429、取消回收、身份不匹配及凭据回显。

## 执行记录

首次离线解析尚缺官方索引上的 HTTPX 文件，退出 1。完成官方锁解析和同步后，离线锁检查通过，并在新建的独立环境完成离线重建。最终测试复跑于 2026-11-18 04:23:44 至 04:23:46 UTC。以下命令均退出 0。

```sh
uv lock
uv lock --check
uv lock --offline --check
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv sync --locked
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv sync --locked --offline
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv run --locked --no-sync pytest -q tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py tests/contract/test_dependency_boundaries.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync pytest -q tests/contract/test_wp03_postgres_migrations.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync ruff check src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py tests/fixtures/wp04/http_support.py tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py tests/contract/test_dependency_boundaries.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync ruff format --check src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py tests/fixtures/wp04/http_support.py tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py tests/contract/test_dependency_boundaries.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync mypy --cache-dir=/tmp/jimu-wp04-http-mypy src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py
```

`TEST_PASSED`。独立离线重建安装 33 项，HTTP 与边界测试 60 项全部通过且无跳过。普通 `pytest` 入口的 PostgreSQL 迁移合同回归另有 2 项通过。Ruff 检查及格式门禁通过，严格 mypy 检查两个源码文件通过。首次迭代的 canonical helper 路径及派生 Schema 注册键已在当前运行前修正。

W4.5 将本适配器接入独立审计与 Worker 栅栏验证。真实服务的可用版本、受支持的 Chat Completions 字段、证书环境与模型部署需要单独实测。

## 原生 diff review 整改

下表记录原生 review 后的本批修订。W4.2 代码与定向回归已交主控冻结。WP-04 整包冻结和最终裁决由主控记录。[原生 review](../../implementation_evidence/WP-04/primary/native_http_review.md)

| Review 项 | 整改与回归 |
|---|---|
| P1 派生 Schema 回包验证 | `bundle/schema_validation.py` 复用仓库已锁定的 `Draft202012Validator`，注册时编译受限离线子集，限制深度、引用展开、Schema 与实例组合工作量。根 `$defs` 内的无环引用可用，外部引用、嵌套 `$id`、动态引用、正则及未审核操作拒绝注册。通用回包同时满足宿主 Pydantic 合同和精确 Schema 才可出具校验摘要。已回归基础模型接受但派生枚举拒绝的完整回包。N04 显式题槽模式绑定选定 Question refs 和派生摘要，验证闭合外信封，再逐槽验证。内部记录缺失或无效槽位，保留有效兄弟项供 WP-05 生成 Facts。未知槽位拒绝整个信封。 |
| P1 Provider 标准元数据 | 保留严格闭合模型，增加有界的 OpenAI token details 和 vLLM `stop_reason` 等已声明元数据，保持单条非流式应答。分别用代表性回环响应验证接受，并拒绝未知、错误类型及超界计数。[OpenAI usage 定义](https://github.com/openai/openai-python/blob/main/src/openai/types/completion_usage.py) · [vLLM 协议](https://docs.vllm.ai/en/v0.15.0/api/vllm/entrypoints/openai/chat_completion/protocol/) |
| P2 模型身份缺失 | 缺失或 null 的 `model` 仅在固定路由持有独立授权版本/所有权证明时通过；观测模型字段保持缺失。覆盖有证据及无证据的两种 wire 形式。 |
| P2 数值无损 | Schema 注册与请求复核拒绝经过浮点表示会改变数值的字面量。响应的 Schema 比较保留十进制精度，并用保留原值的数值下界回归。已验证 `1e-400` 阈值注册拒绝、近邻响应值无法因舍入越过约束。 |
| P2 CPU 校验事件循环阻塞 | Worker bridge 优先使用有界异步准备。请求 JSON/图像复核和响应解析、Schema 验证走受限线程，校验线程结束前一直占用额度；取消或超时后其结果不能接触派发回执。回归分别阻塞请求和响应校验，验证额度归还、后续恢复及完整回包超时时仍保留安全摘要。HTTP 在实际写 socket 前执行可信 bridge 的授权回调，拒绝时无 POST。 |
| P2 TLS 重复初始化 | 每个 adapter 初始化时创建验证用 TLS context，后续传输复用该 context，并保留单次 POST 和关闭连接的行为。回归验证同一 context、证书校验及主机名校验配置。 |
| P2 GMT Retry-After | 解析 HTTP-date 三种日期形态，包括按 GMT 解释无显式时区的 asctime-date。回归未来日期的上限裁剪及非 HTTP-date 时区或语法拒绝。[RFC 9110 HTTP 日期](https://www.rfc-editor.org/rfc/rfc9110.html#section-5.6.7) |

复测使用前述 CPython、uv 及锁定环境，无新增依赖。HTTP 及依赖边界最终复跑于 2026-11-18 04:49:56–04:50:00 UTC，`TEST_PASSED`，86 项通过且无跳过。隔离 PostgreSQL/Worker Gateway 合同集成 5 项通过。Ruff lint/format、严格 mypy 和离线 lock 检查退出码均为 0。曾在并行开发期间遇到共享 Tool registry 新类型未落盘，随后在接口可用时完整集成复测通过。

```sh
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv run --locked --no-sync pytest -q tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py tests/contract/test_dependency_boundaries.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv run --locked --no-sync pytest -xq tests/contract/test_wp04_worker_gateway.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync ruff check src/jimu_runtime/bundle/schema_validation.py src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py tests/fixtures/wp04/http_support.py tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/schema_validation.py src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py tests/fixtures/wp04/http_support.py tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-venv uv run --locked --no-sync mypy --cache-dir=/tmp/jimu-wp04-http-mypy src/jimu_runtime/bundle/schema_validation.py src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/http_wire.py
uv lock --offline --check
```

真实 Provider、远程 TLS 实际握手、GPU 和模型质量仍为 `RUNTIME_NOT_RUN`。

## 取消回执补充回归

主控放行 HTTP 与 Worker bridge 联合补齐取消边界。HTTP 在取消时把只含安全观察的 `TransportCancelled` 附在原始 `CancelledError`，由 bridge 记录派发结果并继续传播原异常。三种回环用例分别验证发送闸门取消零 POST、已进入物理传输且无应答证据、已接收响应头并保留可见 request ID。已发送的两种用例验证连接关闭，未完整接收时无完整摘要、原始正文或可发布解析内容。调用方的 Python 3.12 `asyncio.timeout` 仍转换为 `TimeoutError`。

`TEST_PASSED`。04:53:45–04:53:49 UTC 在同一离线锁定环境重跑 HTTP 与依赖边界，90 项通过且无跳过。Ruff lint/format 与严格 mypy 通过。

```sh
UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv run --locked --no-sync pytest -q tests/unit/test_wp04_http_wire.py tests/negative/test_wp04_http_negative.py tests/contract/test_wp04_http_contract.py tests/contract/test_dependency_boundaries.py
/tmp/jimu-wp04-http-venv/bin/ruff check src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/invocation.py tests/negative/test_wp04_http_negative.py
/tmp/jimu-wp04-http-venv/bin/ruff format --check src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/invocation.py tests/negative/test_wp04_http_negative.py
/tmp/jimu-wp04-http-venv/bin/mypy --cache-dir=/tmp/jimu-wp04-http-mypy src/jimu_runtime/gateway/http.py src/jimu_runtime/gateway/invocation.py
```

接入复跑 `UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp04-http-offline-rebuild-venv uv run --locked --no-sync pytest -xq tests/contract/test_wp04_worker_gateway.py` 退出码 0，9 项通过，无跳过。隔离 PostgreSQL 中发送意图落盘后取消、HTTP 发送闸门取消均留下恰好一个 `not_dispatched/cancelled` 回执，节点发送计数为零，回环端点未收到请求。已有真实回环迟到用例保留 `acknowledged` 记录并验证取消后无权威发布。
