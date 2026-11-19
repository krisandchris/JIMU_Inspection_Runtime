# WP-04 本地执行记录

WP-03 上游冻结清单 SHA-256 为 b8e95556aa80fae936bc12eabdf8dff3eb50509bcbea55735028d89267fb6825。开发工作树基于 2552affeb69fbc2594fe70724f895b0db0967d47。用户已明确继续本地开发，实机与真实服务待授权。

| 接口 | 目前证据和待核对项 |
|---|---|
| 路由与资源 | 主控第二次定向运行得到 34 passed；初次在工作区修改期间遇到 PolicyKind fixture 导入错误，保留首次日志与重跑 JUnit。CPU 子进程回收已有独立预检。 |
| Worker / PostgreSQL journal | 主控发现按单 operation 计算 node budget 会允许多个 operation 各自耗尽同一 Node 的总额。PG 写入方与桥接方联合在加锁发送意图前检查不同 operation 的使用量；并发验证和耗尽前拒发是结项条件。 |
| N04 联合响应 | 已选 Questions 固定的合同摘要必须匹配实际交给 HTTP 的响应 Schema；需检查错误渲染的 Schema 在发送前拒绝。 |
| 工具跨 Attempt | 验证子步骤 A 成功、B 首次失败，进程重开后仅 B 重发；完整回读真实父/子 Attempt、选定来源和逐步骤证据。 |
| 受限外部资源 | 在隔离库、回环 HTTP 和受控 CPU 工作进程上测试。真实云服务、GPU 容量与模型质量保留独立验收项。 |

主控日志和 JUnit 见 planning/implementation_evidence/WP-04/primary。当前为集成中证据，正式结论须基于冻结产品、独立审查、完整回归和差异检查。WP-05 已编制本地实现与实测分开的计划，产品实施按上游验收和技术复核推进。

主控后续定向检查取得 115 项 HTTP/路由/CPU 生命周期回归通过。另增跨版本测试，先用 001/002 建立和受理一条 WP-03 业务记录，再执行增量迁移，经应用角色重新读取原 Task/Execution/Node/Job，验证原 pins、非子步骤属性及历史能力记录的真实缺失，重跑迁移幂等，1 项通过。独立审查在真实回环和 PG 中复现已撤销的 Provider 不再收到 POST，但没有留下第二次拒派记录；桥接方负责补充拒发证据。其回归与跨 operation 预算回归均为局部技术门禁。

当前 Codex CLI 为 0.169.0。WP-04 继续使用已派工的 gpt-6.2-sol / medium。已启动名称为 planner 的原生只读子任务，请求模型 gpt-6.2-astra、思考档位 xhigh。当前会话审批策略为 never，Auto-review 没有触发。针对 W4.4 实际增量另行调用原生 codex exec review，完成状态以 reviewer 实际输出为准。

独立回环与 PG 复测已关闭两个 Finding。撤销所有权和同节点跨 operation 额度用尽均实收一条 POST；第二条留存各自的 not_dispatched 回执。集成中的定向持久化/工具组合重跑 29 项通过。另在跨子步骤实际超时和历史回读联测中发现两个问题：子时限到达应留下可重试的超时结果，反序列化的通用引用须与精确子类型逐字段比较。已安排独立复测即时错误与真实超时两条路径。

本轮主线程实际为 gpt-6.2-astra / high。已核对个人配置的新会话默认 gpt-6.2-sol / xhigh、planner 和 review_model 配置；WP-04 已派发的专项开发任务为 gpt-6.2-sol / medium。首次原生代码审查受当前主机的只读 sandbox 网络命名空间限制，结果为 UNVERIFIED；第二次保留相同权限，向原生 reviewer 直接传入基于 WP-03 清单取得的 72,215 字节产品/测试 diff（SHA-256 7b5edcca67c275b0c9b4228c59202593cdbccdf6e69a50eee580f24262705a8e），结果单列核实。

