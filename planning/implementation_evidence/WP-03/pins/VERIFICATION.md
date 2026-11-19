# W3.2 历史执行固定项解析

状态为 `TEST_PASSED` 和 `RUNTIME_NOT_RUN`。本项开发验收为 PASS。工作树基线为 detached `2552affeb69fbc2594fe70724f895b0db0967d47`，含 WP-01C、WP-02 和并行 WP-03 工作。本项运行了隔离本地 SQLite 中的完整 WP-02 编译、OpenSSL Ed25519 签名与验签、准入、批准、激活、Task 接收和历史回读。没有执行 PostgreSQL、真实模型请求、Tool 或 GPU。W3.2 数据库接线与 WP-03 纵链由对应批次验收。

## 交付与范围

- `src/jimu_runtime/workflow/pins.py` 提供 `resolve_execution_pins`，返回已有 Domain `ExecutionPins`、精确执行策略引用和策略、同包的校验文件和制品目录、Schema 和 FactSchema 视图及宿主模型部署版本。
- `tests/unit/test_wp03_pins.py`、`tests/contract/test_wp03_pins_contract.py`、`tests/negative/test_wp03_pins_invalid.py` 与 `tests/fixtures/wp03/pins/` 覆盖真实编译准入纵链、固定向量及拒绝情形。
- 接入宿主负责授权、传入受信项目与 client 作用域、受信快照、UTC 时间和归档预算。解析器核对不可变 Task、Release、准入回执、Bundle 身份根和 ZIP 字节。它只调用 WP-02 公开读接口，历史恢复与当前激活指针独立。
- 策略使用唯一的 `policies/execution.json` 根制品。模型绑定复用准入的策略、精确回退评估和主机部署匹配校验。工具按原回执的唯一匹配支持项固定实现版本，允许版本集合中的其他可用实现也会被拒绝。任何已声明的工具缺少受信支持都会阻止生成完整的执行固定项。
- 版本化输入摘要使用 `jimu.execution-input.v1` 的明确字段及 UTF-8 stable JSON，绑定 Task 及作用域、媒体及上下文、Release 及准入回执、Bundle、宿主运行版本、工作流和策略、模型配置及实际部署版本和工具版本。按精确引用排序模型绑定和工具，保留路由部署声明顺序。固定向量将合成回执 ID 固定为 `admission-00000000000000000000000000000003`，预期摘要为 `6908fea03da7b95250ebee8d1d9fe44769f362a71b52c53f3c4c16450757eaa8`。
- 新增模块只读，未修改公共 Domain 与 Schema、写入历史、变更依赖或运行迁移。工作树回退范围为清单所列新增解析器、测试、fixture 和证据。

## 验证记录

环境为 Linux x86_64、Python 3.12.3、Pydantic 2.12.5、pydantic-core 2.41.5、pytest 8.4.2、Ruff 0.14.14、mypy 1.18.2、jsonschema 4.25.1、rfc8785 0.1.4、OpenSSL 3.6.1。

| 命令 | UTC 起止或耗时 | 退出码与输出 |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q tests/unit/test_wp03_pins.py tests/contract/test_wp03_pins_contract.py tests/negative/test_wp03_pins_invalid.py` | 25.53 秒 | 0，18 passed，无跳过 |
| `.venv/bin/python -m pytest -q tests/unit/test_wp03_pins.py tests/contract/test_wp03_pins_contract.py tests/negative/test_wp03_pins_invalid.py tests/unit/test_wp02_admission_flow.py tests/unit/test_wp02_releases.py tests/contract/test_c_execution_schema.py` | 03:30:26–03:30:55，2026-11-18 | 0，30 passed，无跳过 |
| `.venv/bin/ruff check src/jimu_runtime/workflow/pins.py tests/unit/test_wp03_pins.py tests/contract/test_wp03_pins_contract.py tests/negative/test_wp03_pins_invalid.py tests/fixtures/wp03/pins/support.py` | 03:30:26，2026-11-18 | 0，All checks passed |
| `.venv/bin/ruff format --check src/jimu_runtime/workflow/pins.py tests/unit/test_wp03_pins.py tests/contract/test_wp03_pins_contract.py tests/negative/test_wp03_pins_invalid.py tests/fixtures/wp03/pins/support.py` | 03:30:26，2026-11-18 | 0，5 files already formatted |
| `.venv/bin/mypy --strict src/jimu_runtime/workflow/pins.py` | 03:30:35，2026-11-18 | 0，Success，1 source file |
| `env MYPYPATH=src .venv/bin/mypy --strict --explicit-package-bases --follow-imports=silent src/jimu_runtime/workflow/pins.py tests/unit/test_wp03_pins.py tests/contract/test_wp03_pins_contract.py tests/negative/test_wp03_pins_invalid.py tests/fixtures/wp03/pins/support.py` | 1.57 秒 | 0，Success，5 source files |

开发中从可执行文件直接调用 pytest 时出现仓库 tests 模块导入失败，改用 `python -m pytest`。首次汇总收集的同名测试文件已改名，fixture 的类型引用等值和 dataclass 替换断言已修正。直接对命名空间 fixture 与产品代码执行 mypy 时发生双重模块归属，使用显式包根与本仓库 `src` 通过。上表均为整改后执行的命令。

真实测试数据来自冻结 WP-02 非机动车合成场景。测试验证了激活切换和重开后历史固定项仍相同，策略同版本但引用变化可区分，宿主注册顺序变化稳定，已评估回退和确切部署存在时可解析。拒绝覆盖跨作用域、伪造历史 Task、更换接收仓库、ZIP 根更换、同根异字节 ZIP、损坏 ZIP、策略缺失或歧义、工作流和 runtime 不兼容、模型或工具不可用以及替换回执所选的工具版本。下一个验证点为 W3.2 仓储和 WP-03 接线。
