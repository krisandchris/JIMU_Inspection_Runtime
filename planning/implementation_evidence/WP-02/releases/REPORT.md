# WP-02 B2.6 Release 与 Task 版本固定

**局部验收 PASS · TEST_PASSED · RUNTIME_NOT_RUN**

本批在已交接的 WP-01C 基线上交付本地 Release 管理。产品文件为 `src/jimu_runtime/bundle/releases.py`，另有三个定向测试文件和独立身份 fixture。沿用 Stage C 的 `ReleaseRecord`、`ApprovalReceipt`、`ActivationRecord`、`TaskSubmission` 和 `AcceptedTask`，冻结 Domain 和生成 Schema 未改。产品与测试摘要见 [sha256sum](sha256sum.txt)。

## 合同与实现决定

- 技术登记从 B2.5 的受信本地 ledger 查阅真实成功回执和通过摘要复核的原始 ZIP。业务审批为每个精确 scope 写入追加快照，激活使用该 scope 的审批。原始技术登记快照可单独读取。
- 宿主注入认证后的 principal 解析、action/target 授权、可信设备/相机/区域/场景解析、完整无重复的 scope 优先级、UTC 时钟和新 Task 使用策略。测试用独立注册、审批、控制和 Task 角色。scene 匹配完整词表成员引用。
- 激活按预期前驱执行原子 CAS，回退引用同一 scope 的历史激活。成功操作的参数摘要、actor、scope、动作和回退来源随操作结果一同保留。完全相同的重试返回先前结果。操作键复用不同参数、重复 Release 身份和陈旧前驱拒绝。注入写入故障后没有孤立激活。
- Task 接受从显式优先级选出精确当前 Release，然后保存 Release ID 和 Bundle 根。显式 requested_release_id 必须是同一选中 Release，并通过附加授权。idempotency key 与 request ID 在 project/client 范围都受约束。注入策略拒绝当前 Release 时，本入口拒绝新的 Task，并保留历史 pin。
- SQLite 原子操作限于同一私有本地目录。历史读取接口供可信宿主调用。生产 principal 权限、生产 scope 优先级和撤销处置决策仍待外部确认。本批没有部署、数据迁移或持久工作流调度。源码回退可移除新增入口和测试，已有本地证据留存。

独立临时目录分别编译两个非机动车场景 Bundle 版本。隔离 Ed25519 签名后，以原始字节独立准入。验证两次激活、分别固定 Task、回退及重开后逐一读取原始 ZIP、历史批准、激活和 Task。并发定向测试覆盖同操作重复及两个操作竞争同一前驱。新旧 Task 选择、完整词表引用、显式 Release 请求、跨 scope 回退及假身份的反例也已执行。

## 本地执行

Linux x86_64 6.8.0-149-generic，glibc 2.39，CPython 3.12.3，SQLite 3.45.1，uv 0.9.15，Pydantic 2.12.5，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2，OpenSSL 3.6.1。全部在锁定 CPU 隔离环境执行，定向测试无跳过。

```text
uv run --locked --no-sync python -m pytest tests/unit/test_wp02_releases.py tests/contract/test_wp02_releases_contract.py tests/negative/test_wp02_releases_rejection.py -q --junitxml=planning/implementation_evidence/WP-02/releases/pytest.xml
exit 0
.............                                                            [100%]
- generated xml file: /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/planning/implementation_evidence/WP-02/releases/pytest.xml -
13 passed in 3.25s
```

```text
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/releases.py tests/fixtures/wp02/releases/support.py tests/unit/test_wp02_releases.py tests/contract/test_wp02_releases_contract.py tests/negative/test_wp02_releases_rejection.py
exit 0
All checks passed!
```

```text
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/releases.py tests/fixtures/wp02/releases/support.py tests/unit/test_wp02_releases.py tests/contract/test_wp02_releases_contract.py tests/negative/test_wp02_releases_rejection.py
exit 0
5 files already formatted
```

```text
uv run --locked --no-sync mypy src/jimu_runtime/bundle/releases.py
exit 0
Success: no issues found in 1 source file
```

[JUnit 结果](pytest.xml)

下一个工程门为 WP-02 独立整包验收。进入 WP-03 仍须按主控工作包门核对。
