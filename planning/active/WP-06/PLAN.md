# WP-06 证据生命周期、回放与评测

WP-06 的验收条件包括历史记录不可变、Artifact Replay 零模型与工具调用、Inference Replay 创建新 Execution，以及数据集、真值、评分协议、模型与工具版本可追溯。开发子任务使用 gpt-6.2-sol / medium。主控已与原生只读 planner 核对依赖。纯评分与复核记录使用 WP-04 封存代码作为独立基线。读取完整现场运行记录的批次依赖已验收的 WP-05 存储和 Worker 接口。

| 批次与写入归属 | 实施方案 | 验收 |
|---|---|---|
| 独立评测，evaluation 包及专属测试 | 建立有版本的样本清单和真值修订、受评预测、评分协议与报告。复用 ScoringProtocolPin、ActorRef、ReviewReportRef、ExecutionPins，记录未知实际部署。按同一输入快照产生可重算的样本结果、各阶段统计和 Bad Case 依据。合成数据用于验证算法，评分与样本真值独立。 | 拒绝重复样本、来源/协议 pin 替换、留出集泄漏、无依据的实际身份和未裁决标签冒充真值。逐项核对分子、分母、零分母、弃判、无目标、漏检/误放，分别报告所有输入和有真值样本。人工意见可分歧，旧快照可追溯。 |
| Artifact Replay，独立只读仓储和评分服务 | 复用 ReplayExecutionSource.validate_execution、get_execution、read_finalization、read_output、read_authority 和 Model/Tool journal，在统一的历史作用域中校验原始记录与规范化记录摘要。读取物理证据时另外校验内容摘要、版本与权限。使用新的运行 ID 和可按 EvidenceRef/digest 读取的评分报告，保留 ArtifactReplayRun。 | 禁用模型和工具端口，独立审计调用数为 0。旧 Execution、来源 Decision/事实、Authority、job 和 dispatch 的内容与计数保持不变。重复请求幂等，错摘要、跨 Scope 和缺件有明确诊断。 |
| Inference Replay，单独的持久受理和 Worker 状态投影 | 以 scope、幂等键和请求摘要原子建立新 ReplayRun/Execution、N01、job 与历史能力快照，沿现有六节点执行。原 Task 关联不变；新 Execution 的调度、取消、失败和完成只投影到本次 replay。最先验证相同配置 pins 的重跑。 | 并发/重复请求只建立一次目标；旧任务已终结时仍能创建新执行。确认物理调用、新 Attempt 和正确关联，源 Task、事件和结果不变。恢复时重开固定 Release，撤销不暗换模型。 |
| Evidence lifecycle，归属媒体适配与相应持久记录 | 复用作用域绑定的媒体读回、Model/Tool 回执、有版本的 retention 和 immutable 引用，区分到期、无权访问、未留存、缺失、损坏及迟到。清理策略先在专用本地库与专用存储目录演练。 | 测试一次写入后跨进程读回、相同物理内容的多方引用、运行中/复核中仍在用的证据、到期执行与中断幂等。保留合法的历史元数据与处置依据。 |

## 契约边界和局部授权

- v1 的 Execution.input_digest 包含 Task 输入、Release、Bundle、receipt、模型和工具。InferenceReplayRequest 要求两次 input_digest 相等。共享原 Task 时新执行还需要独立的状态投影。现有同 pins 回放可以开发；跨版本对比必须有区分 Task 输入身份与执行配置身份的版本化方案，不能更改旧 Digest 的解释。
- ArtifactReplayRun 的 scoring_evidence 必须有真实可读取的独立记录。设计阶段优先验证在现有 EvidenceRef 命名空间中保存有类型、受范围隔离的评分记录。若需要扩充公开 EvidenceType，先完成兼容审查。
- 收到真实标签和业务阈值前，报告仅呈现观测结果。错误放行率的分母为有真值的违规任务，自动放行风险的分母为实际自动放行的、有独立真值的任务，同时报告未标注自动放行数。样本未授权或未定标时，真实质量结论为 UNVERIFIED。
- 本地临时授权范围内的 CPU、隔离 PostgreSQL、合成数据可继续。真实样本、GPU、计费 Provider、对外发布和生产数据处置沿用单独授权。公开 API/Compose 归 WP-07。

完整验收从最终 WP-05 封存包衔接，重启、切 Release、零调用和实重推理用独立探针核验，代码/测试、运行证据、业务质量及用户验收分别报告。回退保留上游快照，追加的复核、回放及评分记录各自按不可变身份重开。

## 当前分批基线

评测实现以正式 WP-04 封存物为上游。另将经过独立回环/CPU Worker 验证的 WP-05 预检包 f4c27c5baa09fb958dd068b301b1d5880e364f6919ed48b67cbae1a84d79504a 的 930 个文件逐项校验，复制到三个彼此独立的开发目录，分别验证 Artifact Replay、同配置 Inference Replay 与 Evidence lifecycle。该操作允许隔离验证持久契约；正式合并时逐文件比较 WP-05 最终封存基线。数据库迁移 009、010、011 分别对应上述三个交付，各自拥有自己的测试。已有源码差异以最终 WP-05 为权威整合，并重跑升级和回归。
