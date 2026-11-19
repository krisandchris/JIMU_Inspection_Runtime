# WP-03 本地实施与验收

上游 WP-02 工程验收通过，3191 项 CPU 测试无失败、错误或跳过。主控独立校验快照、JUnit、静态检查与本地运行记录，按已复核计划放行 WP-03。PostgreSQL 固定镜像、独占临时库、合成停车 Bundle 和无外部调用的 mock 节点均在已记录的本地授权内。[上游交付](../WP-02/FINAL_ACCEPTANCE_REPORT.md) · [实施计划](PLAN.md) · [表、作用域与事务边界](DB_CONTRACT.md)

| 批次 | 当前工作和交接依据 |
|---|---|
| W3.1 | PostgreSQL 16.14 隔离迁移和运行角色授权验证通过。应用与迁移角色分开，脚本上传前验证数据库身份。主控复跑 17 项迁移及拒绝测试。锁中新加 5 个包，原有第三方版本保留。[迁移验收](../../implementation_evidence/WP-03/postgres/README.md) · [主控 JUnit](../../implementation_evidence/WP-03/primary_pg_GhX6gJiq/tests.xml) |
| W3.2 | 已接入上游实际准入记录、历史 Release 和原始 ZIP，核对能力并生成不可变执行 pin。循环接收、恢复的原 scope 与用户身份重新验证。仓储、接入及两条幂等键的 PG 回归进行中。[pin 证据](../../implementation_evidence/WP-03/pins/VERIFICATION.md) |
| W3.3 | 开发租约、时限、取消与迟到响应。PG 调度读取数据库当前时钟，按 Task、Execution、NodeRun、Job 统一加锁。合成 Provider 调用位于事务外。 |
| W3.4 | 六节点纯编排和 mock Provider 已开始联调。单一 N03 接收 N02 全部命中，N04 按 Object 联合观察，N06 先保存有 fence 的草稿。 |
| W3.5 | Worker 接入真实 PG 和已编译 Bundle，在所有分支结算后检查来源并原子发布唯一 Coverage 和 Decision。独立 reviewer 使用专用隔离库验证并发、崩溃、完整审计和旧包回归。[独立验收矩阵](../../implementation_evidence/WP-03/independent/SCOPE.md) |

WP-03 尚在开发，真实模型、设备性能和产品准确率另行验证。独立 reviewer 和各模块写入负责人已分开。

### 联调记录

主控已在独立临时库复跑迁移与权限用例 17 项、接入恢复和冲突用例 3 项，以及双进程竞争并中断提交的租约接管用例。后者从数据库读回原 Attempt、接管后的递增 fence 和唯一生效结果。产品随后扩充了含 NUL 的幂等键、按作用域发现 pending intent、繁忙队列分页和分阶段发布的测试。[接入 JUnit](../../implementation_evidence/WP-03/primary_intake_ZyvjFwPe/tests.xml) · [双进程 JUnit](../../implementation_evidence/WP-03/primary_fence_0rOgXJR8/tests.xml) · [仓储回归](../../implementation_evidence/WP-03/repository/REPORT.md)

W3.1/W3.2 正常入口已接通真实 PG。W3.3/W3.5 独立联调已从进程间接管、N02 调用后中断推进到三个 N06 草稿已写且尚无权威发布的断点。作用域扫描 SQL 与历史 Task→Activation 的精确读取正由对应负责人修复和复测。整包技术验收需要冻结后的完整公开入口、全量回归及独立运行证据。

### 冻结交接

独立预检完成两 Profile、三 Object、每 Object 两个 Question 的整链。PostgreSQL 中可追溯六条 Question Fact、三条 Tool Fact 和三份 Decision。独立 runner 重算 60 份产物摘要；在 N02 回包落库前和 N06 草稿写入后分别结束 worker，凭可信作用域重新找到任务，并在切换激活版本后按原版本发布。开发时发现的历史 Scope 读取及真实失去租约的终态见证已由专门回归覆盖。[独立预检](../../implementation_evidence/WP-03/independent/PREFLIGHT.md) · [历史激活接口](../../implementation_evidence/WP-03/historical_activation/REPORT.md)

产品按清单 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825` 冻结，735 个文件，相对 WP-02 新增 67、修改 10、删除 0。[清单与产品字节](../../implementation_evidence/WP-03/primary_freeze_b8e95556aa80/snapshot.json)。主控在冻结资料上独立复跑完整 Worker 和丢失租约的 Coverage 用例，8 项通过。[JUnit](../../implementation_evidence/WP-03/primary_chain_5acz87pD/tests.xml)。独立冻结轮已经完成一次真实 PostgreSQL 整链，继续全量测试和静态/Schema 核验。
