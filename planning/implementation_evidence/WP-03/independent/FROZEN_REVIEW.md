# WP-03 冻结快照独立验收

审查目标为按已授权计划交付本地 PostgreSQL 持久化工作流、租约与接管和合成停车六节点。工作树基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。冻结清单 SHA-256 为 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`，覆盖 735 个产品文件。ZIP SHA-256 为 `a9feca7f565f22614f9eb3fca5f332a467eee77b76ef6f10ba7d6431e88cab70`，每个 ZIP 成员摘要、唯一文件名和工作树产品路径集合独立核对。 [冻结清单](../primary_freeze_b8e95556aa80/product_sha256.json) [ZIP 复核](frozen_run_uXkkc6tO/archive-repeat.log)

## 产品及契约

相对 WP-02 产品冻结新增 67 个文件，修改 10 个，移除 0 个。新增文件包含工作流迁移、接入、仓储、调度、执行与合成适配器以及测试。旧文件修改涉及有界依赖和 lock、生成器类型整理、历史 Task Activation 查询、已有 Coverage 验证器兼容扩展、引用边界检查。独立 reviewer 的新增产物全部在本证据目录。 [范围对照](../primary_freeze_b8e95556aa80/snapshot.json) [验收矩阵](SCOPE.md)

PostgreSQL 保存版本化执行状态、草稿、权威产物及事件。现有生成 Schema 检查与契约回归记录在本报告的命令表中。历史 Release 通过 Task 自己的 Activation 和作用域读取。N06 的已启动 Attempt 与草稿可先落库，权威选择和最终聚合在同一发布事务中完成。审查区分 Task 和 Execution 生命周期、Coverage 与业务结论，以及终结 Attempt 与选定权威的结果。

## 运行证据

`RUNTIME_VERIFIED` 适用于 Linux x86_64 上的隔离 PostgreSQL 16.14 与确定性 mock。固定镜像 ID 为 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。临时数据库的两次迁移返回 `(1, 2)` 和空结果。非 owner、非 superuser 应用角色越权执行 DDL、历史行写操作和迁移登记更新均被拒绝。 [正式 PostgreSQL 日志](frozen_run_uXkkc6tO/independent-pg.log)

- 两独立 worker 使用不同进程和 PG 后端。首次续租延长期限。第二次续租在期限内进入锁等待，取得锁后由实际数据库时钟拒绝。接手 fence 由 1 增为 2，原轮次为 superseded，重复的迟到响应只保留一个审计事件。
- 两 Profile 产生三个 Object，N04 存在三个合成联合调用和六条 Question Fact。三条 ToolExecution、三条 Tool Fact、三份 Decision 和三项 mandatory 匹配按 Object 连接。独立重算 60 个数据库权威产物的 SHA-256。
- N02 mock 首次完成后、数据库提交前被 SIGKILL。随后有新 Attempt 和新调用 ID，只有后一 Attempt 成为权威。三份 N06 草稿落库后再次 SIGKILL。发布前无终结 Attempt、N06 权威或最终报告。切换活跃 Release 后，新的 worker 仅凭作用域发现并按原 Release 发布三份权威与一份聚合，保留原先 63 条事件。发布后重启仍是 71 条事件，报告、选择和计数无变化。
- 无 eligible Profile 为 `not_evaluated/not_applicable`。真实 mock 调用 no_hits 及 grounding no_target 均给出 `none/uncertain`。一个 Profile grounding 失败时保留另一 Profile 的有效违章及 `partial`。跳过或阻塞分支无 Attempt。未调用时提交的取消意图在进程被终止后恢复，Task、Execution、NodeRun 一致取消且零 Attempt。

## 权限、数据及限制

使用全新独占容器、随机 loopback 端口、一次性身份、合成 RGB 原件和独立本地 Bundle ledger。SQL 迁移 owner 与应用角色分离，历史表由应用追加。进程被终止期间 PG 容器持续运行。真实 Provider、GPU、生产数据库、生产数据、远端部署及数据库容器磁盘恢复均为 `RUNTIME_NOT_RUN`。运行观察仅支持本地持久化、调度和合成关系。预检发现及原始失败留存。 [预检过程](PREFLIGHT.md)

## 测试、生成与交付检查

`TEST_PASSED`。冻结复测从 2026-11-18 03:56:58 UTC 到 04:06:31 UTC，runner 退出码 0。Linux 6.8 x86_64，Python 3.12.3，uv 0.9.15，Pydantic 2.12.5，SQLAlchemy 2.0.52，psycopg 和 psycopg-binary 3.3.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2，rfc8785 0.1.4，jsonschema 4.25.1。 [环境日志](frozen_run_uXkkc6tO/environment.log) [uv 日志](frozen_run_uXkkc6tO/uv-version.log)

| 检查 | 退出码和结果 | 精确命令及原结果 |
|---|---|---|
| Git 基线 `git rev-parse HEAD` | 0。与冻结基线相同 | [日志](frozen_run_uXkkc6tO/baseline.log) |
| 冻结清单前校验 | 0。735 项 SHA-256 无差异 | [日志](frozen_run_uXkkc6tO/verify-before.log) |
| 独立 ZIP 核验 | 0。735 项成员、工作树路径集合及摘要一致 | [日志](frozen_run_uXkkc6tO/archive-repeat.log) |
| 公开 PostgreSQL 探针 | 0。真实 PG、进程间接管、崩溃、取消及六节点合成链 | [日志](frozen_run_uXkkc6tO/independent-pg.log) |
| `uv run --locked python -m pytest -q -ra --junitxml …` | 0。3363 passed，0 failures，0 errors，0 skipped，481.11 秒 | [日志](frozen_run_uXkkc6tO/pytest.log) [JUnit](frozen_run_uXkkc6tO/pytest.xml) |
| `uv run --locked ruff check src tests scripts` | 0。全部通过 | [日志](frozen_run_uXkkc6tO/ruff-product.log) |
| `uv run --locked ruff format --check src tests scripts` | 0。324 个已格式化文件 | [日志](frozen_run_uXkkc6tO/ruff-format-product.log) |
| Ruff 独立验收脚本 | 0。全部通过 | [日志](frozen_run_uXkkc6tO/ruff-independent.log) |
| `uv run --locked ruff check .` | 0。全树全部通过 | [日志](frozen_run_uXkkc6tO/ruff-all.log) |
| `uv run --locked mypy --strict src/jimu_runtime scripts/generate_schemas.py` | 0。126 个源文件无诊断 | [日志](frozen_run_uXkkc6tO/mypy.log) |
| `uv run --locked python scripts/generate_schemas.py --check` | 0。生成内容匹配 | [日志](frozen_run_uXkkc6tO/generated-check.log) |
| 独立临时目录导出 1、2 | 0、0。两次均成功 | [首次](frozen_run_uXkkc6tO/generated-first.log) [第二次](frozen_run_uXkkc6tO/generated-second.log) |
| `diff --recursive --brief …` 两次输出、工作树与输出 | 0、0。无差异 | [独立输出比较](frozen_run_uXkkc6tO/generated-independent-diff.log) [工作树比较](frozen_run_uXkkc6tO/generated-working-tree-diff.log) |
| WP-02 冻结 Schema 摘要核对 | 0。347 个 Schema 和一个 index 的字节全部一致 | [日志](frozen_run_uXkkc6tO/generated-baseline.log) |
| `git diff --check` | 0。无错误 | [日志](frozen_run_uXkkc6tO/git-diff-check.log) |
| WP-02 到 WP-03 产品范围比较 | 0。新增 67、修改 10、移除 0 | [日志](frozen_run_uXkkc6tO/product-delta.log) |
| 冻结清单后校验 | 0。735 项 SHA-256 无差异 | [日志](frozen_run_uXkkc6tO/verify-after.log) |

全量 JUnit 包含 1,065 项 unit、546 项 contract 和 1,752 项 negative。WP-03 命名空间下 165 项随全量运行，其中包含真实 PostgreSQL 的租约耗尽、阻塞证据、已启动轮次与发布恢复测试。冻结过程中无测试因缺少依赖跳过。

## 判断

| 计划门槛 | 结论 |
|---|---|
| 隔离数据库迁移、权限、作用域与接入幂等 | PASS |
| 正常六节点、两 Profile、多 Object 和多 Question 关系 | PASS |
| 过期、续租、fence、旧响应追加及历史 Release pin | PASS |
| 延迟发布、重启、恢复、空分支与部分覆盖 | PASS |
| 全量回归、类型检查、Schema 及冻结交付件 | PASS |
| 真实 Provider、GPU、生产部署与业务质量 | `RUNTIME_NOT_RUN` |

WP-03 本地工程验收为 **PASS**。下一包依路线图为 WP-04 的 Model Gateway 和 Tool Executor，由主控按已授权范围另行放行。
