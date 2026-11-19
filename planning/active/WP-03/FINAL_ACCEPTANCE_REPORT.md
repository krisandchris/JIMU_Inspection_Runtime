# WP-03 工程验收

**PASS。本地 PostgreSQL 持久工作流与六节点合成执行通过验收。** 基线为 WP-02 产品清单 `844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556`。本包在同一 detached Git 基线 `2552affeb69fbc2594fe70724f895b0db0967d47` 上冻结 735 个产品文件，清单 SHA-256 为 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`。相对上一包新增 67、修改 10、移除 0。已保存本包完整产品字节。[清单及变更](../../implementation_evidence/WP-03/primary_freeze_b8e95556aa80/snapshot.json) · [产品快照](../../implementation_evidence/WP-03/primary_freeze_b8e95556aa80/product.zip)

| 维度 | 核对结果 |
|---|---|
| 代码与契约 | 接入身份、不可变执行版本、PG 角色/迁移、作业认领和 fence、可审计的 Attempt、节点编排、模拟适配器、Coverage 原子发布均有实现和独立复核。历史版本精确读取增加了兼容的只读入口。完善了有实际来源时混合 grounding 失败及租约耗尽的校验；原 Domain Schema 字节保留。 |
| 自动化测试 | `TEST_PASSED`。JUnit 共 3363 项，成功 3363、失败/错误/跳过均为 0。正式环境执行产品 lint/format、全树 lint、严格 mypy（含生成脚本）、Schema 双次独立导出、冻结前后核对全部通过。347 份既有 Schema 与索引逐字节一致。 |
| 隔离运行 | `RUNTIME_VERIFIED`。PostgreSQL 16.14 真实连接、双进程竞领、失去租约拒绝旧写、进程中断后按 scope 恢复、模拟服务已返回后再次调用但只选择一个权威结果，均有实际证据。两 Profile 的三 Object 各进行一次含两 Question 的合成联合观察，核验六条 Question Fact、三条 Tool Fact、三份 Decision 及 60 份持久化产物摘要。N06 三份草稿持久化后终止进程；切换 Release 后由新进程按最初 pin 完成唯一终态发布。 |
| 生产和质量 | 真实 Provider/GPU、真实图像业务质量及生产环境为 `RUNTIME_NOT_RUN` / `UNVERIFIED`。本轮验证了 worker 中断恢复，数据库容器磁盘灾难恢复演练在 WP-07。 |

[独立验收报告与原始证据](../../implementation_evidence/WP-03/independent/FROZEN_REVIEW.md) · [主控整链定向复测](../../implementation_evidence/WP-03/primary_chain_5acz87pD/tests.xml) · [独立 JUnit](../../implementation_evidence/WP-03/independent/frozen_run_uXkkc6tO/pytest.xml)

应用角色和迁移身份分离，新建专用数据库；新增依赖限定于 PostgreSQL、异步 SQLAlchemy 和开发类型声明。全量严格类型检查现已包含既有 Schema CLI。检查共享工作树时一次并行导入改变了字节码缓存，单独用例及完整冻结轮均已复测。[依赖与运行环境](../../implementation_evidence/WP-03/postgres/README.md) · [Schema 生成器证据](../../implementation_evidence/WP-03/schema_typing/README.md)

本次是工程团队在本地授权内的验收。用户验收、Git 发布及生产部署单独记录。下游按[WP-04 复核结论](../WP-04/PLAN_REVIEW.md)继续。
