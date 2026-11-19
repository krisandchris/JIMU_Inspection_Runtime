# WP-07 Observability 原生审查处置

| 原生审查项 | 实现和验证证据 |
|---|---|
| 迟到事件的调用键 | 晚到事件核对同一数据库 late_responses 的精确响应引用，再从已归档 ObservationBatch 解析 ModelInvocation，或采用直接 producer 引用。只连接同 scope、Execution、NodeRun、Attempt 且有实际 producer 的 journal。真实取消迟到流程中增加同时间、同 Attempt 的未获发送许可 sibling，核对事件仍只指向实际调用。 |
| 调用操作节点 | CallSpan 分开保留 operation_node_run_id、node_run_id 和父步骤 lineage。真实嵌套 CPU Tool 路径核对两种节点 ID。 |
| 摘要与事件高水位 | EventPage 带 through 和 execution_state_through。测试在较早事件高水位读取当前摘要时，两个高水位可区分。 |
| 调用和工具步骤跨页 | 游标带来源 revision。真实 PG 回执和 ToolStep completion 写入前捕获游标，写后续页拒绝并要求重启遍历。Append-only 数据源的 scope 和过滤条件参与 revision。每页使用可重复读事务。 |
| Token 和时长 | 按 Execution、dispatch 及每项测量选择实际报告值。实际 PostgreSQL 跑同一生产 SQL CTE，覆盖输入 0、输出仅见于晚到回执、起止时间缺失、重复 dispatch ID 在不同 Execution 独立计量。 |
| 保留数据指标类型 | 分布导出 jimu_retained_*_seconds_{bucket,count,sum} snapshot gauge。仪表盘直接查询分布，保留清理后的数值回退具有 gauge 语义。 |
| 就绪检查 | 真实业务 SELECT 的零行投影验证依赖列。隔离库撤销业务列 SELECT、仅保留 owner 列权限时，数据库可连通、schema 不可用，readiness 和指标读取行为一致。 |
| 稳定输出排序 | 同族按标签固定顺序，分布桶维持数值顺序。逆序行的单测逐字节比较。 |
| Bearer | 大小写无关的方案及 header 名，token 原字节恒定时间比较。双 header、错 token 和尾部多余字符仍拒绝。 |
| token 无值断言 | 单测对实际注册名 jimu_retained_reported_tokens 断言，并区分合法报告 0 与缺失。 |

公共投影 Schema 按固定 Pydantic 版本导出到 ops/observability/schema.json。游标的接口变更限于尚未发布的 Observability API。既有 Domain Schema、DB 迁移和 Python 锁文件无改动。Prometheus 和 Grafana 的实际容器启动仍待批准镜像和组合验证。

## 验证

在隔离副本、锁定环境运行。Python 3.12.3、pytest 8.4.2、Pydantic 2.12.5、SQLAlchemy 2.0.52、psycopg 3.3.5。隔离 Docker PostgreSQL 16 使用现有测试 fixture 的固定摘要 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。

```bash
export PYTHONPATH=src
JIMU_WP07_QA=/tmp/jimu-wp05-locked-primary-yhUGyEbS/venv/bin
"$JIMU_WP07_QA"/ruff check src/jimu_runtime/observability tests/unit/test_wp07_observability* tests/negative/test_wp07_observability* tests/contract/test_wp07_observability*
"$JIMU_WP07_QA"/mypy --strict src/jimu_runtime/observability
"$JIMU_WP07_QA"/python -m jimu_runtime.observability.schema | cmp - ops/observability/schema.json
"$JIMU_WP07_QA"/python -m pytest -xq --tb=short tests/unit/test_wp07_observability* tests/negative/test_wp07_observability* tests/contract/test_wp07_observability* --junitxml=/tmp/jimu_wp07_observability_after_review.xml
```

以上退出码均 0。pytest `16 passed in 52.68s`。Ruff 0.14.14 全部通过，mypy 1.18.2 对 7 个源文件通过。Compose `config --quiet`、仪表盘 JSON 解析、19 文件 SHA-256 自核验通过。两个依赖边界检查仅在临时规则副本中启用已批准的 SQLAlchemy 入口后通过，由主控合并对应规则。完整 PostgreSQL 测试日志和 JUnit 见 `/tmp/jimu_wp07_observability_after_review.log`、`/tmp/jimu_wp07_observability_after_review.xml`。复制所需文件清单和摘要在 `/tmp/jimu_wp07_observability_sources.sha256`。
