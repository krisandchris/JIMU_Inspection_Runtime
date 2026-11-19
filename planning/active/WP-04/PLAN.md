# WP-04 Model Gateway 与 Tool Executor

上游 WP-03 已通过独立工程验收，冻结清单为 `b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825`。本计划经主控及 Sol 定向复核，已放行本地实现及隔离测试。本包只实现供应方接入、工具执行、资源控制及其审计，停车业务的真实节点和判定语义属于 WP-05。负责人和测试环境按[授权台账](../PLATFORM_GOAL.md)处理。[技术架构 §3–5](../../../specs/04_Runtime_Technical_Architecture_V1.0.md) · [路线图](../../DEVELOPMENT_ROADMAP.md) · [版本门禁](../../DEPENDENCIES.md)

| 批次与负责人 | 实现与依赖 | 独立验收 |
|---|---|---|
| W4.1 模型路由及审计 | 一个 writer 负责 `gateway/routing.py` 和 `gateway/invocation.py`。Domain 的 ModelBinding、ProviderPolicy、FallbackPolicy、CapabilitySnapshot 和 WP-03 Execution pins 驱动选择。测试原始调用、重试和经评测的显式转移；保存配置身份、实际身份来源、请求/响应摘要、原始调用及 Attempt 引用。 | 远程 primary、本地 primary 的精确例外、缺证据、撤销、错误 scope/版本、同模型但不同部署以及未授权 fallback。超时和限流按配置处置。 |
| W4.2 HTTP 适配器 | 一个 writer 负责 `gateway/http.py`、httpx 运行依赖与 lock。通过受信宿主配置的固定 endpoint、部署标识、协议和凭据引用访问；按明确的受限协议转换图像/Prompt/response contract、限制响应字节及总时限。可选本地 vLLM 的兼容协议走显式本地能力。 | 真实 loopback HTTP server 和实际序列化/解析；检验 success、超时、限流、取消、不兼容响应、缺版本、超限、凭据不进入日志或 Bundle。不得把模拟 server 作为真实模型评测。 |
| W4.3 工具注册与执行 | 一个 writer 负责 `tools/registry.py` 和 `tools/executor.py`。绑定受信工具实现到 ToolDefinition、版本和输入输出；校验 schema、资源预算、坐标系、InvocationOrigin、N03 Grounding 与 N05 step 权限和输出映射。 | 同一实现用于不同节点的 provenance、未注册/错 pin/资源不足的拒绝、执行成功/局部失败/超时/中断，以及工具错误保留原始证据。 |
| W4.4 资源准入 | 一个 writer 负责 `workers/resources.py` 和隔离工作者生命周期。由宿主显式配置 CPU/remote/GPU 预算；同一 GPU 的模型与工具同时计入同一容量。明确预留、占用、释放及 worker 崩溃后恢复、避免旧持有者释放新租约。按真实设备测量结果才接纳运行配置。 | 跨两个本地进程的竞争、取消、过期旧 token、无预算、重复释放与隔离重启。使用合成预算标记调度逻辑验证。 |
| W4.5 执行集成和验收 | 一个 writer 负责 WP-03 Worker 的适配桥及实际调用审计写入。通过现有入口、游标、Attempt 与 fence 验证配置切换和迟到响应。以上接口稳定后接入。 | 经受信 Release 和执行 pins 运行到实际适配器及隔离 CPU Tool，验证正常/failed/undispatched 的完整 ModelInvocation/ToolExecution、证据、受约束 fallback 和权威应用。 |

开发/修复代理使用 `gpt-6.2-sol`，思考档位 `medium`。路由、HTTP、工具和资源预算可在契约确定后分别实施；共享依赖锁与跨模块接线由指定单写者负责。Domain 保持纯契约，适配器内封装供应方 wire。支持的信息必须来自服务返回或授权的外部版本注册表；实际版本缺失时写 Domain 的 unavailable 形态。生产 secrets 由宿主注入且不得写入证据。

### 需要复核的接口

1. 外部调用和业务事实分开持久化。检查外发前后的 Execution/NodeRun/Attempt、调用身份、资源预留和真实 provider_request_id。崩溃/超时后可审计物理重调；回包经当前 fence 验证才成为权威输出。禁止假设网络执行一次。
2. Fallback 从已固定的 primary、触发的失败调用与已独立评测的箭头派生；声明兼容性、功能和当前评测/授权都要匹配。记录每个物理调用而非用最终成功覆盖原失败。明确定义 handoff 和预算耗尽、限流重试、取消同时发生时的裁决。真实模型/工作者的本地部署由对应环境另行放行。
3. Tool allowlist 只由宿主建立。Bundle 只能引用资源和既知实现；工具不会根据数据动态加载可执行代码。按原坐标和变换链审计。资源使用情况区分实测、预估和不可用。长任务必须能隔离终止；不能因调用方超时但后台仍占用资源而提前释放真实容量。
4. CPU-only 回归使用已审有界 httpx 和现有依赖，记录实际 lock、干净运行环境及增量。GPU 层与 CPU 层独立，不把 CUDA/vLLM/SAM3 加进通用环境。服务测试只能访问本包创建的 loopback 端点。
5. 正式 Runtime/GPU 门禁单列：需要受权的实际 provider/部署、可用模型权重及许可、GPU 环境和预算，实测冷启动、各进程峰值显存、共驻、并发、OOM 恢复，记录硬件、驱动、容器摘要与命令。真实测量缺失时保持该验收项开放，其他本地工作可依赖其已满足的接口继续。业务准确率和真实图像逐节点评测在 WP-05/06 归档。
6. 主控冻结产品、独立运行全量测试、检查 347 个既有 Domain Schema 与 source boundary、检查锁定原件和审计；分开报告代码、隔离运行和真实部署验证。回退限本地文件、临时进程与自建存储，历史 Release/Execution 记录保留。

