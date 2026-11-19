# WP-07 恢复原生复核处置

对应 `native_49282b1d8d2b_review.md`。以下验收范围是隔离 PostgreSQL 16.14 和临时卷。

| 发现 | 处置与验证 |
|---|---|
| 错用其他部署的 Bundle 账本 | 同一冻结窗口按 PG 中所有 Task/Execution 的 Release ID 和逻辑 Bundle digest 检查 Release、准入收据及原始归档。真实隔离演练对其他合法空账本拒绝备份。 |
| 错用其他资源账本 | 校验独立可信的宿主描述符原始摘要和 HostInventory 身份。真实演练拒绝相同结构的其他库存。 |
| 特权写者持续停写 | 将外部维护隔离作为显式操作前置。工具验证普通 CONNECT、已知会话、prepared transactions、本地锁和 SQL 表锁。特权入口持续隔离由宿主管理面保证。 |
| 实际 Docker 恢复端点 | 挂载 pgpass 前证明本机与容器共享 Linux boot ID 和网络命名空间。试验包括同宿主真实操作和不相符预检的拒绝。 |
| 备份在媒体源创建锁 | 预置独立命令，备份只读打开。真实测试对缺失分片拒绝，目录未改变。 |
| app 有效权限不足以核对 | 验证角色继承、数据库及 Schema 权限、表与列的允许和禁止权限，含 PUBLIC 有效授权及迁移默认 ACL。真实测试拒绝继承的 DELETE。 |
| --no-owner 改变执行对象身份 | 检查 workflow 对象 owner，同时拒绝 SECURITY DEFINER。真实测试拒绝具有该属性的函数。 |
| 可替换的输出祖先 | 校验目录路径及其所有者写权限，保留受保护的 sticky 根目录特例。测试拒绝由其他用户可改名的目录。 |
| 父 PID 退出无法证明树回收 | 绑定 PID 而缺少 tree_reaped 时始终阻断。真实资源账本中不存在的父 PID 也不会通过。 |
| Docker CLI 超时但实际恢复未决 | 为本次容器分配唯一名字及标签，清理操作只针对精确拥有的容器。工具对可能已发送 COMMIT 的失败返回未知结果。超时测试注入验证清理范围和状态码。 |
| 提交后验证中断无法重验 | 可信原清单与目标挂起标记绑定，`verify` 重验已隔离数据库和文件，无 SQL 重放。真实恢复事务提交后注入停止，随后手工重验通过。 |
| SQLite 附加 FD 提前释放锁 | FD 在 SQLite 只读事务前打开、在其关闭后释放。单独 SQLite 进程在复制完成后仍无法提交写入。 |
| 源 SQLite 热 journal 被自动回滚 | 仅以 mode=ro 打开并拒绝 journal/WAL。真实未提交事务的演练拒绝备份。 |
| schema 过滤漏自定义对象 | 使用严格准入的隔离数据库全量 dump，保留已知默认函数 ACL。以 PG 对象依赖及目录检查拒绝不支持的对象，真实 public collation 负例覆盖。 |
| 本机工具路径与版本探测泄露凭证 | 检查二进制及祖先的所有者和写权限，版本探测独立于 pgpass。测试覆盖可重命名路径和无凭证探测。 |
| 封包清单过大 | 生产和消费同一大小上限。实际封包降低上限的故障注入确认未发布超限清单。 |
| 拷贝写到超出清单大小 | 打开源 FD 后检查大小，流式复制前逐块限制累计写入，并验证最终摘要。测试对过大输入未创建目标文件。 |

## 实际验证

- `PYTHONPATH=src:. ../JIMU_WP05_media_kf6e11wm/.venv/bin/python -m pytest -xq --tb=short tests/contract/test_wp07_recovery.py`，退出码 0，6 passed in 21.32s。
- `MYPYPATH=src:. PYTHONPATH=src:. ../JIMU_WP05_media_kf6e11wm/.venv/bin/python -m mypy --strict --explicit-package-bases ops/recovery`，退出码 0，5 个源码文件通过。
- Ruff lint 和格式核对通过。PostgreSQL 工具来自已在本地主机缓存的 PostgreSQL 16.14 固定镜像。
- 容器超时用受控注入覆盖，实际数据库提交后恢复用真实 PostgreSQL 进程覆盖。部署在维护期间对其他特权写者的持续隔离需要独立的宿主管理保证。
