# WP-03 持久工作流实施计划

WP-02 已通过冻结验收，本计划经过主控及 Sol 定向复核。本包只在本地隔离环境开发和运行。引用[路线图](../../DEVELOPMENT_ROADMAP.md)、[六节点契约 §9–12](../../../specs/03_Six_Node_Execution_Contract_V1.0.md)、[持久架构 §6–7](../../../specs/04_Runtime_Technical_Architecture_V1.0.md)及[现行授权](../PLATFORM_GOAL.md)。上游基线为 668 个文件的 [WP-02 清单](../../implementation_evidence/WP-02/primary_freeze_844ed4ac1275/snapshot.json)。验收清单 SHA-256 为 `844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556`。

## 交付和依赖

| 批次 | 独占修改范围 | 接口、依赖与验收 |
|---|---|---|
| W3.1 PostgreSQL 与版本迁移 | `workflow/postgres.py`、`workflow/migrations/`、pyproject、uv.lock、隔离测试库 fixture | SQLAlchemy 2、psycopg 3，仅新增所需有界依赖。应用显式版本化 SQL，事务记录版本和脚本摘要。重复运行幂等，未知或不兼容版本拒绝。只在独占临时库运行迁移。 |
| W3.2 工作流状态库和接入 | `workflow/intake.py`、`workflow/repository.py`、对应测试 | 基于 W3.1。接入提交、绑定可信 Release、建 Task/Execution/初始作业和事件。相同作用域的两个请求键分别唯一；同键不同字节拒绝。恢复读取原 pin。保存 Domain 的版本化记录和显式索引，PostgreSQL 负责 Task 生命周期。 |
| W3.3 Worker 租约和执行权 | `workflow/jobs.py`、对应并发和恢复测试 | 基于 W3.1/W3.2 的表及事件接口。通过短事务领取，数据库时钟到期、单调 fence、持有者验证、heartbeat、超时/重试预算及取消。写权威产物、事件、终态和后继作业共享事务。保障迟到响应只追加审计。 |
| W3.4 六节点编排与 mock | `workflow/engine.py`、`workflow/providers.py` 和场景 fixture | 复用 WP-01C 节点 envelope、执行记录和有界关联检查。隔离的确定性 mock 返回经 Domain 验证的记录。N01 成功后运行 Pre-N02；业务节点沿已有输入/输出绑定。真实模型、GPU、模型质量等留在各自工作包。 |
| W3.5 故障与整包验收 | 独立测试及证据 | 待接口齐备后以公开入口贯通真实 PostgreSQL、已验收 Bundle/Release、六节点 mock、重启/接管与审计查询。产品清单冻结后独立验收。 |

主控维护唯一计划、检查实际差异和独立报告。开发、整改及定向 Challenge 使用 `gpt-6.2-sol` / `medium`。开发与验收分开派工，迁移及锁文件由 W3.1 单写。每个模块可先验证自身已满足的依赖。

## 接入、恢复与权威边界

1. 接入保存有界 submission 的规范摘要及项目/client/request/idempotency 标识。已有 WP-02 公开 Release 接口返回不可变 AcceptedTask。若本地 Release ledger 与 PG 分离，必须先持久记录 intake intent，以同一受信作用域和身份重试 Release 选择，按已存在 pin 完成 PG 事务。测试在 pin 产生后、PG finalize 前中断并切换激活版本，重投后仍返回原 pin。认证上下文由可恢复的可信宿主提供，禁止持久化凭据。未完成事务不向调用方报告 Task 已接收。
2. 接入完成时校验完整 ExecutionPins、工作流版本、模型和工具绑定与已准入的 Bundle 一致。NodeRun 按分支身份与上游输出建立幂等键，Attempt 编号单调递增；每次变更与 ExecutionEvent 原子落库。每个查询都能按作用域及执行 pin 核对历史。
3. 领取只占有可用且匹配作用域的作业。过期回收、取消与完成竞争时验证数据库时间与当前 fence。禁止跨事务持锁等待 Provider。崩溃窗口保留真实轮次、迟到输出及外部请求 ID，重试只按受限配置执行。原子写入的权威选择保证成功输出只应用一次。
4. 六个业务阶段采用确定性 fanout/fanin。N02 只见 eligible Profile，单一 N03 输入承接 N02 的完整命中集，逐 Profile 独立 grounding，保持有效问题集，N04 每 Object 一次正常联合请求，N05 依据预绑定计划，N06 mock 保留 mandatory 扫描清单和 Coverage，测试结果标为合成。区分无适用 Profile、无命中、no_target、失败和部分覆盖。无调用的 blocked/skipped 分支保存原因且没有 Attempt。
5. PostgreSQL 为本包执行状态的权威源。大媒体按不可变引用和摘要处理。本包只接受已验证的合成原件并检查关联，真实上传服务、鉴权 API、部署和大规模对象存储集成在后续范围。隔离环境所用 WP-02 存储和 mock 输出须在报告中声明。

