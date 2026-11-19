# N05 嵌套模型 fallback 的派发证据

嵌套模型调用按同一父 NodeRun、ToolPlan、step、Tool、Profile、Object、模型绑定和稳定输入摘要定位。每次物理派发仍绑定独立的子 NodeRun、子 Attempt 和资源租约。现有普通模型与 Tool 的记录类型和查询路径分开。嵌套模型额度按父 NodeRun、部署、step 和精确操作分别统计，写入由 Execution 事务锁串行化。

PostgreSQL 在同一时钟采样中检查两个当前 Attempt、各自的 lease 和 deadline，以及 Execution 的取消状态。006 新增可空字段 `dispatch_results.fence_qualified`。真值表示已准入派发在双 fence 内收录了相应回执。旧回执保留空值且无法证明资格。已有派发审计无需重写。

fallback 的来源必须是同一操作、同一 Tool 步骤和同一固定输入的更早子轮次。原父 Attempt 与原子 Attempt 均须有失败终态。journal 要求从原始物理派发读取非迟到且获得双 fence 资格的回执，并与失败子步骤的完整 ModelInvocation 逐字段一致。服务端已确认的失败和规范化的 on-time `timeout/sent_unknown` 均按实际观察保存。后者不提供服务端应答或远程终止的证明。派发意图、无实际派发、错源和失去 fence 后才写入的记录无法授权 fallback。

## 验证

- `TEST_PASSED`。定向与回归共 22 项通过，0 跳过，45.88 秒。覆盖两次真实 PostgreSQL 子 Attempt、父范围查询、预算聚合、同轮重复提交、错源、有效 fence 内的超时和失权后的超时。首轮测试发现 fixture 使用了 NodeRun 上不存在的引用属性，改用真实 Attempt 的 owner 引用后通过。[命令和退出码](nested-verification.log) · [pytest 输出](nested-pytest.log)
- `RUNTIME_VERIFIED`。离线构建 wheel，实际从 wheel 加载 001–006 并逐份核对字节 SHA。用 wheel 中的加载器在隔离 PostgreSQL 执行 005→006，原回执字节语义和时间戳保持，fence 资格为 NULL。重复迁移返回空后缀。[wheel 和升级证据](nested-wheel.log)
- 严格 mypy、Ruff 和 `git diff --check` 通过。迁移 001–005 未改动。[本批 SHA](nested-sha256.txt)
- `RUNTIME_NOT_RUN`。真实 Provider、生产数据库和 GPU。该批证据验证持久派发和资格裁决。业务接线和真实部署由对应验收记录覆盖。

回退时先停止新嵌套派发，保留 journal 和现有 Release、Execution 证据。后续纠偏通过增量迁移完成。
