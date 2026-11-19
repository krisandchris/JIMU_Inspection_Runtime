# WP-04 冻结版独立验收

本地验收范围为 Model Gateway、HTTP、派发审计、Tool Executor、N05 子步骤恢复和资源准入。目标为在隔离 PostgreSQL、回环 HTTP 及 CPU 子进程中核对实际行为。工作树基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，正式冻结清单 SHA-256 为 `924eb788eab11bed400ec1f3602a9a30d3df7b43cba79e8601901eb7921f7495`。起跑时 784 个产品文件全部匹配，ZIP SHA-256 为 `929349a4d831ec2c97068f92813686dcaf662489f5b64fbc1d24a07e11aba11f`，其全部唯一成员及工作树路径集合一致。 [验收矩阵](SCOPE.md) [清单](../primary_freeze_924eb788eab1/product_sha256.json) [前置校验](frozen_run_K0PQbl5I/verify-before.log) [ZIP 校验](frozen_run_K0PQbl5I/archive.log)

## 产品变更和兼容性

与 WP-03 的 735 文件产品快照相比，新增 49 项、修改 30 项、删除 0 项。增量新增 Gateway、受控 Tool 执行、资源账本、守护进程和 PostgreSQL 迁移。独立验收文件均在本目录。跨子步骤及模型回退增加内部归属见证，已公开的 347 个 Domain Schema 和索引逐字节一致。HTTP 加入有界依赖 `httpx>=0.28.1,<0.29`，干净环境从锁定依赖离线安装。 [产品路径差异](frozen_run_K0PQbl5I/product-delta.log) [Schema 基线](frozen_run_K0PQbl5I/generated-baseline.log) [锁文件摘要](frozen_run_K0PQbl5I/dependency-lock.log)

迁移 003–006 增加派发、子步骤和历史能力表，并给已有派发结果增加可空的原 fence 见证。历史记录缺少见证时保留缺失语义。独立 PG 使用持久应用身份和单独迁移身份，新库一次应用 001–006，二次运行无待应用迁移。网络测试仅绑定随机回环端口。原响应超限时保留观测边界和安全诊断。预检发现并保留撤销缺拒派审计、子超时恢复、历史引用比较及父子权威输出持久化几类问题及原始失败，冻结探针已覆盖这些修复。 [迁移和实收日志](frozen_run_K0PQbl5I/independent-dispatch.log) [预检取证](PREFLIGHT.md)

## 运行证据

以下 10 个探针全部退出 0。隔离 PostgreSQL 为 16.14，固定镜像 ID 为 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。

| 验收项 | 观测结果 |
|---|---|
| HTTP 物理调度和过期回执 | 回环收到 8 个 POST，PG 有 8 个物理发送意图及 9 条回执。同一逻辑操作跨两个 Attempt，错误 Attempt 无法冒领原派发，迟到的完整响应归原 fence。429 有界退避提示、缺版本、重复键、非有限数和超限拒绝均有单独结果。 [原件](frozen_run_K0PQbl5I/independent-dispatch.log) |
| 显式 fallback | 首部署收全请求后超时，另一个 owner 在新 Attempt 依据显式审核切换至第二部署。两个端点分别实收一次，继续同一操作并保留精确触发及评审。 [原件](frozen_run_K0PQbl5I/independent-fallback.log) |
| 撤销与配额 | 派发前撤销写入 `not_dispatched`，在途撤销保留已确认的物理应答且屏蔽上层可用输出。跨 operation 的 NodeRun 预算只允许一次 POST，超限操作留下独立拒派记录。 [派发前撤销](frozen_run_K0PQbl5I/independent-revocation.log) [在途撤销](frozen_run_K0PQbl5I/independent-inflight-revocation.log) [配额](frozen_run_K0PQbl5I/independent-ceilings.log) |
| N05 独立步骤恢复 | B 首次结构化传输错误和真实 CPU 超时均实际执行 A/B/B。A 继续使用原子 Attempt，B 保存独立失败与成功 Attempt 和同逻辑 operation。两条链均发布唯一 N06、完整 Coverage 和 completed Execution。 [传输错误](frozen_run_K0PQbl5I/independent-tool.log) [真实超时](frozen_run_K0PQbl5I/independent-tool-timeout.log) |
| Model-backed Tool | 首个嵌套模型超时后，由不同 owner 在同一步骤新的子 Attempt 运行已评审 fallback，两个 ToolExecution 均可独立回读。负向用例用真实回包变造 Invocation ID 及引用摘要，真实模型收据可查，工具记录为 failed，无成功步骤选择。 [嵌套回退](frozen_run_K0PQbl5I/independent-nested-fallback.log) [拒绝变造](frozen_run_K0PQbl5I/independent-nested-forgery.log) |
| CPU 容量和终止边界 | SIGKILL 监督进程后脱组进程树回收，新租约随后取得容量，旧代际无法释放。暂停 guardian 越过物理截止期间拒绝重分配；恢复并取得回执后重新准入。强杀 guardian 后缺整树回执时保留占用。超过排队截止的预留释放且工作进程从未启动。 [原件](frozen_run_K0PQbl5I/independent-cpu.log) |

