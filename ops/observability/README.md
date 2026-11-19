# WP-07 观测

`PostgresObservability` 使用已接入的 PostgreSQL AsyncEngine，从 workflow、dispatch journal 及 Tool Plan 步骤读取受控投影。API 调用方先验证 actor、action 和 `IdempotencyScope(project_id, client_id)`，再传入核验过的 scope。数据库查询始终绑定两个范围键。数据库凭证应交给受信服务，角色仅授予这些表的读取权限，按部署的租户隔离边界管理该角色。查询使用只读、可重复读事务，应用读取预算 5 秒，SQL 上限 4 秒，每个读取器最多 4 个并行查询。

| 方法 | 返回 | 分页 |
|---|---|---|
| `events(scope, execution_id, cursor, limit)` | Task 和 Execution 时间、事件、NodeRun、Attempt 号及 trace/span 关联 | 用 `next_cursor` 原样翻页。首次读取固定事件 `through`，后到事件留给下一次读取。`execution` 是当前读取时的状态摘要，`execution_state_through` 是该摘要自身的事件高水位。 |
| `calls(scope, execution_id, attempt_id, cursor, limit)` | 实际 dispatch、Attempt 及 parent tool lineage。每条物理 dispatch 的操作节点、实际节点、常规与晚到回执分别保留，包括实际 producer ID、边界和观测时点 | 按 `(recorded_at, dispatch_id)` keyset 翻页，可指定 Attempt。原样传回 `next_cursor`。 |
| `tool_steps(scope, execution_id, cursor, limit)` | Tool Plan 父子 Attempt、时间及实际 ToolExecution，区分业务 outcome 和 Attempt 状态 | 用 `next_cursor` 继续。 |
| `metrics(scope)` | Prometheus 文本 | 无动态 ID label。指标结果受固定枚举、固定 bucket 和 64 KiB 限制。 |

事件的 `dispatch_id` 仅在晚到事件能沿已存的响应引用、归档 ObservationBatch 或实际 producer 到达同一 Attempt 的 journal 时填充。调用和工具游标携带来源 revision，绑定 scope、Execution 及可选 Attempt。单页使用一致快照，续页检测到同范围的派发、回执或步骤完成记录变化时，抛出 `ObservabilityCursorStale`。对外 API 可将其映射为 HTTP 409。调用方重新读取首屏。游标用于当前数据库中的一次遍历。

每页最多 100 条，响应上限 256 KiB。scope 外和不存在的 Execution 均返回 `None`。对外应用应将其映射到同一不可见状态。查询与输出均只选取相应字段，Prompt、图像、凭证、HTTP 请求 ID、错误描述、供应商输出及证据位置不会进入投影。错误文本保持固定。

Task 等待时间从受理到首次 Execution 开始，并以同一 Execution 的终止时间计算端到端耗时。节点与模型、工具 duration 仅使用有完整时间戳的实测记录。一个实际 dispatch 有多份回执时，每个 dispatch 的时长采用一对完整且有序的时间戳。输入和输出 Token 各自选择一次实际报告值。每项测量优先使用满足该条件的常规回执。晚到、未知边界及缺少合格 producer 的记录仍可独立核查。指标自保留记录重算，进程重启后可恢复，数据库保留和恢复操作可以改变保留总量。派发许可记录只表示已准许发送，物理通信边界以回执为准。分布导出为 `jimu_retained_*_seconds_{bucket,count,sum}` 快照 gauge，仪表盘直接从当前保留数据的固定桶估计 p95。token 有独立样本计数，缺失时没有 token 值。GPU、评审质量和通用媒体验证错误未接入独立采集源，仪表只展示真实 journal 中记录的响应证据错误。

## 部署配置

`compose.yml` 依赖已验收的 Runtime CPU 镜像，镜像应包含 WP-07 API 使用的 ASGI server。宿主提供 Runtime、Prometheus 和 Grafana 镜像的固定摘要、获准 PostgreSQL 网络、scope 和三个私有凭证文件。Grafana 在独立、关闭 IP masquerade 的 bridge 上仅发布回环端口。Prometheus 和 scrape 服务留在内部监控网络。Grafana 同时通过监控网络访问 provisioned datasource。该文件由主部署在获准环境组装。健康检查和权限行为如下。

- `GET /livez` 可匿名调用，返回固定文本。
- `GET /readyz` 需同 scope 凭证，报告数据库读取和所需 schema，使用业务查询的零行投影验证列和读取权限。主服务可传入异步 `worker_claim_probe`，独立验证 Worker 实际取件能力。探针超时或不可用时标为 `unavailable`，未配置探针时显示 `unknown`。
- `GET /metrics` 需同 scope 凭证。单个 scrape 实例绑定一个 scope，跨 scope 查询见授权 API。
- 私有部署使用凭证文件。Compose 无意引入新的数据库写角色或迁移。若使用代理接入，入口需保留授权，跨网络传输由宿主的受信通道承载。

Prometheus 与 Grafana 使用以下官方配置格式。[Prometheus scrape 配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#http_config) [Grafana 文件 provision](https://grafana.com/docs/grafana/latest/administration/provisioning/) [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)

## 接口与 Schema

调用方从 `jimu_runtime.observability` 导入 `PostgresObservability` 和 `ObservabilityCursorStale`。分页模型在 `jimu_runtime.observability.contracts`。公开投影的 Pydantic Schema 见 [schema.json](schema.json)，`x-exports` 列出各根模型对应的 JSON Pointer。使用锁定依赖重导出。

```bash
python -m jimu_runtime.observability.schema > ops/observability/schema.json
```
