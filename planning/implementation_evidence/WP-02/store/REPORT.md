# WP-02 B2.5 本地存储工程证据

## 范围及改动

在本地隔离目录交付可重开的 Bundle 存储。代码位于 `src/jimu_runtime/bundle/store.py`。合成验证端口 fixture 位于 `tests/fixtures/wp02/store/support.py`，局部测试位于三个 `test_wp02_store*.py` 文件。此批次的回退单位是上述新文件。测试中的持久目录均由 pytest 临时创建。

`StoredAdmission` 使用 `schema_version=1.0.0`，关联操作键、操作者、参数摘要、整包 SHA-256、包字节数及验证结果。结果通过 `kind` 区分可识别清单的既有 `AdmissionReceipt` 和未识别输入的诊断。存储有可信验证端口返回的能力、策略、信任快照及实际验证的签名引用。存储模型支持从 Pydantic 直接生成 JSON Schema，局部测试验证了持久化往返。既有领域回执、Release 和 Activation wire 合同保持原版本。

## 持久化与安全

- 接受结果的 ZIP 原件、逻辑身份绑定、权威回执在一个 SQLite 事务提交。回执插入前读取暂存原件并校验字节数与 SHA-256。重开按操作键或回执 ID 查询也验证接受包的字节和逻辑根绑定。
- SQLite 使用独占 Runtime 所有者目录内的 DELETE rollback journal、`synchronous=EXTRA` 和 `BEGIN IMMEDIATE` 写事务。初始化同步目录项。中断只留下 SQLite 恢复所用的日志，重开由 SQLite 回滚；同一接受 ZIP 可按实际字节摘要共享副本。ZIP 字节数受调用方准入预算和 SQLite BLOB 上限约束。
- 相同操作键按原件 SHA-256、操作者及参数摘要核对，参数不同时拒绝复用。新的操作键重新调用受信验证器，即使 ZIP 已保存。接受身份的 ID/version 换根会生成有诊断的拒绝记录。未接受的 ZIP 原件不会进入存储。
- 受信进程负责构造 store、调用私有准入写接口及授权 Release 写事务。想提交伪造回执的调用者必须越过该进程边界。进程内的组件和 Runtime 所有用户均在同一受信区；生产 API 仍须在入口鉴权。供 Release 控制器使用的账本支持同一事务内查询权威回执、写历史及比较前驱。
- SQLite 模式版本为 1，未知版本拒绝打开。首次建库无迁移；数据库须位于支持 SQLite 本地锁和同步语义的文件系统，使用可预期的 Linux 所有权。WP-03 的 PostgreSQL 工作流持久化独立交付。

## 验证和交接

`TEST_PASSED` 20 个，跳过 0 个。覆盖重开查询、相同逻辑根独立 ZIP、竞争写入、幂等、模拟签名撤销后的重验、拒绝与原件隔离、回执冲突、篡改检测、Release 前驱比较及锁忙重试。两个子进程故障注入均留下真实 rollback journal，分别在写入暂存字节后、写入回执后退出；重开恢复日志，同时确认原件、身份绑定和回执都无未提交残留。精确命令、版本、退出码、JUnit 和 SHA-256 均随附。

`RUNTIME_NOT_RUN` 真实远端信任、运行时独立准入全链及业务批准未在此批次执行。存储端口的合成验证器用于控制准入结果，真实 Bundle 完整性、验签和 Release 授权由各自集成测试覆盖。存储工程子门 `PASS`。WP-02 总门由主控在整包准入和 Release 接入后判定。
