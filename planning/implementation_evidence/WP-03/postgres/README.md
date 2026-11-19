# W3.1 PostgreSQL 工程证据

**验收 PASS · TEST_PASSED · RUNTIME_VERIFIED。** W3.1 在经独立复核的 WP-02 第二冻结产品上交付异步 PostgreSQL 连接、隔离库 fixture、角色边界和版本化 SQL 执行入口。基线为 [WP-02 冻结清单](../../WP-02/primary_freeze_844ed4ac1275/snapshot.json)。本组证据覆盖真实 PostgreSQL 16.14 与本地 Python，业务工作流的整包运行由 W3.5 另行验收。

## 交付

- `src/jimu_runtime/workflow/{__init__.py,postgres.py,migrations/__init__.py,migrations/001_workflow_schema.sql}`。连接工厂使用显式 psycopg URL 和 SQLAlchemy AsyncEngine。迁移先验证由宿主提供的数据库名称、owner、实例标识及数据库中的实际角色，再于 PostgreSQL 单事务内串行应用编号连续的 SQL 并保存原始字节的 SHA-256。启动时以持久历史为准，脚本修订、版本倒退、非连续或未知记录均拒绝。
- `pyproject.toml`、`uv.lock`。新增 SQLAlchemy 2.0.52、psycopg 3.3.5 及所需传递依赖。旧冻结 ZIP 与新锁逐个比较，既有第三方记录相同，仅根包依赖元数据更新。官方 PyPI 保持唯一锁定源。异步方言和 binary wheel 选择依据对应官方安装说明。[SQLAlchemy PostgreSQL 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg) · [psycopg 安装](https://www.psycopg.org/psycopg3/docs/basic/install.html)
- `tests/fixtures/wp03/postgres/` 和 `tests/{unit,contract,negative}/test_wp03_postgres*.py`。使用本地缓存镜像，在独占容器的新数据库验证空库迁移、首次迁移竞争、查询连接重开、拒绝未确认实例、append-only 权限、失败事务、重试及历史漂移。
- `tests/contract/test_dependency_boundaries.py`、`tests/helpers/boundary_graph.py`、`tests/unit/test_b_dependency_graph.py`。仅白名单中的 workflow PostgreSQL 模块可直接导入 SQLAlchemy。Domain 独立于 Bundle 和 Workflow，Bundle 独立于 Workflow，间接 reexport 同样检查。已有第三方库及防环断言保留。

## 验证结果

命令从同一工作树根执行。日志中的临时目录为 `/tmp/jimu-wp03-w31.vOfF0s`。归档保留输出和 JUnit，临时密码与连接字符串未入档。

| 精确命令或命令主体 | 退出 | 结果 |
|---|---:|---|
| `uv run --locked --no-sync python -m pytest tests/unit/test_wp03_postgres_catalogue.py tests/contract/test_wp03_postgres_migrations.py tests/negative/test_wp03_postgres_migration_rejections.py -q --junitxml=/tmp/jimu-wp03-postgres-first-run.xml` | 0 | 17 passed。[JUnit](pytest-first-run.xml) |
| `uv run --locked --no-sync python -m pytest tests/unit/test_wp03_postgres_catalogue.py tests/contract/test_wp03_postgres_migrations.py tests/negative/test_wp03_postgres_migration_rejections.py tests/contract/test_dependency_boundaries.py tests/unit/test_b_dependency_graph.py -q --junitxml=/tmp/jimu-wp03-w31.vOfF0s/pytest-combined.xml` | 0 | 47 passed，0 skipped。[输出](pytest-combined.log) · [JUnit](pytest-combined.xml) |
| `uv run --locked --no-sync ruff check src/jimu_runtime/workflow/postgres.py tests/{unit,contract,negative}/test_wp03_postgres*.py tests/fixtures/wp03/postgres/support.py tests/contract/test_dependency_boundaries.py tests/helpers/boundary_graph.py tests/unit/test_b_dependency_graph.py` | 0 | All checks passed。[输出](ruff-check-final.log) |
| `uv run --locked --no-sync ruff format --check src/jimu_runtime/workflow/postgres.py tests/{unit,contract,negative}/test_wp03_postgres*.py tests/fixtures/wp03/postgres/support.py tests/contract/test_dependency_boundaries.py tests/helpers/boundary_graph.py tests/unit/test_b_dependency_graph.py` | 0 | 8 files already formatted。[输出](check-2.log) |
| `uv run --locked --no-sync mypy src/jimu_runtime/workflow/postgres.py` | 0 | 严格类型检查通过。[输出](check-3.log) |
| `uv --no-config lock --check --offline --default-index https://pypi.org/simple` | 0 | 锁校验通过。[输出](uv-lock-check.log) |
| `UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp03-w31.vOfF0s/runtime-env uv --no-config sync --locked --no-dev --offline --default-index https://pypi.org/simple` | 0 | 新建 Python 3.12.12 运行环境，16 个发行包。[输出](uv-sync-no-dev.log) |
| `/tmp/jimu-wp03-w31.vOfF0s/runtime-env/bin/python -` | 0 | 运行环境启动专用临时库，调用 `apply_migrations` 返回 `(1, 2)`，且从活动连接查询 PostgreSQL 16.14。[输出](no-dev-postgres.log) |
| `uv --no-config build --wheel --offline --out-dir /tmp/jimu-wp03-w31.vOfF0s/wheel` | 0 | 构建成功，wheel 中 001 SQL 与源字节相同。[输出](check-4.log) · [摘要](wheel-content.log) |

首次 `uv lock --offline` 与显式官方源离线解析均缺少完整注册表或跨平台轮子元数据，退出 1。使用 `uv --no-config lock --default-index https://pypi.org/simple` 后锁定 28 条包记录。第一次显式官方源离线同步缺少相应缓存键的 Linux CPython 3.12 psycopg-binary wheel，随后 `uv --no-config sync --locked --default-index https://pypi.org/simple` 同步成功并通过上述全新环境离线重建。

初次收集发现两个测试文件同名，改名后 17 项通过。共享工作树中的第一次 47 项合跑遇到其他批次尚未写完的 `workflow.repository`，依赖图按未解析导入拒绝。该文件到位后复跑通过。隔离部分在等待期间独立通过 46 项。[阶段输出](check-0.log) · [隔离输出](pytest-isolated.log)

## 数据与运行边界

并发测试用第三连接持有迁移事务锁，以 `pg_locks` 确认两个连接同时等待，释放后仅一个连接应用版本，另一个得到空增量。失败测试在已有版本上注入 SQL 错误和显式提交，验证新增 DDL 与版本记录一起回滚，再正常重试。使用应用角色直接尝试 DDL、迁移表改写及历史记录的 UPDATE、DELETE、TRUNCATE 均遭数据库拒绝。可恢复路径为对已验证的同一库重新运行原脚本，已应用版本的变化以新编号交付。

[环境记录](environment.json)对应 Linux x86_64、Python 3.12.3、uv 0.9.15、Docker 29.9.3 和已实际查询的 PostgreSQL 16.14。容器仅使用本地镜像、短期 loopback 随机端口、随机角色密码与临时数据路径，结束后清理。现有数据库与历史 Release 数据无迁移影响。真实模型 API、GPU 和生产环境 RUNTIME_NOT_RUN。
