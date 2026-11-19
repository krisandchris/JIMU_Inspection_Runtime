# WP-02 工程验收

**本地工程验收 PASS。** 冻结的 668 个文件通过独立复核。主控读取 JUnit、检查每步的退出码与快照、核对编译和准入的独立输入，复核了 Release 激活、回滚、Task 固定和拒绝路径。Git 工作树基线为 `2552affeb69fbc2594fe70724f895b0db0967d47`，WP-02 产品清单 SHA-256 为 `844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556`。比 WP-01C 新增 60 个文件、修改 8 个，完整差异见[清单](../../implementation_evidence/WP-02/primary_freeze_844ed4ac1275/snapshot.json)。

| 验收维度 | 实际证据 |
|---|---|
| 代码 | 25 种制品、原始字节和逻辑根、闭包与语义复验、独立外部信任配置、准入回执、不可变存储、作用域审批/激活/回滚及 Task 选版。严格 mypy 检查 100 个源文件；产品范围内的 261 个 Python 文件满足 Ruff 格式。额外对仓库根运行 format 检查只报告 4 个既有历史审计脚本；这些封存文件未改动。 |
| 测试 | `TEST_PASSED`。3191 项通过，0 失败、0 错误、0 跳过。首轮失败保留原记录。整改后主控独立复跑受影响的 42 项测试。两次导出的 347 个 Schema 和索引均与 WP-01C 逐文件相同。 |
| 隔离运行 | `RUNTIME_VERIFIED`。非机动车场景合成包经两次实际编译，使用临时 Ed25519 密钥签名，独立准入后在本地 SQLite 提交，验证切换、回滚、重开与原 Task 版本固定。使用无开发依赖的临时锁定环境复测通过。 |
| 生产和质量 | `RUNTIME_NOT_RUN` / `UNVERIFIED`。真实信任、审批主体、部署、实际巡检与生产质量需各自环境和实测。用户验收与 Git 发布由用户单独决定。 |

[独立验收与命令](../../implementation_evidence/WP-02/independent/FINAL_ACCEPTANCE.md) · [JUnit/逐步结果](../../implementation_evidence/WP-02/independent/frozen_0535kpib/run_result.json) · [主控定向复测](../../implementation_evidence/WP-02/primary_ref_binding_KtBTwY2l/pytest.xml)

本包使用命名的本地测试签名域和外部注入的信任策略，本地存储只操作明确指定的私有目录；不涉及生产数据迁移。WP-01C 的公开 Schema 保持原字节。产品新增的应用接口、必要的语义校验兼容调整和 jsonschema 运行依赖分类见[执行记录](EXECUTION_STATUS.md)。首轮发现的 SchemaIndex 来源身份问题已修复并全量回归。

已为下游保留[完整产品字节快照](../../implementation_evidence/WP-02/primary_freeze_844ed4ac1275/product.zip)及[ZIP 摘要](../../implementation_evidence/WP-02/primary_freeze_844ed4ac1275/product.zip.sha256)。封存时 WP-03 已更新两个依赖文件，归档使用与冻结清单完全匹配的 WP-02 依赖内容。归档全部成员逐项重新计算 SHA-256 并与清单比对。WP-03 按[已复核的计划](../WP-03/PLAN.md)在隔离环境继续。
