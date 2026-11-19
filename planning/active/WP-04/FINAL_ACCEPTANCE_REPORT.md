# WP-04 Model Gateway 与工具执行的本地验收

**本地工程验收 PASS。** 产品基于 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`、WP-03 封存清单 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`。本次封存 784 个产品文件，比 WP-03 新增 49 个、修改 30 个。产品清单 SHA-256 为 `924eb788eab11bed400ec1f3602a9a30d3df7b43cba79e8601901eb7921f7495`，ZIP 为 `929349a4d831ec2c97068f92813686dcaf662489f5b64fbc1d24a07e11aba11f`。[文件与摘要](../../implementation_evidence/WP-04/primary_freeze_924eb788eab1/snapshot.json)

| 验收项 | 实际结果 |
|---|---|
| Code | PASS。Remote Primary 与已评测的显式 fallback、有界 HTTP、隔离工具、资源准入、PostgreSQL 派发与 N05 子 Attempt 均有产品实现。七份原生限定 diff 审查已逐项处置，主控检查实际代码、历史样本及相关回归。原生 Auto-review 没有触发。 |
| Test | TEST_PASSED。2026-11-18 05:04:39–05:17:41 UTC 在独立锁环境运行，3631 项通过，失败、错误、跳过均为 0。Ruff、格式和严格 mypy 通过。347 个公开 Schema 和索引与 WP-03 逐字节相同；两次从零导出一致。 |
| 隔离运行 | RUNTIME_VERIFIED。10 个独立探针全部通过。实际 PostgreSQL、回环 HTTP 和 Linux CPU 子进程验证发送前撤销与在途撤销、调用额度、迟到回执、精确 fallback、嵌套来源、只重试失败步骤及 guardian 异常。两种 N05 故障均观察到 A/B/B 三次物理调用。 |
| 真实环境和质量 | 真实 Provider、RTX 4090、部署容量、标注质量及生产恢复为 RUNTIME_NOT_RUN / UNVERIFIED。用户选择持续本地开发，待真实环境授权后补。 |
| 用户及发布 | 本报告供用户验收。现有 Git 授权未涵盖此包的发布。 |

主控直接解析正式 JUnit 的 3631 条 testcase 并核对 SHA-256 `448675a452dcbb4d2989e38c783d294774b19f55060046c2d87b9eb25ff0d994`。运行使用 CPython 3.12.3、Pydantic 2.12.5、httpx 0.28.1、SQLAlchemy 2.0.52、psycopg 3.3.5。正式命令、每项退出码及环境保存在[独立报告](../../implementation_evidence/WP-04/independent/FROZEN_REVIEW.md)和[机器可读结果](../../implementation_evidence/WP-04/independent/frozen_run_K0PQbl5I/verified_result.json)。全套回归及所有探针前后均复核 ZIP 和工作树字节。

在隔离库验证增量迁移和旧 Execution。无法从历史数据证明的 fence 保持未确认。新 Bundle 要求每个具名工具输出能独立省略。主控使用真实 WP-03 封存代码生成、签名、准入旧 Bundle，再由 WP-04 重开同一 Release；历史成功输出仍可用，旧 Schema 无法表示的部分回包保留原始字节摘要和原因。[跨版本探针](../../implementation_evidence/WP-04/primary/historical_tool_probe.log) · [审查与针对性测试](../../implementation_evidence/WP-04/primary/PRECHECK.md)

宿主无法证明实际 Provider 终态时，provider-inflight 占用保留至可信结算；容器重建涉及 PID namespace 的恢复边界见[真实环境验证协议](REAL_ENVIRONMENT_PROTOCOL.md)。WP-05 接线继续使用本次已封存的本地产品及先前交付的 Domain/Bundle 合同。