### 调用审计与 Worker 交接

- 每次物理发送独立分配 dispatch ID，关联 Execution、NodeRun、Attempt、调用目的、配置、请求摘要和预算。先在 PostgreSQL 提交发送意图，再在事务外发送；回包、传输错误、拒派或取消各有独立的追加记录。服务在发送与响应落库之间崩溃时保留已知边界和 provider_request_id，由重试或查询补充；不能凭发送意图推断远端执行次数。响应证据须逐 dispatch 保存，不能只保留同轮的第一个响应。历史日志、迟到记录和权威 Fact 各按自己的权限写入。
- 触发 fallback 的原始失败保留精确 InvocationError.error_class。transport、timeout、rate_limit、unavailable 分别与显式 Policy、评测、剩余重试预算核对；对 NodeError 的分类需有固定映射（如限流为资源类、服务不可用为传输类），不能据映射猜测触发资格。Worker 先在原 Attempt 落真实终态与触发 Invocation，再认领同操作下的新 Attempt；fallback 明确引用原轮次、Invocation、授权转移和独立质量证明。SDK 禁止隐式重试/换模型。超过 node/operation/provider 预算即输出可审计终态。
- N02 选题与发现可以共享一个实调记录，只有策略明确允许时才有独立选题调用。N03 同一逻辑轮可能有多条 ModelInvocation 或 ToolExecution，N04 每个 Object 正常路径保持一次联合请求，单个字段解析失败仍保留物理回包和其他可验证字段。N06 的可选 candidates 调用与业务扫描分开。真实操作归属通过 exact input/response pin 和原始 operation ID 确认。WP-04 交付调用边界和可注入 adapter，WP-05 承接停车业务的实际转换与判定。
- W4.5 真实 PG 合成故障测试包括发送意图提交后与 socket 发送前中断、隔离 HTTP 端点已经收包但本地尚未提交、一个逻辑操作有两次物理调用且第一份回包迟到、越 fence 仍保留非权威证据、失败后首次允许的 fallback 由不同 worker 接管。认证参数与原始敏感响应都不得进入持久诊断或测试日志。

### 跨轮次、协议和容量的复核修订

1. 可信宿主的 actual provider/deployment/model/implementation 版本与证明引用在接入时固定。保留可回读的解析结果和摘要；派发时取当前登记、独立评测及撤销状态，只可验证既定选择是否仍可用。注册表变动不自动替换执行版本。多个无明确次序的可用 fallback 边拒绝歧义。响应中缺版本只能用精确、可核验的版本证明补足；实际 model ID 和版本都无法可信确认时，保留 dispatch 诊断和原始可见字段，禁止发布依赖可信版本的 ObservationBatch。
2. W4.5 将 Worker 取历史媒体、已准入配置与实际调用分开。保留现有合成 adapter，提取不要求 fixture_deployment_id 的宿主源接口，调用 journal 位于调度事务外的受限 append-only 接口。对于未收到完整响应或拒绝超限响应，只记录确实观察到的边界与诊断；不得把前缀摘要作为完整响应摘要。
3. HTTP 适配仅向事先配置的端点发请求，默认 TLS；明文仅允许明确隔离的 loopback 服务。禁用环境代理、重定向及 SDK 隐式重试。媒体以验证后有界字节传入；Bundle 和服务响应均无权指定取数目的地。限制未压缩与解码后字节或禁用压缩，流式累计字节并实施单调时钟总截止。自建服务测试无 Content-Length、慢速分块、超限、非有限数、重复 JSON 键、取消、429 和有界 Retry-After。
4. W4.3 校验同一 NodeRun/Attempt 的合法兄弟 Tool 调用。唯一执行身份绑定 Execution、NodeRun、Attempt、Profile、Object、精确 Plan 与 step（N03 则为具体 Profile Grounding binding）。不允许复用任意 ToolExecution ID、混用 N03/N05、冒充另一 scope 或同一选定 step/Attempt 两份结果。W4.3 的专门契约复核须把 Domain 的 peer 检查与 N05 总结的逐步引用放在同一用例，不能通过省略 peers 获得通过。
5. per-step 重试保存同 NodeRun 下的独立 Attempt、ToolExecution 和全步输出组。已成功兄弟步骤的权威结果不变。最终 step summary 对每个实际 ToolExecution 恢复其原始 Attempt；引用较早工作时提供 NodeRun 历史、完全一致的上游/配置、该 step 的明确权威选择或等价可查证记录。没有历史见证时保留现有同轮约束。重试不要求调用已经成功的兄弟步骤。嵌套 ModelBinding 失败后的合法转移发生在新 Attempt，保留两条 ModelInvocation 及各自 ToolExecution。对已确认 wire 的兼容性分析与先前 WP 全量回归是该批的技术交付。
6. GPU/CPU worker pool 要区分常驻与单次用量，并以原子事务或唯一资源拥有者核算共享总量、每 worker、每工具或部署的限制。超时/失联先标 draining。旧 token 不得释放新持有者的额度，旧进程树仍存在时不得重分配实际资源。CPU 用真实子进程回收、取消和 supervisor 重启证明；GPU 另需取得目标设备、软件和负载授权并实际测量。不得用声明容量取代实测。
