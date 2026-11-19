# WP-02 B2.5 准入验证

本地入口 `jimu_runtime.bundle.admission.admit_bundle` 接收 ZIP 原字节、唯一操作号、已认证提交者、部署预算、可信运行身份与验证环境，以及本地持久化仓库。每次调用独立核对存档、清单、摘要、制品闭包、语义、能力和外部 detached 签名。准入使用既有 C `AdmissionReceipt`。没有可验证逻辑根的拒绝保留原 ZIP SHA-256、字节数和诊断路径。成功结果可在重新打开仓库后，按收据读取与复核原字节。

准入同时绑定操作号、提交者、原 ZIP 摘要、验证时间和实际核验结果。相同操作号在验证环境或结果发生变化后参与新验证并拒绝复用。历史结果通过 `LocalBundleStore.get_admission` 与 `get_receipt` 查询，历史原包通过 `read_archive` 读取。Release 可直接使用收据、原 ZIP 摘要和 `verification.evidence` 中的能力、信任策略、信任快照及签名引用。

测试使用完整合成非机动车场景、临时 SQLite 仓库与隔离测试域的 OpenSSL Ed25519。覆盖签名准入、重开、同逻辑根不同 ZIP、无签 ZIP、签发者撤销、验证时间、独立证据缺失、破损清单、归档预算、载荷篡改、可通过哈希和签名核验的闭包及 Producer 错误、能力撤销、同一身份更换逻辑根和已生成 C Schema 的验证。

```bash
uv run --locked --no-sync python -m pytest tests/unit/test_wp02_admission_flow.py tests/contract/test_wp02_admission_receipt.py tests/negative/test_wp02_admission_rejections.py -q --junitxml=planning/implementation_evidence/WP-02/admission/junit.xml
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/admission.py tests/{unit,contract,negative}/test_wp02_admission*.py tests/fixtures/wp02/admission
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/admission.py tests/{unit,contract,negative}/test_wp02_admission*.py tests/fixtures/wp02/admission
uv run --locked --no-sync mypy src/jimu_runtime/bundle/admission.py
```

验证结果为 `TEST_PASSED`。17 个测试在 19.71 秒内通过，退出码为 0，无跳过。Ruff lint、Ruff format 和 mypy 均退出 0。测试环境的版本和输出见 [checks.txt](checks.txt)。测试终端摘要和用例级结果见 [pytest.txt](pytest.txt) 与 [JUnit](junit.xml)。

生产运行 `RUNTIME_NOT_RUN`。生产签名策略、信任根、外部身份服务和生产数据库验证留待对应部署工作。既有 C Schema 和历史发布记录无需迁移。
