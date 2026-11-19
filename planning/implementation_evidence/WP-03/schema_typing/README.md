# WP-03 Schema 导出类型门禁

主控复跑 `uv run --locked --no-sync mypy src scripts/generate_schemas.py` 时发现导出器的 jsonschema 缺类型声明，以及入口 `main` 被推断为 `object`。导出器保留延迟导入、Schema 校验与公开导出入口，通过显式 `Callable[[], int]` 给 CLI 入口独立类型。开发环境增加与已锁运行库匹配的 `types-jsonschema` 4.25.1.20251009。移除了 Bundle 中两条既有的 import-untyped 抑制。运行依赖和所有既有第三方锁记录经逐包比较相同。[类型声明版本](https://pypi.org/project/types-jsonschema/4.25.1.20251009/) · [锁变化](lock-delta.json)

## 验证

以下命令在 WP-03 共享工作树根执行，临时目录为 `/tmp/jimu-wp03-typing.lNEhzl`。主环境 Python 3.12.3、uv 0.9.15、mypy 1.18.2。全新离线运行环境 Python 3.12.12。

| 精确命令 | 退出 | 结果 |
|---|---:|---|
| `uv run --locked --no-sync mypy src scripts/generate_schemas.py` | 0 | 126 个源文件通过 strict 检查。[输出](mypy-final.log) |
| `uv run --locked --no-sync ruff check scripts/generate_schemas.py src/jimu_runtime/bundle/artifacts.py` | 0 | All checks passed |
| `uv run --locked --no-sync ruff format --check scripts/generate_schemas.py src/jimu_runtime/bundle/artifacts.py` | 0 | 2 files already formatted |
| `uv run --locked --no-sync python scripts/generate_schemas.py --check` | 0 | Schema export matches。[输出](cli.log) |
| `uv run --locked --no-sync python scripts/generate_schemas.py --output /tmp/jimu-wp03-typing.lNEhzl/generated-final` | 0 | 新导出 347 份 Schema 与 index.json。逐文件对比既有目录和 WP-02 的冻结 ZIP，全部字节相同。[比较输出](schema-byte-comparison.log) |
| `uv --no-config lock --check --offline --default-index https://pypi.org/simple` | 0 | 29 条锁记录通过校验。[输出](uv-lock-check.log) |
| `UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp03-typing.lNEhzl/runtime-env uv --no-config sync --locked --no-dev --offline --default-index https://pypi.org/simple` | 0 | 安装 16 个发行包。隔离运行环境实际加载 jsonschema 4.25.1，未安装 stub，并通过 Draft 2020-12 校验冒烟。[安装输出](uv-no-dev.log) · [冒烟输出](runtime-package-scope.log) |
| `/tmp/jimu-wp03-typing.lNEhzl/runtime-env/bin/python scripts/generate_schemas.py --check` | 0 | 全新离线运行环境检查通过。[输出](cli.log) |
| `uv run --locked --no-sync python -m pytest tests/unit/test_b_schema_export.py tests/contract/test_b_schema_inventory.py tests/contract/test_c_schema_inventory.py tests/contract/test_dependency_boundaries.py -q --junitxml=/tmp/jimu-wp03-typing.lNEhzl/pytest.xml` | 1 | 106 passed。唯一失败为共享 `artifacts.cpython-312.pyc` 在并行运行环境冒烟期间刷新导致的文件树比较。[输出](pytest-first.log) · [JUnit](pytest-first.xml) |
| `uv run --locked --no-sync python -m pytest tests/unit/test_b_schema_export.py -xq` | 0 | 28 passed，0 skipped。冻结产品字节保持一致。 |
| `PYTHONDONTWRITEBYTECODE=1 uv run --locked --no-sync python -m pytest 'tests/unit/test_b_schema_export.py::test_cli_check_is_read_only_and_detects_mismatch[valid]' -q --junitxml=/tmp/jimu-wp03-typing.lNEhzl/pytest-valid-recheck.xml` | 0 | 1 passed，单独复验首轮缓存断言通过。[输出](pytest-valid-recheck.log) · [JUnit](pytest-valid-recheck.xml) |
