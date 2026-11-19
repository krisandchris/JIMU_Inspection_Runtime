# WP-05 独立预检记录

以下各轮复现均使用当轮清单锁定的文件，记录预检事实。正式独立验收由 `run_acceptance.sh` 针对主控提供的封存清单执行。实际测试仅访问隔离 PostgreSQL、回环 HTTP 和主机 CPU。外部 Provider、部署、代表性实拍数据与生产质量未验证。

| 已验证行为 | 证据 |
|---|---|
| 场景编译、Ed25519 验签、激活、原图与 Task intake。客户端跨范围和错误摘要拒绝。独立固定双物体图原像素已核对。 | [入库预检原日志](preflight-intake-01.log) |
| 空发现、成功定位到零对象、模型返回越界坐标和实际定位超时分别得出 uncertain，分派次数依次为 1、2、2、2，均无物理 CPU 调用。 | [无 Object 四条路径](wheel_preflight_uGjkLZ1k/no-object-cases-system-python-02.log) |
| 单对象贯穿六节点。实际已发 4 个 HTTP POST、3 个 CPU 工具，终态 violation，候选空列表下强制 Rule 已完整检查。 | [固定清单](wheel_preflight_8q7xlIe9/product_sha256.json) [独立读回](wheel_preflight_8q7xlIe9/workflow.log) |
| 双对象独立判定。部分覆盖保留已有违规。缺槽为 error，有效不可见响应为 unknown。整批坏包仅重发一次。候选 HTTP 503 仍按强制规则得出违规。 | [六条场景日志](wheel_preflight_8q7xlIe9/more.log) |
| 独立双标签图片中 20 个重合像素分别进入轮胎和盲道掩模。几何 sidecar 为 blocking_tactile_paving，违规、引用和原像素已核对。 | [输入摘要](fixtures/wheel-on-tactile.sha256) [真实六节点日志](wheel_preflight_3ZYeSVrI/overlay.log) |
| N01 或 N04 应用后终止初始进程。激活 B、验证新 Task 使用新 Device 和 Camera 修订，重开 A 且继续原版原图和证据。 | [N01](wheel_preflight_8q7xlIe9/restart-N01.log) [N04](wheel_preflight_8q7xlIe9/restart-N04.log) |
| N04 已失败整批并持久化后 SIGKILL。不同进程、相同 A 固定输入，第二批物理发送且唯一具有权威，失败批和 previous_batch_ref 可独立查询。 | [失败整批重开](wheel_preflight_3ZYeSVrI/restart-N04-retry-03.log) |
| N05 仅第一 Tool 已持久化且实际发送时 SIGKILL，激活 B。A 恢复旧 Context 与工具结果，后两步真实派发，三工具各只启动一次。 | [子步骤分配前断点](wheel_preflight_3ZYeSVrI/restart-N05-pre-admission.log) |
| N05 第二子步骤已分配且尚未实际派发时 SIGKILL。场景 max_attempts=1 时预算耗尽，第二步零物理发送，guard 有缺证据原因，结果 uncertain。将预算版本化为 1.0.1、参数为两轮后经正式编译、签名、准入和激活，同窗口恢复并完成。第二工具两条 child 历史的物理分派分别为零和一次。 | [预算一轮](wheel_preflight_3ZYeSVrI/restart-admitted-no-retry.log) [预算两轮](wheel_preflight_3ZYeSVrI/restart-admitted-retry-02.log) |
| 旧 A 重开时当前登记的部署 ownership 已撤销。匹配不到可派发的固定部署，PG 保存 model_unselected / not_dispatched / model.ownership_unavailable，回环和 CPU 都没有实际启动，终态 uncertain。 | [撤销恢复](wheel_preflight_3ZYeSVrI/restart-revoked-02.log) |
| Linux managed CPython 3.12.12 缺少解释器封装的 memfd/fcntl 常量。在含兼容修复的锁定快照构建 wheel，完整真实六节点成功。 | [固定清单](wheel_preflight_vrVlryJH/product_sha256.json) [物理运行](wheel_preflight_vrVlryJH/workflow.log) |
| 缺少候选配置时使用真正新签并激活的 RuleSet 1.0.1，只有三个必要模型 POST。与 HTTP 503 和真实候选超时一样，仍逐条扫描强制 Rule。 | [候选未配置](wheel_preflight_2b1lL22r/candidate-not-configured.log) [真实候选超时](wheel_preflight_vrVlryJH/candidate-timeout.log) |
| 新版 N04 通过统一的 scoped LocalMediaStore 读取提示和 Schema 原件，逐条核对大小、摘要及权限边界。 | [统一媒体存储下的真实六节点](wheel_preflight_2b1lL22r/workflow.log) |

早期实物差异有对应原日志。3.12.12 下资源模块导入依赖解释器提供 Linux 标志，导致 wheel 无法启动。一个过渡快照中 `StepAttemptStore.begin` 尚未接收集成方的 `ancestor_evidence`；另一个过渡快照中 N04 使用了已改名的 `failure_code`。更早的 N05 子步骤校验则复现了已完成祖先证据未纳入权威输入核对。各轮使用日志中的独立清单。用同一早期快照执行零 Object 也出现过验收 SQL 选择列歧义，之后修正并成功。早期 strace 仅将临时隔离租约 token 脱敏，保留物理进程命令及返回。后续探针仅保留成功启动的子进程 PID 和工具名。
[早期 Python 导入问题](wheel_preflight_uGjkLZ1k/wheel.log) [早期子步骤 API](wheel_preflight_uGjkLZ1k/workflow-system-python.log) [早期 N04 API](wheel_preflight_AJYNMY36/workflow.log) [早期 N05 输入来源](preflight-workflow-03.log) [验收 SQL 误差](wheel_preflight_uGjkLZ1k/no-object-cases-system-python.log)

同一 fc66f900… 固定清单上 Ruff 检查及格式通过，strict mypy 在两处产品模块发现六项错误，已交对应维护人。Schema 命令位于失败命令之后，此轮未运行。 [静态预检](wheel_preflight_vrVlryJH/static-snapshot.log)

同样各轮在固定产品上运行。ac5ddbbd… 的 Schema 检查及 63 项单谓词 pytest 通过，保存 JUnit。f4c27c5b… 的 strict mypy 检查 182 个模块通过，Schema 两次导出的 347 个文件及索引与 WP-04 逐字节一致。该轮 Ruff 查到一份新测试需格式化，已交维护人，最终快照会重验。 [63 项 JUnit](wheel_preflight_2b1lL22r/predicates.xml) [严格类型与 Schema](wheel_preflight_4L7CxZYM/mypy-schema.log) [格式诊断](wheel_preflight_4L7CxZYM/static.log)

在这一固定版本重验 N05 已完成子步骤恢复、Scoped Store 提示与 Schema 的实际 HTTP 内容、两类超时以及缺槽与不可见的精确原因。 [N05 验证](wheel_preflight_4L7CxZYM/workflow.log) [物理 HTTP 与归档逐条核对](wheel_preflight_4L7CxZYM/wire-archive.log) [后台结果和失败原因](wheel_preflight_4L7CxZYM/exact-reasons.log)
