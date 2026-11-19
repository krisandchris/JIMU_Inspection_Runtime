# WP-04 定向技术复核

独立 reviewer 使用 `gpt-6.2-sol` / `medium`，主控核对了实际 ModelInvocation、ToolExecution、ToolStepSelection 和 Worker 接口。首轮指出跨 Attempt fallback、失败后物理调用留证、多步工具执行身份、HTTP 有界读取和进程未退出时释放资源五类风险。复核后的[计划](PLAN.md)把独立调用日志、输入版本证明和资源回收边界列为交付。WP-03 的正式验收是开工依赖。

| 项目 | 局部放行条件 |
|---|---|
| W4.1 模型路由、W4.2 有界 HTTP、W4.4 本地资源准入 | 已完成计划复核；上游验收后可在隔离环境实现和联测。 |
| W4.3 工具注册和直接执行 | 上游验收后可验证两种 origin、同一轮不同合法 step、版本及 I/O 绑定；先保留全套独立 peer 检查。 |
| W4.5 审计、可信输入和 PG 适配桥 | 可先做按物理派发身份追加的审计和恢复。集成依赖对应 adapter/queue 接口，有权威结果前需重验 fence。 |
| 失败的父 N05 中已成功 step 的跨轮复用 | 当前 authority 严格要求选中 SUCCEEDED Attempt，不能用 FAILED 的父级当作成功依据。仅这一项需进一步核对独立的 step 执行身份与历史见证。 |
| 真实 GPU 和 Provider | 受目标主机、权重许可、凭据使用范围、输入数据授权和预算等外部条件约束。测量前保留未验证状态。 |

步骤复用的实现首先验证能否保留独立、真正完成且被当前 fence 接受的成功步骤 Attempt，并显式关联其父 NodeRun/Attempt、step、Object、输入和结果。要求可回读不可变的 Step Completion、对应 ToolExecution 和完整输出组；通过实际 PostgreSQL 和全 peers 校验重建第一轮 A 成功/B 失败，第二轮仅重试 B 的结果。旧 Attempt 与结果均保留。如果使用现有已批准领域关系无法保存此身份，先出具可复现 Finding，并说明需要的外部 wire 变化，其他批次按既定范围继续。选择某个失权或失败的 Attempt 作为权威结果会改变已批准规则。

定向复核已找到符合现有 Domain 的实现路径。每个实际执行的工具步骤使用一个真实的 N05 子 NodeRun（node_id 仍为 N05）和原类型 NodeAttemptOwner 的独立 Attempt。父为业务 N05 工作单元，子仅为内部工具执行身份。源于配置的子分支不会成为第七个业务节点。调用前持久创建子轮，提交时同时验证父/子 claim、输入、版本和 fence。PG 以范围内外键保存 parent→child、Plan/step/Profile/Object、原输入及整组输出、提交/选择版本。先前成功的子 Attempt 可在原输入与配置仍相同时被后续父轮通过权威选择引用。失败或失权的父/子状态保持真实，不得补写成功。WP-01C 的 ToolNodeOutput.check_steps 对无内部见证的旧调用仍要求同轮。新来源验证路径需检查确切父子关系，聚合时完整装载子记录给 runtime authority 和 coverage，Planner 只接收父业务分支。独立 PG 测试必须观察真实子调用前后、重启后仍保留成功 A、只重试 B，没有旧 fence 逆写。此技术路线使用现有公开 ArtifactKind、AttemptOwner 与 RuntimeRecordKind。

正式派工基线为[WP-03 最终验收](../WP-03/FINAL_ACCEPTANCE_REPORT.md)的 735 文件快照。W4.1 路由、W4.2 HTTP、W4.3 工具、W4.4 资源池、W4.5 派发审计/桥接与子步骤各有独占写入负责人。跨步骤子执行使用现有真实成功的子 Attempt，先前失败父轮不可升级为成功。本轮派工只覆盖本地与隔离环境。
