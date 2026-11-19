# WP-05 工作者兼容性复核

ACTIVE 工作树为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，包括 WP-05 当轮尚未提交的实现。测试依赖锁 SHA-256 为 `10f8ba1e57d15b6c5185e08d9eaeb1f0007230e4fc77a02b221c634cae81c7f4`。宿主 Linux `6.8.0-149-generic` x86_64，glibc `2.39`，uv `0.9.15`，Pydantic `2.12.5`，pytest `8.4.2`。

缓存 CPython 3.12.12 缺少 `os.memfd_create`、`fcntl.F_GET_SEALS`、`os.pidfd_open` 和 `signal.pidfd_send_signal`。Ubuntu 的 CPython 3.12.3 提供这四个绑定。两套解释器共用支持相关操作的内核和 libc。这是解释器构建差异。改动范围限可信 Linux 进程操作，无 Domain wire、数据库 Schema、GPU 或外部调用变更。

- `workers/sealed_memory.py` 优先使用 CPython 绑定，缺少创建绑定时仅访问加载进当前进程的固定 libc `memfd_create`。创建采用固定匿名名称和 Linux 的 `MFD_CLOEXEC | MFD_ALLOW_SEALING`。使用已核对的本机 `linux/memfd.h`、`linux/fcntl.h`、`asm-generic/fcntl.h` 常量完成内核操作。在交付 fd 前按 `F_GET_SEALS` 核验 WRITE、GROW、SHRINK 和 SEAL。
- `workers/lifecycle.py` 与几何作者接线后的 `tools/registry.py` 共用该 helper。缺内核能力、无法加封或检测到未完整封存时以类型化错误拒绝工件。
- `workers/process_guardian.py` 优先使用 CPython 的 PIDFD 接口，缺绑定时使用当前进程 libc 的固定 `pidfd_open` 和 `pidfd_send_signal`。实际 worker 启动前用本进程的 pidfd 发送信号 0 验证能力。强制清理依赖内核绑定的进程句柄。平台缺能力时拒绝启动工人进程。
- `tests/unit/test_wp05_sealed_memory.py` 检验真实内核拒绝写入、收缩和增长，拒绝未封存的 memfd 和普通磁盘 fd，并模拟缺少内核封存能力的错误。WP-04 既有真实 guardian/supervisor 用例只替换了测试工件的 fd 创建与封存器。

## 本地验证

两次运行均使用命令 `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 <python> -m pytest -q tests/unit/test_wp05_sealed_memory.py tests/unit/test_wp04_resource_inventory.py tests/contract/test_wp04_worker_lifecycle.py tests/contract/test_dependency_boundaries.py::test_runtime_layers_have_a_pure_domain_and_an_acyclic_import_graph`，覆盖实际封存、篡改拒绝、已封存 fd 传递、进程组强杀、独立会话后代退出与回执。

| 解释器 | SQLite | 退出码和结果 | UTC |
|---|---|---|---|
| `.venv/bin/python`，CPython 3.12.3 GCC 13.3.0 | 3.45.1 | 0，36 passed in 14.73s，0 skipped | 05:57:20–05:57:35 |
| `/tmp/jimu-wp05-predicates-nw7hqmx7-venv/bin/python`，缓存 CPython 3.12.12 Clang 21.1.4 | 3.50.4 | 0，36 passed in 14.88s，0 skipped | 05:57:20–05:57:36 |

`.venv/bin/ruff check src/jimu_runtime/workers tests/unit/test_wp05_sealed_memory.py tests/contract/test_wp04_worker_lifecycle.py`、相同路径的 Ruff 格式检查、`.venv/bin/mypy src/jimu_runtime/workers`（8 个源文件）及 `git diff --check` 均退出 0。

## 实际安装 wheel

2026-11-18 UTC 05:58:55 用 `uv build --offline --wheel --out-dir /tmp/jimu-wp05-compat-wheel.Vombujl7` 本地构建。SHA-256 为 `fbb31b022d29cd0ccdb7fdc5ae46de584001df432d5b11556d42dc3205c2bb3e`。首次尝试直接执行未锁定的离线依赖解析，由于缓存不能解析 `pydantic-core==2.41.5` 而退出 1。随后使用保留 lock 的成功路径。

1. 设 `UV_PROJECT_ENVIRONMENT=/tmp/jimu-wp05-compat-wheel.Vombujl7/venv` 与 `UV_PYTHON=/home/hy/.local/share/uv/python/cpython-3.12.12-linux-x86_64-gnu/bin/python3.12`，执行 `uv sync --locked --offline --no-dev --no-install-project`。
2. 执行 `uv pip install --offline --no-deps --python /tmp/jimu-wp05-compat-wheel.Vombujl7/venv/bin/python /tmp/jimu-wp05-compat-wheel.Vombujl7/jimu_runtime-0.1.0-py3-none-any.whl`。
3. 在构建临时目录、清除 PYTHONPATH 并使用 `env -u PYTHONPATH /tmp/jimu-wp05-compat-wheel.Vombujl7/venv/bin/python -I -`，从标准输入运行 smoke probe。确认 `sealed_memory.__file__` 位于该 venv 的 site-packages 下，成功导入 WorkflowWorker，使用实际上锁的 memfd 使 `os.pwrite` 返回 PermissionError，并用已安装的 IsolatedWorker、真实 guardian 和 CPU 子进程读回原字节，随后确认 resource lease 已释放。

锁定路径退出 0，输出 `WorkflowWorker guardian read sealed bytes and exited`，UTC 05:59:33–05:59:36。早先试用仅有谓词依赖的 3.12.12 venv 直接导入 WorkflowWorker 缺 SQLAlchemy。正式 smoke 使用锁定的完整运行环境。

`TEST_PASSED` / `RUNTIME_VERIFIED` 限上述 Linux 测试宿主和隔离 CPU 工作。真实 GPU/Provider 与生产部署为 `RUNTIME_NOT_RUN`。
