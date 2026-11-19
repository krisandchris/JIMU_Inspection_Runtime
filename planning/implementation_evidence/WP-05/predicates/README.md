# WP-05 单谓词求值

技术复核范围为从已准入 Catalogue 准备 ConditionExpr、在明确的 Execution/Profile/Object 与输入域内求值、生成现有 PredicateRecord。调用方提供已验证的 Context 投影和本次选择的 Facts。Required Evidence Guard、Decision 发布与 Worker 接入独立验收。

使用已核对的 WP-02 产品清单 844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556 及 668 个原件。7 个新增文件在单独目录开发。主控使用 uv 0.9.15、CPython 3.12.12 和锁定的离线环境复跑。用例覆盖 10 个操作符、三值逻辑、非 known Fact、零和 false、单位、枚举身份、Record 字段、作用域及从真实本地签名 Bundle 重开求值。

| 记录 | 结果 |
|---|---|
| [原始差异](native_cf0b4f2e6d3a.patch)与[原生审查](native_cf0b4f2e6d3a_review.md) | 原生审查读取限定 diff，指出非法 Context 容器和构造不完整的 KnownFact 会抛出意外异常。审查进程的工作树读取受沙箱限制。主控另行读取源码和封存原件。 |
| [第一次复测](primary_predicates.log) | 58 项通过，7 个文件格式通过，4 个产品源文件 strict mypy 通过。 |
| [整改差异](remediation.patch) | 入口检查 Mapping；在读取 KnownFact 和嵌套容器前检查字段存在，维持先做预算检查再递归校验的顺序。 |
| [最终复测](primary_after_review.log)与[JUnit](primary_after_review.xml) | 主控复跑全部 63 项新增测试通过，0 个跳过。Ruff、7 个文件格式及 4 个产品文件 strict mypy 通过。 |
| [七文件清单](91a208f70e75_sha256.json)与[已核对的交接包](91a208f70e75_sources.zip) | 清单 SHA-256 91a208f70e757f9c00498545f56bc39ad5969a5ac81d09dcf616a7932af35a96。 |

主控使用 `uv run --locked --offline pytest -q tests/unit/test_wp05_predicates*.py tests/negative/test_wp05_predicates_rejections.py`，以及相同锁环境下的 `ruff check`、`ruff format --check` 和 `mypy --strict src/jimu_runtime/inspection`。整改后四项命令退出码均为 0。独立切片为 TEST_PASSED。场景 Worker 和真实模型验收另行留证。