## 验证与回退

- 使用本机已缓存的 PostgreSQL 16 镜像，记录完整镜像 ID、数据库版本、驱动及 Python lock；新建唯一命名的测试实例、空库和临时数据目录。禁止复用未知数据库。测试至少有两个独立连接或进程；真正终止 worker 后重启验证。固定时间/屏障驱动竞态，数据库时钟负责实际租约判定。
- 验证重复请求、两种键各自冲突、同键并发提交、申请与选版间崩溃、激活切换、过期但尚未接管的 worker、旧 fence 写入、续租竞态、重试耗尽、取消与迟到、原始事件/Attempt 不可变、发布重复后继与 PG 重开。每次校验数据库中的状态与历史，而非只断言返回值。
- 用同一经完整准入链验证的合成停车 Bundle 运行单命中、多 Object、空 eligible、无检测、局部失败与部分覆盖，另用两个 Profile 验证 N03 整批输入，验证输入输出、关联与隔离。保留完整关联 pin、终态、计数和外部调用为零的证明。新增越权作用域和跨 Execution 写入的拒绝用例。
- 全量 CPU pytest、Ruff、strict mypy、Schema 保真、已有 WP-01/02 回归和产品范围 diff。分别报告本地 PostgreSQL 运行验证、真实模型质量及尚待授权的外部验收。
- 迁移只对本包创建并能验证身份的数据库执行。回退使用事务或从专用测试备份在新的空库恢复并核对；清理仅限本包创建的容器和数据。项目历史证据和使用者数据库均须保留。

主控自审要求独立 reviewer 重点挑战跨存储接入丢失身份、时钟与 fence 竞争、未经验证的 mock 输出，以及将模拟节点结果当成真实推理的风险。

## 定向 Challenge 后的接口裁决

- **接入与身份。** PG intent 固定版本化的完整 TaskSubmission、stable_json 摘要、两个范围内唯一键、已认证 issuer/subject、状态和失败原因。宿主在恢复时重新解析该身份，以原作用域调用 ReleaseManager.accept_task 并重新鉴权。临时不可用进入可重试队列；身份变更或不可恢复保留可审计失败。相同提交只有在鉴权再次成立后才能读取结果。Release 和 Activation 的权威记录由其所属 ledger 保留。PG 通过公开 get_release、准入 receipt 和 read_archive 验证准确的 pin。当前激活指针不参与历史重建。
- **执行版本。** 核验实际 Bundle 字节，从 policies/execution.json 的 locator 确定唯一的 execution 策略，保存其精确 ContentRef，缺失或歧义拒绝；核对模型绑定/Provider/Fallback/部署。对 ToolDefinition 的 requirement，从准入回执中取得唯一匹配的 SupportedCapability.implementation_version 并验证 worker 可用性；无法唯一解析时给出路径。记录实际 host runtime 版本并核验兼容范围。按版本化、显式字段和稳定顺序计算输入摘要，写已解析 pin 的固定向量，测试同版本不同 ref、漏配、额外或替换的能力。重开按历史记录解析。
- **迁移和事务。** 测试自建库分 migration owner 与非 owner、非 superuser 的应用角色，应用不能建表、改迁移历史或改写历史事件。仓储调用必须带可信作用域，跨 Task/Execution 写入受复合 FK/唯一约束验证。设计表明示 scoped intake 双唯一键、每个执行唯一事件序号、每个逻辑作业的唯一键/当前 fence 与持有者、分支产物及完成唯一键。锁定行后按数据库当前实际时间验证租约；测试事务开始时有效、获得锁时过期。
- **抢占、超时和取消。** 领取待运行作业本身仅记录 job；真正开始时在一个事务创建并启动 Attempt 和 NodeRun。lease 过期的原 Attempt 依已知时间判定为 superseded，明确调用 deadline 先到者记 timed_out。终结后才追加符合 Domain 的 late event。重试创建新 Attempt 并保留运行中的 NodeRun。提前取消先持久化取消意图；调度器真实接管后按现有 Task/Execution 边记录 RUNNING，取消尚未调用的 NodeRun 后收敛到 cancelled，不生成模型或工具调用。验证取消意图到执行接管之间进程被杀后可重复处理。
- **公开场景与断点。** 使用 WP-02 的非机动车合成源经过完整编译、测试签名、准入、批准和激活；按公开校验器核对 envelope 成对关系和节点间记录。终态提交前后分别杀 worker，并检查子作业、事件和输出唯一性。所有“已运行”声明标出真实 PostgreSQL 与 mock Provider 的边界。

