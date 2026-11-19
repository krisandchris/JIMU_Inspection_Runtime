# WP-03 数据库契约与共享仓储验证

`TEST_PASSED` · `RUNTIME_VERIFIED`（独立本地 PostgreSQL，合成记录）

工作树为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，包括未提交的 WP-01C、WP-02 和 WP-03 文件。2026-11-18 03:41:04–03:41:44 UTC 运行。PostgreSQL 16.14 来自本机缓存镜像 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。Docker 29.9.3，Linux 6.8.0-149-generic x86_64，Python 3.12.3，SQLAlchemy 2.0.52，psycopg 3.3.5，Pydantic 2.12.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。

## 变更

- `planning/active/WP-03/DB_CONTRACT.md`，共享表、列、作用域、锁顺序和仓储接口。
- `src/jimu_runtime/workflow/migrations/002_workflow_tables.sql`，业务表及应用角色权限。
- `src/jimu_runtime/workflow/repository.py`，基于调用方事务的 Domain 恢复、接入、生命周期、产物与审计。
- `tests/{contract,negative,unit}/test_wp03_repository_*.py` 和 `tests/fixtures/wp03/repository/support.py`。

Domain 及生成的外部 Schema 未发生变化。服务将作用域内幂等键的原始 UTF-8 字节作为 SQL 索引，JSON 文档保存合法的转义 NUL。任务验收快照、执行和节点的初始记录及 pin 仅允许写入一次。可变投影、启动和终结尝试、产物引用、事件及暂存决策分别落库。SQL 复合外键验证行粒度的作用域和归属。Domain 校验原始记录、索引和值之间的关系。聚合服务在同一事务写入决策、Coverage、Execution 和 Task 当前状态，成功释放前查询不可变终结结果和 Execution 终态。SQL 没有把所有独立 Execution 的完成结果绑定为原 Task 的结果。

## 已运行命令

```sh
.venv/bin/python -m pytest -q tests/contract/test_wp03_repository_roundtrip.py tests/negative/test_wp03_repository_rejections.py tests/unit/test_wp03_repository_projection.py tests/contract/test_wp03_intake_recovery.py tests/negative/test_wp03_intake_conflicts.py tests/contract/test_wp03_jobs.py tests/contract/test_wp03_postgres_migrations.py tests/negative/test_wp03_postgres_migration_rejections.py --junitxml=planning/implementation_evidence/WP-03/repository/pytest.xml
.venv/bin/ruff check src/jimu_runtime/workflow/repository.py tests/contract/test_wp03_repository_roundtrip.py tests/negative/test_wp03_repository_rejections.py tests/unit/test_wp03_repository_projection.py tests/fixtures/wp03/repository
.venv/bin/mypy src/jimu_runtime/workflow/repository.py
```

pytest 退出码 0，`25 passed in 39.69s`，零失败、错误或跳过。Ruff 和严格 mypy 退出码 0。首次使用 `.venv/bin/pytest` 直接执行接入套件时因导入根路径退出 2，切换到 `python -m pytest` 后运行通过。

- 两连接并发 finalize 同一请求，验证一个 Task、Execution 和三条初始事件所对应的持久记录。同作用域的 request ID 和 idempotency key 冲突分别拒绝，交叉匹配拒绝。原认证身份和合法 NUL 字节经重读核对。宿主分页只返回指定作用域的 pending 记录，并验证光标、失败排除与页长上限。
- 使用独立应用角色验证迁移记录无写权、历史事件无 UPDATE/DELETE/TRUNCATE 权限、Attempt 快照及固定策略无 UPDATE 权限。跨 Execution 后继关联由 PostgreSQL 外键拒绝。
- 回滚后权威产物、任务与执行终态、审计和后继登记均消失。再次提交可见完整结果、终结记录及连续事件。不同字节重复提交终结结果被拒。
- 合并运行覆盖独立进程完成事务持锁时被 SIGKILL、租约过期接管、数据库锁后时钟验证、完成收据幂等、迟到审计、取消、N06 暂存后重启与聚合。

运行限独占的临时回环 PostgreSQL，迁移和应用分权。无生产数据库、模型 API 或 GPU 调用。失败的临时库由 fixture 清理，生产或未知库无迁移。本报告覆盖仓储及相邻入口的局部验收，完整六节点纵链由 W3.5 独立验收。

## 文件校验

```text
9a48bfd3146120b7598e45d85e3e0572f5582b1ce2fccc26d66d9bc5e792c285  src/jimu_runtime/workflow/migrations/002_workflow_tables.sql
d84761f15377c00b596f71c5753a8a14400ceffd8e89538e7b7c5a24e0d03d0a  src/jimu_runtime/workflow/repository.py
89b79940788ecf8df037e7204f877941cf9b966cbaaef56167e7a1933b467c80  uv.lock
```
