# 冻结前独立预检

按 AGENTS、README、四项规范、架构决策、路线图、依赖与测试策略顺序核对，再参考停车示例、WP-04 计划与复核和 WP-03 验收。产品基线、场景及边界见[验收矩阵](SCOPE.md)。以下均为冻结前记录。

| 检查 | 证据 |
|---|---|
| 独立 CPU 子进程 | `RUNTIME_VERIFIED`。共享 SQLite 账本由两个本地进程竞争，同一容量拒绝并发占用。SIGKILL 运行监督进程，工具和脱离原进程组的后代结束后才接纳新租约，旧 token 无法释放新租约。实际超时后容量亦恢复。首次脚本把 /proc 已消失误当异常，随后纠正观察逻辑。 [原始失败](preflight-resources-1.log) [复测](preflight-resources-2.log) |
| 物理派发、速率限制、上限、版本和迟到归属 | `RUNTIME_VERIFIED`。新 PG 16.14 和真实 loopback。已提交 8 个发送意图，端点收到 8 次 POST，PG 保留 9 个观察，含一份归属原 fence 的迟到完整响应。跨 Attempt 冒领被拒。429 退避上限、超限无完整摘要、JSON 重复键、非有限数及缺版本均通过独立断言。 [日志](preflight-model-9.log) |
| 当前登记撤销 | 先复现网络拒绝成功且第二次无拒派审计。修复后当前登记撤销得到 `model.ownership_unavailable`，审计含第二个 `not_dispatched`，物理网络仍只有一次。 [原始数据](preflight-bridge-4-revocation.log) [修复后复测](preflight-bridge-6-revocation.log) [在途撤销](preflight-bridge-1-inflight-revocation.log) |
| Node / deployment 预算 | 同一 NodeRun 执行两个不同 operation，预算为 1。第二个被拒，PG 中可见 `not_dispatched / model.call_budget_exhausted`。 [日志](preflight-bridge-1-ceilings.log) |
| N05 计划内独立 Step 实际超时 | 真实 CPU 步骤在子截止时间运行超时，PG 保留 A succeeded 与 B timed_out，journal 保留 B 的 timeout。父仍持有效 lease，接桥返回 `tool_step_fence_lost`，父执行未按显式可重试 timeout 路由。 [复现和只读 SQL 结果](preflight-tool-7-physical-timeout.log) |
| N05 第二轮聚合读取 | B 首次从真实子进程返回结构化临时错误，下一轮实际只执行 B。PG 持久聚合曾拒绝读取真实成功组。具体 diagnostic 显示所有 owner 和 pin 存在，子输入 digest 一致，两个输出引用字段相同；仓储还原为 `RuntimeRecordRef`，内存结果为子类 `ToolExecutionRef` / `FactRef`，直接 Pydantic 对象比较不等。 [公开入口复现](preflight-tool-6.log) [定位](preflight-tool-8-history-diagnostic.log) |

预检还保存了 fixture 接入兼容、时钟与对象作用域的调试原件。预检脚本不承担业务运行或生产部署验收。

两项 N05 修改后的下一轮预检确认真实调用序列 A / B / B、原 A 子身份和完整 `ToolNodeOutput` 读回均成立。父 Worker 提交时曾在持久化通用循环尝试用父身份存储已由子 Attempt 生产的 Fact/ToolExecution，仓储拒绝 `output_owner_mismatch`。修复前的 [即时失败](preflight-tool-9-retry-recheck.log)、[真实超时](preflight-tool-10-timeout-recheck.log) 和 [调用及父子身份](preflight-tool-11-parent-artifact-proof.log) 原件保存。

修复后的两条 N05 完整路径都已独立通过。真实物理序列始终为 A / B / B，原父 Attempt 保留失败，A 权威来自原子 Attempt。即时 B 错误保留 failed / transport，超时路径保留 timed_out / timeout。后继 Worker 均发布唯一 N06、complete Coverage、合成 Decision 和 completed Execution。 [即时故障恢复](preflight-tool-12-retry-release.log) [物理超时恢复](preflight-tool-13-timeout-release.log)

独立回环验证还覆盖两台部署之间显式 fallback。真实主端点在收全请求后超过本地 HTTP 时限，不同 owner 认领后续 Attempt，按当前有效的独立评审切换第二端点。两端各收一次，PG 两份派发共享原 operation，后继 Invocation 直接引用原 timeout 和准确评审。 [日志](preflight-fallback-2.log)

资源补充故障验证独立暂停 guardian 越过截止时间、恢复后收到整树回执才重新准入，并独立强杀 guardian 后在无法证明整树终态时保持容量占用。额外占住执行线程覆盖派发前超时，等待中的请求失效后释放预留，物理子进程未启动。 [原件](preflight-resources-4-queued-deadline.log)

独立 model-backed Tool Bundle 使用额外的 vision_auxiliary Binding，经真实编译、签名和准入。N05 子工具的首次嵌套模型请求在回环主端点超时，后继子 Attempt 在不同 worker 上依法切换部署。两端各收一次，两个 ToolExecution 和两个 nested_model 派发保留各自子身份，N06 恢复完毕。负向试验以真实调用为基础变造同一 Tool 回报的 Invocation ID 和匹配的引用摘要，Tool 持久审计为 failed / validation，未生产成功组或选择，真实网络调用仍可查，变造 ID 未进入 journal。 [嵌套 fallback](preflight-nested-1-fallback.log) [拒绝变造](preflight-nested-1-forged.log)