Sol 定向独立复核已允许 W3.1 隔离环境/连接/迁移骨架及 W3.4 纯 mock/公开 Domain 校验部分在上游验收后开工。W3.1 业务 DDL 与 W3.2/W3.3 共享表、锁顺序、作用域和角色授权由主控另作技术交接。持久化接线和全量验收按 DAG 继续。

### PG 仓储接口与并发约束

迁移使用显式 schema 名和命名参数，限定应用角色到本包专用库。迁移角色拥有 DDL 和版本记录。应用角色能读取所需表，向 intents、Tasks、Executions、NodeRuns、jobs 的当前投影写入指定列，仅可向历史事件、Attempt 结果、输出与权威关联插入新记录。用独立应用连接验证 UPDATE/DELETE/TRUNCATE 历史表及写 migration 表均失败。可信应用接口提供租户身份，全部项目数据查询与写入携带 project_id/client_id；数据库以相同作用域的复合外键限制跨执行绑定。运行角色凭据仅由宿主注入隔离环境，不向终端用户提供连接。

| 关系 | 最小主键或唯一性 | 原子写入、锁与不变量 |
|---|---|---|
| intake_intents | scoped request_id 和 scoped idempotency_key 两个独立唯一约束 | 对合法原身份原请求作幂等重试；双键指向不同意图时冲突。唯一冲突后开可用事务重读检查。 |
| tasks / executions | scoped task_id；scoped execution_id 对应唯一 parent task；本次 intake 引用唯一初始 execution | 保存受信 pin、源请求摘要、接收状态、关联；固定的输入与 pin 不受后续版本影响；允许后续独立 Execution。 |
| node_runs / jobs | execution + node + 稳定的分支身份区分全量/按 Object；每 NodeRun 一个逻辑作业 | job 保持当前可领取状态、lease_owner、lease_until、fence、ready_at、预算；每次授予新 claim 递增 fence。 |
| attempts / produced_outputs | node_run + attempt_number；每个权威输出另有明确的归属和幂等键 | 锁内检查当前 attempt/owner/fence/到期，成功才写结果；历史终态不可改。迟到产物不得占据权威键。 |
| execution_events / successors | execution + seq；父节点到子节点的解除依赖标志唯一 | 事件与投影同事务。防止重复解除依赖、重复扇出。重投重复完成需核对原结果摘要与身份，冲突拒绝。 |

实现需记录所有事务使用的一致锁顺序。候选作业可用 SKIP LOCKED 找到；领取作业以外凡同时更新 execution、node、job 的路径必须使用同一顺序，领取在确定候选后仍在锁内重验状态、期限和取消标志。不得将网络调用放在持锁事务中。共享 schema 与角色确认后，主控按实际 DDL 复核约束和索引，再放行 W3.2/W3.3 的写入部分。
