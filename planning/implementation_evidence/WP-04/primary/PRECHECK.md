# 主控 WP-04 集成预检

这些命令在开发中的工作树实际执行。独立冻结验收使用另一份清单。

| 原始 JUnit | 执行退出码 | 用例 / 失败 / 错误 / 跳过 |
|---|---:|---|
| [in_progress_routing_resources](in_progress_routing_resources.xml) | 2 | 1 / 0 / 1 / 0 |
| [routing_resources_02](routing_resources_02.xml) | 0 | 34 / 0 / 0 / 0 |
| [migration_boundary_preflight](migration_boundary_preflight.xml) | 2 | 2 / 0 / 2 / 0 |
| [migration_boundary_02](migration_boundary_02.xml) | 0 | 38 / 0 / 0 / 0 |
| [upgrade_from_wp03_01](upgrade_from_wp03_01.xml) | 0 | 1 / 0 / 0 / 0 |
| [adapter_preflight_03](adapter_preflight_03.xml) | 0 | 115 / 0 / 0 / 0 |
| [persistence_tools_04](persistence_tools_04.xml) | 2 | 1 / 0 / 1 / 0 |
| [persistence_tools_05](persistence_tools_05.xml) | 1 | 22 / 6 / 0 / 0 |
| [persistence_tools_06](persistence_tools_06.xml) | 0 | 29 / 0 / 0 / 0 |

## 实际命令

```bash
uv run --locked --no-sync pytest -q tests/unit/test_wp04_resource_inventory.py tests/unit/test_wp04_gateway_routing.py --junitxml=planning/implementation_evidence/WP-04/primary/in_progress_routing_resources.xml
uv run --locked --no-sync pytest -q tests/unit/test_wp04_resource_inventory.py tests/unit/test_wp04_gateway_routing.py --junitxml=planning/implementation_evidence/WP-04/primary/routing_resources_02.xml
uv run --locked --no-sync pytest -q tests/contract/test_dependency_boundaries.py tests/unit/test_b_dependency_graph.py tests/contract/test_wp03_postgres_migrations.py tests/negative/test_wp03_postgres_migration_rejections.py --junitxml=planning/implementation_evidence/WP-04/primary/migration_boundary_preflight.xml
uv run --locked --no-sync python -m pytest -q tests/contract/test_dependency_boundaries.py tests/unit/test_b_dependency_graph.py tests/contract/test_wp03_postgres_migrations.py tests/negative/test_wp03_postgres_migration_rejections.py --junitxml=planning/implementation_evidence/WP-04/primary/migration_boundary_02.xml
uv run --locked --no-sync pytest -q tests/contract/test_wp04_workflow_upgrade.py --junitxml=planning/implementation_evidence/WP-04/primary/upgrade_from_wp03_01.xml
uv run --locked --no-sync pytest -q tests/unit/test_wp04_gateway_invocation.py tests/unit/test_wp04_gateway_routing.py tests/contract/test_wp04_http_contract.py tests/negative/test_wp04_http_negative.py tests/unit/test_wp04_http_wire.py tests/contract/test_wp04_worker_lifecycle.py tests/unit/test_wp04_resource_inventory.py --junitxml=planning/implementation_evidence/WP-04/primary/adapter_preflight_03.xml
uv run --locked --no-sync pytest -q tests/unit/test_wp03_worker_postgres.py tests/contract/test_wp03_repository_roundtrip.py tests/negative/test_wp03_repository_rejections.py tests/contract/test_wp04_dispatch_journal.py tests/contract/test_wp04_tool_executor.py tests/contract/test_wp04_workflow_upgrade.py --junitxml=planning/implementation_evidence/WP-04/primary/persistence_tools_04.xml
uv run --locked --no-sync pytest -q tests/unit/test_wp03_worker_postgres.py tests/contract/test_wp03_repository_roundtrip.py tests/negative/test_wp03_repository_rejections.py tests/contract/test_wp04_dispatch_journal.py tests/contract/test_wp04_tool_executor.py tests/contract/test_wp04_workflow_upgrade.py --junitxml=planning/implementation_evidence/WP-04/primary/persistence_tools_05.xml
uv run --locked --no-sync pytest -q tests/unit/test_wp03_worker_postgres.py tests/contract/test_wp03_repository_roundtrip.py tests/negative/test_wp03_repository_rejections.py tests/contract/test_wp04_dispatch_journal.py tests/contract/test_wp04_tool_executor.py tests/contract/test_wp04_workflow_upgrade.py --junitxml=planning/implementation_evidence/WP-04/primary/persistence_tools_06.xml
```