A/B 步骤回归已取得两份独立的 PostgreSQL + 实际子进程证据。B 首次直接报可重试错误或实际超时，均只发送 A/B/B 三次。原 A 的权威选择、父/子身份和失败记录保持可读，替换进程提交了唯一 N06 结果。经核对，原生资源 diff review 在提供实际 diff 后已返回 4 个 P1 和 5 个 P2；其中包括 provider 终态证据能否跨租约复用、guardian 自身截止、进程 namespace 恢复。当前正在逐项修复及复核，整包尚未签收。嵌套模型工具跨子 Attempt 的显式 fallback 另有定向验证；普通模型路由回归只覆盖其自身语义。

已从本地构建 wheel，在独立离线虚拟环境中用非 editable 方式安装并经 Python 隔离模式导入 Worker、Gateway、Journal 和工具执行器。wheel 中 001–005 五份迁移均与实际源码 SHA-256 一致。详细原始输出见 primary/wheel_preflight.txt。

独立 nested 联测已使用同一逻辑 N05 工具步骤的两轮真实子 Attempt。第一个回环主端确实超时，后续 Worker 沿相同操作和审核过的 fallback 路由向第二端发送一条 POST。两个端点各收一次，两轮都保留各自 ToolExecution，N06 发布并得到完整覆盖。另一次联测中，工具将真实 Invocation ID 替换为自报身份；校验记录失败，未形成成功的步骤选择。详见 independent/preflight-nested-1-fallback.log 和 independent/preflight-nested-1-forged.log。迁移 006 以可空字段保存原双 fence 内收到的事实，历史数据未推测回填。带存量记录的升级和迁移回归重测 3 项均通过。

首次全量预检的 6 个失败项已在新的产品快照、独立锁定环境复测全部通过。仍在开发的实际 JIMU 工作树保留所有失败记录。正式签收以最终版本的完整回归为准。原生资源审查的 9 项已记录处置：8 项本地修复，容器重建使用稳定宿主 PID namespace 时的实测留到获授权的环境。HTTP 与模型路由 diff review 的处置分别由相应维护者记录。当前验收边界详见 [主控预检](../../implementation_evidence/WP-04/primary/PRECHECK.md)、[独立验收矩阵](../../implementation_evidence/WP-04/independent/SCOPE.md)及[真实环境验证协议](REAL_ENVIRONMENT_PROTOCOL.md)。

已逐份读取七组原生限定 diff 审查，完整输出和对应 patch 位于 primary/。Journal 审查未提出可定位的缺陷。Step authority 要求区分超时终结与回包是否按时；Worker bridge 复核覆盖最后一次派发放行、N03 工具时限、永久预算耗尽和发送前取消。工具审查另外提出执行文件的实际字节、远端租约终态、存证异常和部分输出表示能力。这些任务按单写归属修复，保留历史读取兼容性及实际物理回归证据。WP-05 单谓词求值经只读 planner 确认完全独立于这些接口，已放到基于 WP-02 冻结 ZIP 的单独目录开发。WP-04 产品快照不包含该目录。

784 个产品文件已封存。产品清单 SHA-256 为 924eb788eab11bed400ec1f3602a9a30d3df7b43cba79e8601901eb7921f7495。独立验收已核对同一份 ZIP 和工作树，使用新建的离线环境启动宿主进程、PostgreSQL、回环 HTTP、CPU 工具、完整回归与 Schema 比对。真实模型服务、目标 GPU 和生产容量另行实测。[封存清单](../../implementation_evidence/WP-04/primary_freeze_924eb788eab1/snapshot.json) · [独立验收原始记录](../../implementation_evidence/WP-04/independent/frozen_run_K0PQbl5I/)

正式冻结轮已经结束，本地工程验收 PASS。3631 项全量测试、10 项独立运行探针全部通过，正式报告记录代码、运行环境、业务质量及用户授权的各自状态。[最终报告](FINAL_ACCEPTANCE_REPORT.md)
