# WP-07 集成树 Observability 验证

- 集成树 JIMU_WP07_integration_cpk000he。修复 tests/contract/test_wp07_observability_postgres.py 以正式 WP05 测试入口运行取消后迟到回执，沿用真实场景及观测断言。
- 两个 ops 文件采纳已验证的 Docker 网络修复。Grafana 的宿主端口只绑定 127.0.0.1，专用 bridge 关闭 IP masquerade，原监控 bridge 保持内部网络。
- 正式可观测性 Schema 由 src/jimu_runtime/observability/contracts.py 中 Pydantic 模型与 src/jimu_runtime/observability/schema.py 生成。PYTHONPATH=src .venv/bin/python -m jimu_runtime.observability.schema | cmp - ops/observability/schema.json 退出 0。生成文件与独立隔离版逐字节一致。摘要见 integrated-obs-hashes.sha256。
- 真实 PostgreSQL 集成回归 16 passed in 62.09s，无 skip。Ruff 和严格 mypy 7 个源文件通过。日志及 JUnit XML 为 integration-observability.log 和 integration-observability.xml。
- 固定 Prometheus v3.13.5、Grafana OSS 13.3.1、PostgreSQL 16.14 摘要见 local-pinned-images.log，两个官方上游 manifest 见 official-registry-manifests.log。集成项目锁定依赖镜像携带 uvicorn 0.52.4，Runtime 临时镜像摘要见 runtime-digest.log。
- 实际 Compose smoke 退出 0。宿主机随机回环 Grafana health HTTP 200，Prometheus target up，仅查询到授权范围 Task 1，数据库共 2 条，Token 无测量系列。Grafana 通过 provisioned datasource 对 dashboard 的 13 个表达式执行成功，首面板取到 Task 1。测试生成的资源及凭证已清理。

完整 pytest 命令为 PYTHONPATH=src .venv/bin/python -m pytest -xq --tb=short tests/unit/test_wp07_observability* tests/negative/test_wp07_observability* tests/contract/test_wp07_observability* --junitxml=/tmp/jimu_wp07_integration_observability.xml。Compose 复现脚本为 run-smoke.sh 与 monitor_smoke.py，需要用集成项目锁导出的 requirements.txt 与正式 src/jimu_runtime 生成 CPU 测试容器。
