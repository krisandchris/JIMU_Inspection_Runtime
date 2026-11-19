# WP-02 B2.3 编译入口验证

本批交付 `bundle/compiler.py`、`tests/fixtures/wp02/compiler/scenario.py` 和三个 `test_wp02_compiler*.py` 文件。批准范围是本地编译、附签引用及合成验证。现有 Domain Wire Schema、数据库和部署配置无需迁移。

编译接收有限逻辑制品及原始资源字节、固定构建元数据和显式信任环境。每份输入通过独立的 `SourceIdentity` 对应唯一获准的外部来源条目，编译在处理内容引用前核对原始字节 SHA-256。外部来源身份允许独立于当前 Bundle 的制品 kind、ID 和 version。每个编译期引用位点用 JSON 路径和唯一目标身份声明。已有的完整引用保留原值并经闭包核对。编译从 Fact 类型生成并去重 child Schema，读取实际配置派生 ProducerCatalogue，输出 ZIP 后重新核验全包字节与语义。编译报告保留能力检查和语义诊断。附签操作添加上游签名证据的不可变引用，保留覆盖字段的原始值和逻辑根。独立准入验证签名原件、授权、撤销和门限。

## TEST_PASSED

Python 3.12.3，Linux 6.8.0-149-generic x86_64，glibc 2.39，uv 0.9.15，Pydantic 2.12.5，RFC 8785 0.1.4，jsonschema 4.25.1，pytest 8.4.2，mypy 1.18.2，Ruff 0.14.14。

```sh
uv run --locked --no-sync python -m pytest tests/unit/test_wp02_compiler.py tests/contract/test_wp02_compiler_packaging.py tests/negative/test_wp02_compiler_invalid.py -q
uv run --locked --no-sync mypy --follow-imports=silent src/jimu_runtime/bundle/compiler.py
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/compiler.py tests/fixtures/wp02/compiler tests/unit/test_wp02_compiler.py tests/contract/test_wp02_compiler_packaging.py tests/negative/test_wp02_compiler_invalid.py
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/compiler.py tests/fixtures/wp02/compiler tests/unit/test_wp02_compiler.py tests/contract/test_wp02_compiler_packaging.py tests/negative/test_wp02_compiler_invalid.py
```

所有命令退出码为 0。pytest 报告 `20 passed in 7.25s`，无跳过。mypy 报告 `Success: no issues found in 1 source file`。Ruff 报告 `All checks passed!` 和 `5 files already formatted`。

覆盖固定构建与可重现性、内容变更传递、资源原始字节、独立根重算、聚合 locator、重复附签引用与附签前后的覆盖字段、源引用缺项和成环、预 pin 摘要不匹配、重复身份、Schema 路径歧义、F04 生成错误、Tool DAG 环、无生产者的 Rule、未满足能力、未授权来源、来源字节摘要不匹配，以及来源条目缺失、孤立和复用。

在独立临时目录重复编译的命令如下。

```sh
uv run --locked --no-sync python - <<'PY'
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.compiler import compile_bundle
from tests.fixtures.wp02.compiler.scenario import LIMITS, compile_environment, source_scenario
runs=[]
for _ in range(2):
    with TemporaryDirectory() as tmp:
        report=compile_bundle(source_scenario(), limits=LIMITS, environment=compile_environment(Path(tmp)))
        assert report.bundle is not None, report.errors
        bundle=report.bundle
        checked=read_bundle(bundle.archive_bytes, LIMITS)
        print(f'{bundle.bundle_ref.bundle_digest} {sha256(bundle.archive_bytes).hexdigest()} {len(checked.manifest.artifacts)} {len(checked.payloads)} {len(report.semantic_checks)}')
        runs.append(bundle)
assert runs[0] == runs[1]
PY
```

退出码为 0，两次均有 54 个制品、39 个载荷文件、30 项语义检查。逻辑根 `01342a6f02f3b1eec9c794c5ab0f2cf8ab7de3d871f7686715e0c074baca7f88`，ZIP SHA-256 `100c5fcb2a889d3949405d5a9b9b90acdce038852981ca40714fca43eb428e55`。

## 编译与独立准入联测

```sh
uv run --locked --no-sync python -m pytest tests/unit/test_wp02_compiler.py tests/contract/test_wp02_compiler_packaging.py tests/negative/test_wp02_compiler_invalid.py tests/unit/test_wp02_admission_flow.py tests/contract/test_wp02_admission_receipt.py tests/negative/test_wp02_admission_rejections.py -q
```

退出码为 0，`37 passed in 29.79s`，无跳过。联测包括真实的本地隔离签名及独立准入信任校验。

## RUNTIME_NOT_RUN

场景仅含合成业务条件、注册能力、模型协议和资源预算。未接入真实信任域、硬件、模型或 Tool。B2.3 的工程验收为 PASS。跨编译、独立准入和 Release 的交接由 WP-02 总体验收覆盖。
