# WP-04 W4.5 Worker 集成验证

## 范围和实现

在 WP-03 验收后接入公开的 `WorkflowWorker`，使其按实际启动的 Attempt 调用受信 Model Gateway 或 Tool Executor。Task 入库在同一 PostgreSQL 事务记录执行能力快照与精确的模型和工具 ownership。Worker 用该历史快照重建并核验既有 Execution pins，在派发和接受结果时读取当前授权。原有合成适配器可继续运行。历史上没有能力快照的 Execution 使用原有合成入口。切换到实际适配器需要可核验的历史快照。

- `src/jimu_runtime/gateway/bridge.py` 为每次物理派发保存独立 intent 和收据，区分授权前拒发、已发未确认和已确认响应。确定未发出的取消不占硬预算。实际网络发送前核验拥有者、派发时限和 PostgreSQL fence。节点、操作与 deployment 硬额度均产生不可重试的具名结果。
- `src/jimu_runtime/gateway/bridge_tools.py` 以有界 typed Tool 输入确定复用资格。N05 独立子 Attempt 在原有 owner 下保存结果。父节点引用匹配的完整输出组。核验当前 Tool 注册覆盖 intent 提交完成后的启动窗口。
- `src/jimu_runtime/workflow/` 的 Worker 接口、状态、校验、归档和 planner 视图分别接收与验证业务父节点和内部 N05 子记录。N03 单次 Tool 截止基于本次调用开始时间并受父 Attempt 截止限制。
- `src/jimu_runtime/workflow/migrations/005_execution_capabilities.sql` 为增量迁移。部署失败时保留已有关系和执行证据，修复代码或表权限后向前恢复。实际适配器要求存在准确的历史身份。原有 WP-03 合成入口仍可读旧执行。
- `src/jimu_runtime/workers/ownership.py` 统一模型和工具的线程资源移交。等待方退出后，尚未移交的资源由获得资源的线程补偿，包括事件循环已退出的场景。

## 复核和实际证据

本批隔离验收为 `PASS`。桥接定向用例 18 项全部通过，未跳过。独立拒发和嵌套探针均以 0 退出。静态类型检查通过 14 个源码文件。

Native diff review 对基于 WP-03 冻结产品的桥接差异提出四项意见。收据、双 fence、N03 截止和硬预算修订由当前隔离测试验证。原始静态复核和 scoped diff 见 [native review](../primary/native_worker_bridge_review.md) 与 [scoped diff](../primary/native_worker_bridge_delta.patch)。本批没有交互式 Auto-review 审批请求。

- [integration-final.log](integration-final.log) 记录公开 Worker 的普通执行、真实 503 后跨 Attempt 显式 fallback、在途撤销和取消、发送前中断接管、新近未决派发阻断旧 fallback、未发出取消和拒发、Tool 注册撤销、硬预算，以及 deadline 和资源回收。
- [dispatch-final.log](dispatch-final.log) 记录两条独立 loopback 请求场景。当前 ownership 撤销和跨 operation 原子预算均只发送一次，并各保存第二条拒发收据。
- [nested-fallback-final.log](nested-fallback-final.log) 记录独立 Tool 子 Attempt 的嵌套模型 fallback。两次实际 POST 分属不同子 Attempt 与 worker owner，前次 timeout 留证，成功输出最终进入 Coverage。
- [nested-forged-final.log](nested-forged-final.log) 记录伪造嵌套 ModelInvocation 被拒绝。物理记录保留，伪造结果无 Tool selection。
- [static-final.log](static-final.log) 和 [bridge-final-sha256.txt](bridge-final-sha256.txt) 记录本批源码静态检查和相关文件摘要。

测试使用 Python 3.12.3 与 PostgreSQL 16 隔离容器。容器镜像摘要为 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。各日志列出实际依赖版本、准确命令、时间与退出码。CPU 工具由受信主管启动，HTTP 流量仅到测试自建 loopback 端点。整体发布门禁由主控汇总。

## 限制与交接

物理请求递交与业务结果采用是独立边界。进程中断留下未经证实的已准入派发时，按可能发生发送计入预算。某个较新已准入派发仍未决时，以较早失败为依据的 fallback 会被阻断。

Provider-inflight 资源在发出许可前按精确 ARMED owner 回收。后续检查若在取得 ACTIVE 后拒发，派发日志保存 `not_dispatched`，额度进入保守结算路径，由宿主独立 verifier 认可证据后释放。工具和模型的历史证据从原始 scope、owner 和 Attempt 读取。

本次运行证据覆盖隔离 PostgreSQL、loopback HTTP 与受监督 CPU subprocess。目标 GPU、真实模型服务、模型质量及生产配置验收尚待授权与验证。下一交接点为主控的 WP-04 产品验收和后续 WP-05 接口门禁。