## 正式命令及结果

正式 runner 从 2026-11-18 05:04:39 到 05:17:41 UTC，退出 0。环境为 Linux 6.8 x86_64、CPython 3.12.3、uv 0.9.15、Pydantic 2.12.5、SQLAlchemy 2.0.52、psycopg 3.3.5、httpx 0.28.1、httpcore 1.0.9、pytest 8.4.2、Ruff 0.14.14 和 mypy 1.18.2。每份原始日志保存逐命令、UTC 起止和退出码。 [环境](frozen_run_K0PQbl5I/environment.log) [离线安装](frozen_run_K0PQbl5I/locked-sync.log)

| 检查 | 结果和原件 |
|---|---|
| `uv sync --locked --offline` | 退出 0，独立虚拟环境安装锁定依赖。 [日志](frozen_run_K0PQbl5I/locked-sync.log) |
| 产品 Ruff 和源码格式 | 退出 0，369 个文件格式正确。 [lint](frozen_run_K0PQbl5I/ruff-product.log) [格式](frozen_run_K0PQbl5I/ruff-format-product.log) |
| 独立探针 Ruff | 退出 0。 [日志](frozen_run_K0PQbl5I/ruff-independent.log) |
| `uv run --locked --offline mypy --strict src/jimu_runtime scripts/generate_schemas.py` | 退出 0，149 个源文件无类型问题。 [日志](frozen_run_K0PQbl5I/mypy.log) |
| Schema 检查、两次独立生成及比较 | 全部退出 0，重复生成一致，工作树与生成结果一致，上一包 347 个 Schema 和索引摘要完全匹配。 [生成器](frozen_run_K0PQbl5I/generated-check.log) [独立重现](frozen_run_K0PQbl5I/generated-repeat.log) [工作树比较](frozen_run_K0PQbl5I/generated-working-tree.log) [上一包比较](frozen_run_K0PQbl5I/generated-baseline.log) |
| `git diff --check` | 退出 0。 [日志](frozen_run_K0PQbl5I/git-diff-check.log) |
| `uv run --locked --offline python -m pytest -q -ra -o cache_dir=… --junitxml …` | 退出 0。3631 passed，0 failures、0 errors、0 skipped，656.19 秒。 [日志](frozen_run_K0PQbl5I/pytest.log) [JUnit](frozen_run_K0PQbl5I/pytest.xml) |

真实云 Provider、RTX 4090、目标 PID namespace、真实模型权重、生产数据库和实拍图像均为 `RUNTIME_NOT_RUN`。回环响应和停车 fixture 只证明接入、运行和持久化关系。供应方 `Retry-After` 的实测结论限于 HTTP 解析和限幅，调度退避由已配置 JobBudget 决定。测试结束后用同一清单与 ZIP 再比对，784 个产品文件、工作树路径集合和归档摘要均一致。 [结束摘要](frozen_run_K0PQbl5I/verified_result.json) [再次校验](frozen_run_K0PQbl5I/verify-after.log) [归档复核](frozen_run_K0PQbl5I/archive-after.log)

| 验收门禁 | 判定 |
|---|---|
| 本地代码、公开契约、依赖与全量回归 | **PASS** · `TEST_PASSED` |
| 隔离 PostgreSQL、回环网络与实际 CPU 工作者 | **PASS** · `RUNTIME_VERIFIED` |
| 真实供应方、GPU、目标主机与模型质量 | **CONDITIONAL** · `RUNTIME_NOT_RUN`，等待对应环境与测试授权 |

