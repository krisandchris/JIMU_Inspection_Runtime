# WP-07 隔离备份与恢复

工具面向获准的本地 PostgreSQL 16 测试部署。备份包含 PostgreSQL custom archive、Bundle 和资源 SQLite 账本、私有媒体文件及 `manifest.json`。连接凭证通过独立私有 pgpass 使用。宿主预置 PostgreSQL 实例和角色。

## 外部维护前置

停止受理、Worker、清理器及有权直接改写数据库或卷的维护进程。撤销普通登录角色对指定数据库的有效 CONNECT 权限并关闭已有连接。通过宿主管理面持续隔离其他 owner 和 superuser 连接及卷写入入口，直到工具结束并发布标记。单次 PostgreSQL 会话检查无法独立证明特权写者始终停止。`--confirm-isolated-maintenance` 记录操作者已完成这些外部措施。

以独立可信的宿主配置生成私有 JSON 描述符。描述符包含 `schema_version: "1.0.0"`、实际部署配置摘要 `deployment_config_sha256`、该宿主唯一的 `inventory_id` 和由预期 HostInventory 得到的 `inventory_sha256`。备份和恢复以该 JSON 原始字节的独立可信 SHA-256 核验配置及资源账本。pgpass 和配置描述符存放在封包及恢复目录之外。

## 备份

维护前单独预置媒体锁文件。此操作只为受控媒体卷创建固定锁名，备份只以只读方式打开既有文件。

```sh
python -m ops.recovery prepare-media-locks --media-root "$MEDIA_ROOT"

python -m ops.recovery backup \
  --database "$SOURCE_DATABASE" --port "$SOURCE_PORT" --logical-id "$LOGICAL_UUID" \
  --pgpass "$SOURCE_PGPASS" --postgres-image "$PINNED_POSTGRES_IMAGE_ID" \
  --host-config "$HOST_BINDING" --host-config-sha256 "$HOST_BINDING_SHA256" \
  --confirm-isolated-maintenance \
  --bundle-db "$BUNDLE_DB" --resource-db "$RESOURCE_DB" --media-root "$MEDIA_ROOT" \
  --package "$NEW_PACKAGE_DIRECTORY"
```

备份同时持有媒体分片锁、两份 SQLite 只读事务及 PostgreSQL 表 SHARE 锁，将同一 PostgreSQL 快照传给 `pg_dump`。SQLite 只读连接不会恢复源 journal，遇到 journal 或 WAL 时失败。绑定 PID 且缺少进程树回收证明的 lease 阻断备份。无绑定进程的预留 lease 和未决派发保留账本状态。工具核对 PostgreSQL 的 Task/Execution 固定 Release 与 Bundle、已提交媒体以及应用角色的有效授权。PostgreSQL 针对严格限定对象的整个隔离数据库执行 dump，保留业务 Schema 和运行时默认函数权限。

通过独立可信渠道保存返回的 `manifest_sha256`。`--pg-bin` 可指定受保护的宿主 PostgreSQL 16 工具目录。Docker 模式使用本地镜像 ID，并在交付只读 pgpass 挂载前证明本进程与容器共享同一 boot 和网络命名空间。版本探测独立于凭证。pgpass 需为当前用户所有、权限 0600 的普通文件，匹配回环地址、端口、数据库和 `jimu_workflow_owner`。

## 恢复

预置不同的隔离 PostgreSQL 16 实例和空数据库。目标按原逻辑 UUID 设置 `jimu-workflow-instance:<UUID>` comment，使用受限的 `jimu_workflow_owner` 与 `jimu_workflow_app`，保持外部维护隔离。恢复目录须尚不存在，目标的所有祖先目录须防止其他用户替换路径。

```sh
python -m ops.recovery restore \
  --database "$TARGET_DATABASE" --port "$TARGET_PORT" --logical-id "$LOGICAL_UUID" \
  --pgpass "$TARGET_PGPASS" --postgres-image "$PINNED_POSTGRES_IMAGE_ID" \
  --host-config "$HOST_BINDING" --host-config-sha256 "$HOST_BINDING_SHA256" \
  --confirm-isolated-maintenance --manifest-sha256 "$TRUSTED_MANIFEST_SHA256" \
  --package "$BACKUP_DIRECTORY" --destination "$NEW_TARGET_DIRECTORY"
```

工具按清单大小上限与摘要复制文件，校验媒体身份和 SQLite，再写入 `restore-pending.json`。PostgreSQL 在单个恢复事务中提交。复查 PostgreSQL 表逐行摘要、权限、固定 Release 与 Bundle、媒体证据之后，无覆盖地发布 `restore-verified.json`。目录可直接作为 Bundle ledger 和 LocalMediaStore 的根，资源账本位于 `resource.sqlite3`。

超时会尝试停止本次明确标识的 Docker 进程。恢复连接中断时可能已提交，继续保持目标隔离。将原命令的 `restore` 换为 `verify`，可以按原清单和同一挂起标记重新验证，不重新执行 SQL。完整复核仍无法通过时保留原目标并在另一套获准的空目标重试。通过业务读回后，由宿主恢复应用 CONNECT。

局部演练包含实际 PostgreSQL dump/restore 和数据库提交后的失败注入。恢复后通过应用查询原 Decision 和 Release，并读回实体 PNG 和资源租约。Decision 使用合成测试模型，PNG 独立注册于同一 Scope。WP-06 回放与真实模型的组合验证另行记录。
