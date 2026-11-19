# WP-02 独立工程验收

**PASS**。验收对象是 WP-02 的本地 Bundle 编译、技术准入、不可变制品存储和 Release 生命周期。第二轮冻结覆盖 668 个产品文件。测试前后及每条命令前后都匹配 SHA-256 `844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556`，无新增、消失或变化的产品路径。[正式冻结](../primary_freeze_844ed4ac1275/snapshot.json) · [第二轮结果](frozen_0535kpib/run_result.json)

| 证据层 | 判定 | 实际结果 |
|---|---|---|
| Code | `STATIC_REVIEW_ONLY` 与静态检查 PASS | 逐项复核 Bundle 规格 §5 的 14 步、§6 准入检查和 §12 的 11 条。Ruff lint 通过，261 个文件格式正确，严格 mypy 检查 100 个源文件通过。 [覆盖矩阵](PREFREEZE_MATRIX.md) · [日志](frozen_0535kpib/commands.json) |
| Test | `TEST_PASSED` | CPU 全量 3191 项通过，0 失败、0 错误、0 跳过，276.23 秒。两次 Schema 导出和仓库 `--check` 均成功，347 份 WP-01C Schema 与 `index.json` 在旧清单、两份独立输出、当前冻结间逐文件一致。 [JUnit](frozen_0535kpib/pytest.xml) · [逐文件 Schema 比较](frozen_0535kpib/schemas_comparison.json) |
| Isolated Runtime | `RUNTIME_VERIFIED` | 以真实 OpenSSL Ed25519、真实临时 SQLite 和公开入口串通合成非机动车停车场景。使用临时、锁定、无开发依赖的环境重复相同纵链亦通过。 [正式探针输出](frozen_0535kpib/isolated_runtime.stdout) · [无 dev 复测](runtime_deps_xcl5xmpf/run_result.json) |
| Production Quality | `RUNTIME_NOT_RUN` / `UNVERIFIED` | 生产信任根、签发人和撤销策略、业务批准人/作用域、生产接入验签、真实设备和模型／Tool、真值数据及目标机容量需要对应部署与负责人验证。 |

## 改动与合同

产品文件变化由冻结清单记录。与 WP-01C 相比，新增 16 个 Bundle 应用文件和 44 个测试或 fixture 文件，修改 8 个既有产品文件。锁文件把已锁定的 jsonschema 从开发组移到运行依赖。外部 WP-01C Schema 无删改或新增。本地存储在私有临时目录初始化 SQLite，独立验收未连接生产或未知数据库。[产品改动清单](../primary_freeze_844ed4ac1275/snapshot.json) · [无 dev 安装命令](runtime_deps_xcl5xmpf/commands.json)

首轮冻结在跨目录 Schema Index 的诊断发生失败。低层拒绝后返回 `schema_reference_source_missing`，预期为 `schema_reference_index_mismatch`。整改让 Schema Index 和语义入口绑定确切 ArtifactCatalogue，增加无引用的索引错配和闭包错配回归。首轮 runner 同时将生成目录 348 个文件误记作 348 份 Schema，第二轮修正为 347 份 Schema 和 1 份索引，并保留对全部 348 文件的比较。两轮原始记录可追溯。[首轮记录](FROZEN_ROUND_1.md) · [修复文件](../primary_freeze_844ed4ac1275/snapshot.json)

## 隔离实测

- 合成包有 58 个已编入制品，含 Scene/Region、Region 多边形、精确坐标系和单位词表、静态来源描述符、源文件原始字节、Question 与 Tool 两种 Fact 来源和 mandatory Rule。同一输入在两个临时编译环境以不同制品顺序生成相同 ZIP 和逻辑根。第二个 Bundle 版本更改资源原件，生成不同逻辑根。携带旧授权摘要的改动以 `source_provenance_digest_mismatch` 拒绝。[编译与制品输出](frozen_0535kpib/isolated_runtime.stdout)
- 两版 Bundle 均使用独立生成的临时 Ed25519 密钥签名，在新的主入口调用中独立验签、核对注入的测试来源证明、出具回执和保存原始 ZIP。SQLite 重开后回读的原件与送入字节一致。来源证明注册表是独立合成装置，签名运算和文件存储是本机真实执行。[探针脚本](frozen_0535kpib/probe.py) · [签名及回执摘要](frozen_0535kpib/isolated_runtime.stdout)
- 空信任、缺来源证明、无签同根包、带有效签名但缺声明生产方的包、篡改后的静态资源、未受权激活、无权指定 Release 均拒绝。向临时库注入不可读制品字节后，精确读回和新 Task 接收都拒绝。测试 profile 与生产域不匹配的拒绝覆盖在全量用例中。[独立拒绝输出](frozen_0535kpib/isolated_runtime.stdout) · [基线回归 JUnit](frozen_0535kpib/pytest.xml)
- 两版分别技术准入、注册和获准，A 激活时固定 Task A，切换 B 后新接收 Task B 固定到 B。回滚 A 并重开后，两个原 Task、两个历史 Release、批准和激活记录均保持原值，新 Task 选择 A。测试使用明确的本地角色权限和 `device → region → scene → global` 作用域优先级。生产信任、生产审批和新 Task 策略需要部署侧注入。[切换及回滚输出](frozen_0535kpib/isolated_runtime.stdout)

## 实际命令与证据

Python 3.12.3、uv 0.9.15、pytest 8.4.2、Ruff 0.14.14、mypy 1.18.2、OpenSSL 3.6.1 和 SQLite 3.45.1。pytest 使用 `uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q -p no:cacheprovider --junitxml=<证据目录>/pytest.xml`。同一锁定环境分别执行源和测试的 Ruff 检查及格式检查、`mypy src`、两次独立导出、仓库 Schema 比对和隔离探针。每条命令的实际参数、UTC 起止、退出码、标准输出、标准错误和文件比对均单独保存，所有验收命令退出 0。[逐条命令](frozen_0535kpib/commands.json) · [依赖版本](frozen_0535kpib/dependency_versions.stdout) · [OpenSSL](frozen_0535kpib/openssl_version.stdout)

WP-03 可由主控按已授权范围检查前置并另行开始，验收 PostgreSQL 持久工作流、租约与故障恢复。本次隔离纵链未运行六节点、Provider 或 GPU，未在业务数据上测量准确率。
