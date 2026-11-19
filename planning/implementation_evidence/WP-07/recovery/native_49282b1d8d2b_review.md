固定 manifest SHA 能验证包内字节，却不能证明各账本属于同一部署、写者持续停止，或执行恢复的连接就是已核验的目标；这些缺口会使不一致的恢复获得成功标记，或使写入发生在授权范围之外。STATIC_REVIEW_ONLY：结论仅基于所给 diff 和项目指令；本地沙箱未能启动，冻结基线未独立核验，未调用外部服务，TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P1] 发布备份前核对 Execution 固定的 Release 与 Bundle — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:113-113
  `ledger_versions()` 对 Bundle 库只检查版本和三张表是否存在。若 `--bundle-db` 指向另一部署的合法账本，PostgreSQL 行摘要和 SQLite 文件摘要仍会各自通过，恢复也能发布 `restore-verified.json`，但 Execution 引用的 Release 可能不存在，或对应另一份 Bundle。应在同一冻结窗口内逐一核对 PostgreSQL 引用的 Release ID、Bundle digest 与账本内容；仅固定清单 SHA 不能证明这些输入原本就属于同一部署。见 [AGENTS.md:87](AGENTS.md#L87)。

- [P1] 将资源账本身份与独立受信配置比对 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:156-160
  `inventory_id` 和 `inventory_sha256` 均取自待备份的资源库，`host_config_sha256` 则直接取自调用参数，两者之间没有核对。误选另一个结构合法、已停写的资源库时，恢复只是把该库的身份与它自己生成的清单比较，错误主机的租约也会被标记为已验证。应从已固定的主机配置取得预期 inventory 身份并在打包前比对，而不是仅记录实测值。

- [P1] 不要把 PostgreSQL 会话抽查当作持续停写证明 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:211-216
  `pg_stat_activity` 只能证明查询当下没有其他会话，而 CONNECT 检查明确排除了 owner 和 superuser。另一个使用 owner 的维护进程可以在两次检查间连接、提交并退出；尤其是恢复验证事务释放表锁后，到发布成功标记前仍可写入。应要求覆盖全部写入入口、持续到发布完成的外部停写措施，或明确把这一点列为未由代码验证的前提。

- [P1] 确保执行 pg_restore 的连接指向已验明的目标库 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:134-134
  在 Docker 模式下，psycopg 使用调用进程的 `127.0.0.1`，而 `pg_restore` 使用 Docker daemon 主机网络中的 `127.0.0.1`。若调用进程处于另一个网络命名空间且两边相同端口、库名和凭据均可用，空库及逻辑 ID 检查针对 A，实际写入却发生在未经检查的 B；事后再检查 A 无法撤销这次写入。应拒绝无法证明两者共享端点的运行方式，或在实际写入连接内完成身份门禁。授权范围仅覆盖隔离环境，见 [AGENTS.md:41](AGENTS.md#L41)。

- [P1] 备份时不要在源介质卷上创建锁文件 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/filesystem.py:183-185
  `freeze_media()` 以 `O_CREAT | O_RDWR` 打开全部 256 个分片。只要有一个锁文件尚未预置，备份就会改变源目录；即使后续因 SQLite 忙而失败，已创建的文件也会留下。在真正的只读挂载上，这条路径还会把无法写入误报为 writer 未停。应将锁文件预置与备份分开，备份只打开既有锁，缺失时不修改源卷。

- [P1] 按运行时权限契约验证目标角色的有效授权 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:296-298
  这里仅验证 schema 的 `USAGE`、`tasks` 的 `SELECT` 和没有 schema `CREATE`。例如，目标 app 角色额外继承了对 `execution_finalizations` 的 `DELETE`，这三个条件仍可全部成立；恢复报告会通过，一旦重新开放 CONNECT，该角色即可删除历史决策。应检查关键表上实际生效的必需及禁止权限，包括角色继承带来的授权。见 [AGENTS.md:137](AGENTS.md#L137)。

- [P1] 使用 --no-owner 前验证具备执行权限的对象归属 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:258-260
  `tables()` 只检查部分 relation 的 owner，但 dump 包含整个 workflow schema。若其中有由较低权限角色拥有的 `SECURITY DEFINER` 函数，`--no-owner` 会使它在目标端归执行恢复的数据库 owner 所有；即使表行摘要完全相同，函数调用者获得的有效权限也已改变。应在备份前拒绝这类不支持的所有者差异，或保留并验证受支持的所有权映射。

- [P1] 将输出写入绑定到受保护的目录而非反复解析路径 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/filesystem.py:54-56
  `create_directory()` 只确认父路径在检查时不含符号链接，并未确认父目录不允许其他用户重命名其子项。在可被他人改名的共享父目录下，新目录即使是 `0700`，后续按路径进行的 `new_private()` 和 `publish_marker()` 仍可能写到已被替换的位置；`O_NOFOLLOW` 只保护最后一级。应拒绝不受信的可写祖先，或持有经过验证的目录描述符并相对它创建全部输出。

- [P1] 不要以父 PID 已退出代替进程树已回收 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/filesystem.py:253-259
  对于明确记录为 `tree_reaped = 0` 的租约，当前分支只要 `process_exited(...)` 返回真就继续备份。父进程退出而子进程仍运行时，父 PID 的退出不是整棵进程树停写的凭据。对已绑定进程但尚无回收证明的租约应拒绝备份，或要求独立的进程树清空证明；这不要求把尚未绑定 PID 的预留租约一并拒绝。

- [P1] 超时后确认 Docker 内的恢复进程已经终止 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:157-161
  镜像模式下，`subprocess.run()` 超时终止的是本地 Docker CLI，不保证 daemon 管理的 `pg_restore` 也已退出；`--rm` 仅在容器退出后清理。若归档已传完而数据库操作仍在等待，调用方可能收到失败后，目标库仍继续写入甚至提交。应跟踪本次容器并在超时后确认容器和恢复会话已终止，否则应将目标状态报告为未知而非已停止。

- [P2] 为数据库已提交但整体验证失败提供恢复路径 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:263-266
  `--single-transaction` 的提交发生在 `pg_restore` 返回前，而行摘要和介质关联检查位于随后另开的 `target.transaction()`。若后验检查失败，目标库已经非空、文件根没有成功标记，按同一接口重试又会被空库门禁拒绝。应使提交受验证结果约束，或为已提交的隔离目标提供按原 manifest 重新验证的受控路径，而不是要求调用者自行清库。见 [AGENTS.md:134](AGENTS.md#L134)。

- [P2] 避免原始文件复制提前释放 SQLite 的冻结锁 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:129-130
  在 Linux 默认 SQLite Unix VFS 上，传统 POSIX 文件锁属于进程；同一进程关闭指向同一 inode 的另一个描述符，会释放该进程在此文件上的锁。`file_witness()` 恰好另行打开并关闭仍由 `ledger()` 持有的数据库文件，因此两次复制返回后，虽然 `BEGIN EXCLUSIVE` 的上下文尚未退出，其他进程已可能写入源账本。已复制的字节不会因此倒改，但后续 PostgreSQL dump 期间账本仍被冻结的保证不成立。应避免在持锁进程中另开再关闭同一数据库文件。

- [P2] 防止 SQLite 在只读备份检查期间自动恢复源库 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/filesystem.py:203-205
  检查 `-journal` 是否存在与 SQLite 实际取锁不是原子操作。若写者在二者之间留下 hot journal，随后用 `mode=rw` 进行的首次读取可以触发 SQLite 自动回滚并修改源库，而不是返回预期的 `sqlite_recovery_required`。若源库必须保持只读，应先取得独立写入隔离再只读打开，或使用不可写快照，把 journal 恢复留给单独授权的流程。

- [P2] 拒绝未纳入 schema dump 的额外数据库对象 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:255-260
  `isolated_schema()` 对 `public` 只枚举 relation、函数和类型等部分对象。例如，单独存在的自定义 `public` collation 不会命中任何检查：源端会接受它，但限定 `--schema=jimu_workflow` 的 dump 不会保存它；目标端有同类对象也会被判为空库。应按允许的已预置对象验证源库和目标库，不能将这几类目录均为空等同于数据库隔离。

- [P2] 在交付 pgpass 前验证本机 PostgreSQL 工具路径 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/postgres.py:120-122
  `--pg-bin` 分支把文件名拼成绝对路径就执行，未验证二进制及其祖先目录是否可被其他用户替换，而且连 `--version` 调用也携带 `PGPASSFILE`。若工具安装在共享可写目录，显式选择目录并不等于授权随后替换进去的程序读取 owner 凭据。应在首次执行前验证工具来源和路径权限，版本探测无需提供凭据。

- [P2] 在发布前执行与恢复端相同的清单大小限制 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:165-169
  `read_manifest()` 拒绝超过 16 MiB 的清单，但 `backup()` 对每个介质文件写入一条 witness 后不检查编码长度，仍会发布并返回 SHA。介质数量足够多时，未损坏、未篡改的备份也必然在恢复入口被拒绝。应让生产端和消费端共用同一上限，至少在发布成功标记之前拒绝超限清单。

- [P2] 按已固定的文件大小限制恢复写入量 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/recovery/__init__.py:230-236
  清单已经给出每个文件的可信 `size`，但恢复会先将实际文件完整写入目标卷，再比较 witness。若包内文件在备份后异常增长，写入量可以远超获准清单，甚至先耗尽隔离卷空间才报告摘要不符。应对已打开的源描述符预检大小，并在流式复制时以清单大小为硬上限，同时保留最终 SHA 校验。