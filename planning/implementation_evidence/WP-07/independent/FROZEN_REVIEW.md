# WP-07 独立封存验收

| 封存项 | 实测值 |
|---|---|
| 产品清单 SHA-256 | `2a013b38fd05125a54b1b8126932a75f2b20849d8d148efbb0b2b3793ce53d68` |
| 产品 ZIP SHA-256 | `a458149665893542f29b71e48e9d4d4fbf6c9025f231a86c557a4bc59c14d1c9` |
| 产品文件 | 1018 个，包含 `src`、`tests`、`scripts`、`schemas/generated`、`examples`、`ops`、`pyproject.toml` 和 `uv.lock` |
| 独立探针 | 监控 `f992fc907a7edf311540a9350232ed5c0306dfec4c9cfde95c34bfabc9db4700`，组合恢复 `115f456fce57182264c7ff4db50923e7ca67bbb9b5511cebf7447935146219aa` |
| 本地环境 | CPython 3.12.3、uv 0.9.15、pytest 8.4.2、Ruff 0.14.14、mypy 1.18.2、Docker Engine 29.9.3、Compose 5.7.0、隔离 PostgreSQL 16.14 |

从外部固定的清单校验 ZIP 及 1018 个成员，再独立解包。已复核可信 WP-01B 历史 Schema fixture 的 SEAL、原件和临时副本。冻结包与预检候选的唯一内容差异为 `src/jimu_runtime/api/boundary.py` 的 Ruff 格式修正。证据见 [完整验收日志目录](frozen_run_gdkMGNme) 和 [运行组独立日志](frozen_run_Y3ovKjUF)。

| 验收门 | 当前结果与证据 |
|---|---|
| 静态验证 | `PASS`。Ruff lint 退出 0，497 文件格式通过，strict mypy 核查 230 个源文件。生成检查、两次干净重导出、冻结 Schema 树及 Observability Schema 字节对照均退出 0。已验收 WP-06 的 347 份公共 Schema 和索引无增删及字节变化。见 [Schema 核对](frozen_run_gdkMGNme/schema-347.log)。 |
| 全量 pytest | `TEST_PASSED`。`run_acceptance_v2.sh --pytest` 采用标准 `umask 022`，以同封存重新解包并运行全部 3988 项。本轮独立 JUnit 记录 3988 通过、0 失败、0 错误、0 跳过，pytest 退出 0，耗时 1735.00 秒。WP-07 29 项、场景 24 项全部通过，真实 Compose 主用例耗时 95.051 秒。见 [最终测试日志](frozen_run_N2X0sklL/pytest.log)、[JUnit](frozen_run_N2X0sklL/pytest.xml) 和 [测试收集](formal_collection.log)。 |
| 回环监控 | `RUNTIME_VERIFIED`。正式包构建离线镜像执行。Grafana 宿主随机回环 health 200，匿名 metrics/readiness 401。Prometheus 目标 up，仅授权范围 Task 1 条，数据库有 2 条。Token 数据缺失时无对应系列。Grafana 执行 13 个 provisioned dashboard 表达式，首面板为 1。探针确认测试容器、卷与网络完成清理。见 [监控日志](frozen_run_Y3ovKjUF/monitoring.log)。 |
| 组合恢复 | `RUNTIME_VERIFIED`。安装 wheel 的独立进程实际执行 PostgreSQL dump/restore，封包包含 30 个文件。覆盖错误 alias、错误 hash、缺少关联完成记录、hold 持有内容缺失四种拒绝，以及 6 个范围拒绝。旧 Release、原图、Decision、评分和 hold 可重新读取，已处置内容仍保持 expired。未确认的物理派发在恢复后取消并完成对账，额外 HTTP、CPU 与 dispatch 次数均为 0。见 [原始探针记录](frozen_run_Y3ovKjUF/combined-recovery.log)。 |
| 运行组证据完整性 | `PASS`。进程退出 0，前后 ZIP 及解包树逐文件校验通过，可信历史 fixture 和探针输入复核通过，运行组输出摘要逐文件验证通过。见 [退出记录](frozen_run_Y3ovKjUF/run-status.txt) 和 [输出摘要](frozen_run_Y3ovKjUF/run_outputs.sha256)。 |

## 运行器修正与证据复核

首次 `--all` 的 JUnit 记录 3987 通过、1 失败、0 错误、0 跳过，1771.33 秒。运行器将 `umask 077` 传给 pytest，导致历史目录权限负例创建的 `0755` 目录实际变成 `0700`，产品正确接纳该私有目录。固定封存下对照复跑，077 退出 1，022 退出 0。新版运行器仅调整 pytest 子进程的 umask，产品文件摘要保持一致。全量结果采用校正后的完整 JUnit。见 [首次 JUnit](frozen_run_gdkMGNme/pytest.xml) 和 [权限对照](pytest_umask_reproduction.log)。

同一产品封存的静态组、独立 `--runtime` 及校正 `--pytest` 构成本地技术验收，结论为 `PASS`。校正测试退出 0，校正组 19 项输出逐文件复核通过。最终 JUnit SHA-256 为 `ce20189e77152a1a9cc3a4234d229fee551a8033360a237ffea8431568241d91`，校正组输出清单 SHA-256 为 `bebfb3ce235f290b419a6721d6e9768fce5bdaafebde3b0b40db7012acccd327`，运行组输出清单 SHA-256 为 `a6c4ad5820f2a71c319ac6e00c6dde129155178ed1647ec677f2d5d052b35d3e`。见 [测试输出摘要](frozen_run_N2X0sklL/run_outputs.sha256) 和 [运行输出摘要](frozen_run_Y3ovKjUF/run_outputs.sha256)。

真实目标主机、真实身份配置、真实模型和生产恢复为 `RUNTIME_NOT_RUN`。工作包接受由主控结合评审决定。