## 集成中的首次全量运行

实际运行 `uv run --locked --no-sync python -m pytest -q -ra --junitxml=planning/implementation_evidence/WP-04/primary/integration_full_preflight.xml`，退出码 1，3514 passed，6 failed，耗时 533.65 秒。运行时其他写入者仍在更新产品。三项 journal 故障的共同症状为旧连接代码缺少新 relation 的 operation_node_run_id，一项独立子进程读取到不同版本的容量清单，一项 Schema 工具读前后遇到其他进程写入的 __pycache__。另有一项跨轮工具引用见证不匹配。集成中的日志与 JUnit 原样保留。正式签收要求冻结同一版本，独立重建环境并完整复测。

同一工作树直接复跑 6 个失败 ID 为 3 passed、3 failed。随后从实际产品文件复制出单独的 779 文件临时快照，路径与各文件 SHA-256 组合指纹为 15cd9ad9b8fba56fa61ec3c3e360eedbeb767b1e7ff78c3fa7e5600d05810a87。全新离线锁定环境 CPython 3.12.12，在该快照、不写 .pyc 的条件下复跑原 6 项，全部通过（32.15 秒，退出码 0）。这项结果只覆盖 6 个原先失败的用例。正式冻结后的整套回归仍要独立执行。原始证据见 [隔离复测日志](isolated_failed_recheck.txt) 和 [JUnit](isolated_failed_recheck.xml)。

迁移 006 纳入后，执行 `PYTHONDONTWRITEBYTECODE=1 uv run --locked --offline --no-sync python -m pytest -q tests/contract/test_wp04_workflow_upgrade.py tests/contract/test_wp03_postgres_migrations.py`，3 passed（4.31 秒，退出码 0）。Schema 导出检查通过，347 份 Domain Schema 与 index 和 WP-03 固定清单字节匹配。集成时全量 strict mypy 对 148 个源文件通过，Ruff check 通过。正式冻结后以实测文件数重新报告。

隔离完整回归已运行完成。784 文件清单 SHA-256 为 `e035ee03e51c14066622e948bbeaafeaf4281f34e1463da5534e133832346146`，包括产品、测试、Python 版本标识以及 Schema 历史比较所需的 WP-01B 清单。这个独立副本通过 uv 离线锁定环境（CPython 3.12.3）执行完整 pytest。JUnit 记录 **3590 passed，0 failed，0 errors，0 skipped**，pytest 耗时 608.01 秒。见[源清单](isolated_full_manifest.json)、[实际命令与日志](isolated_full_preflight.txt)和[JUnit](isolated_full_preflight.xml)。此后修改的审查修复单独验证，并须进入最终冻结回归。

### WP-03 实物生成与 WP-04 历史兼容

主控核对并解包封存清单 b8e95556aa80… 的 735 个原件。独立子进程导入封存的 WP-03 compiler、admission 和 ReleaseManager，用含共享必填字段的历史 Tool Schema 生成、签名、准入及激活 release-1。当前代码在另一进程重开原账本和固定 Bundle，摘要为 9f3c1414bf2e696b8fb9cbf2237d706c1c04c0546cbd756a1dc7e9d2425563e7。实际 CPU 工具成功回传两个已知 Facts。另一次回传在旧 Schema 中无法表达局部成功，记录 tool_partial_payload_unrepresentable 和原始 412 字节响应摘要，未产生权威 Fact。使用合成数据、本地账本和当前 Python 依赖，验证范围是旧产品与新产品之间的读取兼容性。

命令 `PYTHONPATH=.:src uv run --locked --offline --no-sync python planning/implementation_evidence/WP-04/primary/historical_tool_probe.py`，退出码 0。见[可复现探针](historical_tool_probe.py)和[两进程输出](historical_tool_probe.log)。
