# WP-03 历史激活查询

**局部验收 PASS · TEST_PASSED · RUNTIME_NOT_RUN**

`ReleaseManager.get_task_activation(task)` 供已授权的宿主读取实际接受的 Task 的历史激活。接口重用接入的两个 scope 内索引，校验两者一致且指向完整匹配的 Task，再核对精确激活身份、时间、Release、Bundle 及已有的 scope 审批。设备和区域 scope 同时核对 Task 中的受信输入。场景 scope 使用历史激活保存的完整词表成员引用。查询无匹配证据时返回 `None`。SQLite 存储错误仍由宿主处理。

测试通过真实 ReleaseManager 操作激活两个 Release、分别接受 Task，回退并重开 ledger，核对历史查询返回最初各自的激活。测试替换读取端口以验证缺失或冲突的索引、错误场景词表摘要、错误 Bundle 版本和伪造 Task 均无结果，并断言查询从未访问当前激活指针。

变更限于 ReleaseManager 和两个测试文件。Domain 模型、Schema 和历史 ledger 行未修改，无迁移。代码回退可移除新接口及测试，历史 pin 保留。Worker 消费者由主控联验。

本地运行环境为 Linux x86_64 6.8.0-149-generic，glibc 2.39，CPython 3.12.3，SQLite 3.45.1，Pydantic 2.12.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。无跳过。合并验证 7 项历史激活测试和 13 项现有 WP-02 Release 回归测试。

```text
uv run --locked --no-sync python -m pytest tests/contract/test_wp03_historical_activation.py tests/negative/test_wp03_historical_activation_invalid.py tests/unit/test_wp02_releases.py tests/contract/test_wp02_releases_contract.py tests/negative/test_wp02_releases_rejection.py -q --junitxml=planning/implementation_evidence/WP-03/historical_activation/pytest.xml
exit 0
....................                                                     [100%]
- generated xml file: /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/planning/implementation_evidence/WP-03/historical_activation/pytest.xml -
20 passed in 3.64s
```

```text
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/releases.py tests/contract/test_wp03_historical_activation.py tests/negative/test_wp03_historical_activation_invalid.py
exit 0
All checks passed!
```

```text
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/releases.py tests/contract/test_wp03_historical_activation.py tests/negative/test_wp03_historical_activation_invalid.py
exit 0
3 files already formatted
```

```text
uv run --locked --no-sync mypy src/jimu_runtime/bundle/releases.py
exit 0
Success: no issues found in 1 source file
```

[JUnit 结果](pytest.xml) · [文件摘要](sha256sum.txt)
