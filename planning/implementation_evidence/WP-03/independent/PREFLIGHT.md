# WP-03 独立预检

`TEST_PASSED` 和 `RUNTIME_VERIFIED` 适用于 [预检 #14](preflight-full-pg-14.log) 的真实本机 PostgreSQL 16.14 和模拟节点。冻结后的全量结果另附。预检启动时工作树处于 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，WP-02 与 WP-03 实施文件均在本地工作树。各次预检期间产品实现持续更新。

## 独立数据库观察

- PostgreSQL 16.14 以本机固定镜像运行。迁移首次应用 001 和 002，重跑为空。应用角色的 DDL、迁移登记更新和历史 UPDATE、DELETE、TRUNCATE 均返回 SQLSTATE 42501。并发重复提交返回同一执行，其他作用域无法领取、取消或读最终报告。
- 独立进程的续租延长有效期。竞争方在此期间无法领取。另一次续租在租约有效时进入数据库锁等待，取得锁时租约已失效，因此被拒绝。接手 worker 使用 fence 2，原 fence 1 的 Attempt 终态为 superseded。迟到样本只新增审计，重复样本幂等。
- 两个 Profile 分别独立 grounding，产生三个 Object。每个 Object 发出一次含两个 Question 的 mock 联合请求。SQL 中关联到六条 Question Fact、三条 ToolExecution、三条 Tool Fact 和三份 Decision。独立重算 60 个权威产物 SHA-256。候选类别为空，必查 Rule 仍完成并保留三项有效命中。
- N02 第一次 mock 返回后、提交前被 SIGKILL 终止。恢复产生不同的 Attempt 和调用身份，数据库仅选择一个权威 Attempt。三份 N06 草稿提交后终止 worker，数据库此时无 N06 权威、无 terminal Attempt 结果和无最终报告。激活 release-2 后，新的 worker 只携带作用域从 PG 找到待完成运行并用历史 release-1 发布。保留前 63 条事件并形成 71 条完整事件。终止并再次重启后，报告、事件和发布计数保持一致。
- 无 eligible Profile、真实执行的 mock no_hits、已命中 Profile 的 no_target 分别保留不同结果。两 Profile 中一个 grounding 失败的场景保留另一 Profile 的违章，覆盖为 partial。skipped/blocked 无 Attempt。取消意图提交后终止进程，重启完成 Task/Execution/NodeRun 取消且零 Attempt。

## 预检发现与证据留存

- 使用 `uv run --locked pytest` 的入口缺少本地测试 namespace 搜索路径，pytest 收集失败。使用 `uv run --locked python -m pytest` 运行相同的迁移/接入四个模块，10 项通过。 [原始输出](preflight-pg-intake.log) [复测输出](preflight-pg-intake-module.log)
- 产品发现为历史执行策略引用的 Pydantic 子类比较、PG 轮询保留字、历史 Release 的 approval receipt，以及恢复时 N06 草稿产物重建。相关失败保留在对应的 [#2](preflight-full-pg-2.log)、[#7](preflight-full-pg-7.log)、[#8](preflight-full-pg-8.log) 和 [#10](preflight-full-pg-10.log) 中。修复后的路径在 [#14](preflight-full-pg-14.log) 通过。
- 其余阶段性失败来自探针接线及判读。包括 fixture 的接受时间晚于 PG 当前时间、以 Python 字典传递严格时间模型、N02 按 Profile 有多条调用审计、单 Question fixture 不能验证联合请求，以及 partial 分支应比较 wire 值 objects。保留原运行日志，最终探针用完整准入链额外配置第二个 Question 和独立 Fact Schema。
- 真实 Provider、GPU、外部系统、生产迁移和生产数据为 `RUNTIME_NOT_RUN`。这套证据只支持本地工程验收中的持久化、控制流及合成记录关联能力。
