# W4.5 物理派发记录

## 范围与交付

在上游 WP-03 验收和独立计划复核后放行。增加受信 Worker 的发送前意图、物理调用结果、迟到归档与额度审查接口。业务产物提交仍由现有 DurableJobs fence 处理。按 Execution、NodeRun、Attempt、job、fence 与 Worker 绑定原始调用。嵌套工具步骤按父 NodeRun 查找逻辑操作，同时保留每次调用自己的子 NodeRun 和 Attempt。

- `src/jimu_runtime/gateway/journal.py` 提供事务内派发校验、按实际发送记录的预算、原始身份核验、独立结果追加与稳定分页。
- `src/jimu_runtime/gateway/journal_records.py` 定义版本化内部 Pydantic 记录，包含端点描述符和所有者证明、实际 Provider 身份、完整响应摘要、可信 Tool 版本及资源租约身份。
- `src/jimu_runtime/tools/audit.py` 共享受信的工具诊断文本与状态码。工具业务输出必须经过已准入契约校验后才保留为正式 ToolExecution。
- `src/jimu_runtime/workflow/migrations/003_dispatch_journal.sql` 增量增加操作、意图和结果三张表以及查询索引。应用角色拥有读与追加权限。
- `tests/fixtures/wp03/{intake,jobs,repository}/support.py` 使用实际迁移目录完成测试环境启动。旧迁移并发、失败和前缀场景仍在专门用例中断言。
- `tests/unit/test_wp04_dispatch_records.py`、`tests/contract/test_wp04_dispatch_journal.py` 验证记录形状、拒绝输入与隔离故障窗口。

## 记录与状态

发送方先以当前 fence 获准并提交派发意图。意图无回执表示结果未决。已知拒派、可能已发但结果未明、已收到确认分别保留。跨操作的预算在同一 Execution 锁内检查。明确证实未派发的尝试释放发送额度，已准入但送达情况未明的意图继续计入额度。第一个回执和随后异步到达的确认以同一物理派发 ID 分别追加。先收到的业务权威结果可继续引用其原始成功 Attempt，迟到归档只写审计表。

模型通信仅记录有界且已过滤的结构化字段、规范化 ModelInvocation 及完整响应 SHA。未完整读取的回包保留实际边界、字节数与受控省略原因。工具保留经过验证的正规 ToolExecution、独立记录摘要及选定的原始输入引用。原始供应方正文、请求正文、凭据、认证头及任意错误消息未进入日志字段。

## 验证与限制

- `TEST_PASSED`。合并回归 42 项通过，0 跳过，用时 55.05 秒。独立纯 Domain 层依赖边界检查通过，依赖锁断言由负责 HTTP 依赖的批次验收。验证命令、退出码和开始/结束时间见 [verification.log](verification.log) 和 [static.log](static.log)。见 [pytest.log](pytest.log)、[环境](environment.txt) 和 [文件 SHA-256](final-sha256.txt)。
- `RUNTIME_VERIFIED` 限隔离 PostgreSQL 16.14、自建 loopback 服务与合成 CPU 工具结果。案例确认两个物理 HTTP 调用保留不同整包摘要，原 Worker 在写入意图后被终止仍能恢复读取，已取消 Tool 和失去父 fence 的子步骤仍可独立归档。跨项目、错原始 fence 和重复写入受到校验。
- 首轮定向测试暴露了测试环境禁止 loopback 及引用类型过泛。后续使用仅允许该测试自建端口的 fixture 和严格的 NodeRunRef/AttemptRef，重跑通过。定向类型检查也纠正了复用 canonical_bytes 的导入路径。无未执行的定向子用例。
- `RUNTIME_NOT_RUN` 真实 Provider、生产数据库和目标 GPU。该证据覆盖派发和原始归属；可信业务输入、节点接线与真实模型表现由其他批次验收。

## 迁移与验收

验证原 001/002 历史行的 SHA 和时间戳不变，并验证追加迁移的幂等性。发生应用故障可先停用派发写入路径并保留审计表。恢复采用后续增量迁移，不删历史 Release、Execution 或派发证据。

隔离环境的派发日志验收为 PASS。WP-04 集成和外部部署按主控报告分别验收。
