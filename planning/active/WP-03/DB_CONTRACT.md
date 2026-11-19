# WP-03 PostgreSQL 持久化交接

基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 加已验收 WP-02 工作树。迁移 `001_workflow_schema.sql` 创建 `jimu_workflow` 并由 `jimu_workflow_owner` 拥有。`002_workflow_tables.sql` 只追加业务对象，对应用角色 `jimu_workflow_app` 显式授予读取、插入及可变投影列的更新权限。无需默认表授权。应用无 DDL、迁移记录写权及表的 DELETE/TRUNCATE；历史表无 UPDATE。持久写入仅使用已验证身份与独占临时数据库。

## 表和列

下表的 S 是 `project_id text, client_id text`。E、N、J、A 分别是 `execution_id`、`node_run_id`、`job_id`、`attempt_id`，类型都是 text。ID、状态和摘要必填，未注明 NULL 的值必填。Domain JSON 的 `schema_version` 为 `1.0.0`。应用验证完整 Domain 类型，SQL 验证每条记录的身份索引和所属复合外键。FK 无级联。状态投影与事件在同一事务更新。

| 表 | 列及约束 |
| --- | --- |
| `intake_intents` | S、`request_id text`、`idempotency_key bytea` (end-to-end 保持 Domain 字符串，索引保存 UTF-8 原字节)、`submission_digest text`、`submission json` (TaskSubmission)、`issuer text`、`subject text`、`created_at timestamptz`、`status text` (pending/accepted/failed)、`failure_code text NULL`、`task_id text NULL`、E NULL。PK(S,request_id)，UNIQUE(S,idempotency_key)，另有按请求 ID 字节序的 pending 局部索引。仅状态和结果列可更新，accepted 要求同原 request 的 Task/Execution，failed 要求失败码，pending 无结果。 |
| `tasks` | S、`task_id text`、`request_id text`、`release_id text`、`bundle_digest text`、`record json` (AcceptedTask)、`status text` (TaskStatus，当前投影)。PK(S,task_id)，UNIQUE(S,request_id)，FK(S,request_id)→intent。 |
| `executions` | S、E、`task_id text`、`release_id text`、`bundle_digest text`、`execution_policy_ref json` (精确 PolicyContentRef)、`scheduling_limits json`、`scheduling_limits_version text`、`scheduling_limits_digest text`、`record json` (初始 ExecutionRecord)、`state json` (可变生命周期字段)、`status text`、`next_event_number bigint DEFAULT 1`、`cancel_requested_at timestamptz NULL`。PK(S,E)，FK(S,task_id)→Task。原接入引用还验证 Task/Execution 归属。Execution 允许独立 replay ID。 |
| `node_runs` | S、E、N、`node_id text`、`branch_key text`、`input_digest text`、`predecessor_id text NULL`、`record json` (初始 NodeRun)、`state json` (可变生命周期字段)、`status text`。PK(S,E,N)，UNIQUE(S,E,node_id,branch_key)，FK(S,E)→Execution，FK(S,E,predecessor_id)→NodeRun。 |
| `jobs` | S、E、N、J、`status text` (ready/leased/staged/done)、`fence bigint DEFAULT 0`、`lease_owner text NULL`、`lease_until timestamptz NULL`、`ready_at timestamptz`、`max_attempts integer`、`timeout_ms integer`、`retry_backoff_ms integer[]`、`retryable_error_classes text[]`、`retry_on_lease_loss boolean`、`attempt_count integer DEFAULT 0`、`current_attempt_id text NULL`、`attempt_deadline timestamptz NULL`。PK(S,E,J)，UNIQUE(S,E,N)，UNIQUE(S,E,N,J)，FK(S,E,N)→NodeRun。持有人/租约成对且仅 leased 有值，leased 的当前轮次/调用期限成对。staged 保留 current_attempt_id/fence，清 owner、lease_until 和 deadline，退出可领取集合。同 Node 的当前轮次、当前 fence 与 lease_owner 的复合 FK。 |
| `attempts` | S、E、N、A、`attempt_number integer`、J、`fence bigint`、`lease_owner text`、`record json` (running Attempt snapshot)。PK(S,E,N,A)，UNIQUE(S,E,A)，UNIQUE(S,E,N,attempt_number)，UNIQUE(S,E,J,fence)，FK(S,E,N,J)→Job。 |
| `attempt_results` | S、E、N、A、`status text`、`record json` (terminal Attempt snapshot)。PK(S,E,N,A)，FK→Attempt。启动及终结均为独立不可变记录，读回终结优先。 |
| `produced_outputs` | S、E、N、A、`output_key text`、`output_kind text`、`output_digest text`、`record json` (版本化 Domain 记录，比如 NodeInputEnvelope/NodeOutputEnvelope)。PK(S,E,N,A,output_key)，FK→Attempt。 |
| `authority_outputs` | S、E、N、A、`output_key text`、`output_digest text`。PK(S,E,N)，固定 `node.output` 键和生成的 `node_output` 类型加入产物复合 FK，生成的 `succeeded` 状态加入 AttemptResult 复合 FK。仓储还验证所选成功 NodeOutputEnvelope 的记录及摘要。 |
| `execution_events` | S、E、`event_number bigint`、`event_id text`、`recorded_at timestamptz`、`record json` (ExecutionEvent)、`task_id text NULL`、N NULL、A NULL。PK(S,E,event_number)，UNIQUE(S,E,event_id)，可选索引列按事件类型检查并用复合 FK 限定所属。 |
| `successors` | S、E、`upstream_node_run_id text`、`downstream_node_run_id text`，两端 FK 同一 Execution，PK 为 S、E 和两端。初次写入代表该上游已满足依赖。 |
| `job_completions` | S、E、N、J、A、`fence bigint`、`result_digest text`。`PRIMARY KEY (S,E,J,fence)`，按组合列交叉约束已有 Attempt 及其 terminal 结果。完成重投核对原 Attempt、fence、owner、摘要。 |
| `late_responses` | S、E、N、A、`response_key text`、`result_digest text`、`external_request_id text NULL`、`response_ref json`、`event_number bigint`。PK(S,E,N,A,response_key)，FK→AttemptResult，FK→ExecutionEvent。 |
| `engine_outputs` | S、E、N、`output_key text`、`output_kind text`、`output_digest text`、`record json`。PK(S,E,N,output_key)，FK→NodeRun。Pre-N02 Gate 等工作流运算和无 Attempt 的 SkippedDiscoveryOutput 使用此表。 |
| `decision_drafts` | S、E、N、A、J、`fence bigint`、`draft_key text`、`draft_digest text`、`record json` (W3.4 版本化 draft)。PK(S,E,N,A)，UNIQUE(S,E,N,draft_key)，FK→相同 job/fence 的 started Attempt。可取消未发布的 draft。 |
| `execution_finalizations` | S、E、`finalization_digest text`、`record json` (W3.4 控制的版本化决策与 Coverage 封装)。PK(S,E)，FK→Execution。写入最终封装及终态共用单事务。 |

Digest 列与 Domain 一致采用 `64 lowercase hex`。SQL JSON 保留文档原文，包括合法转义 NUL。SQL 作用域、归属和状态约束使用关系列。仓储根据 Domain 交叉核对文档与索引，避免用 JSON 解码 SQL 运算处理含 NUL 的完整记录。SQL JSON 读回通过 Pydantic 的 JSON 校验路径，避免 strict 模型在 Python 模式下将数据库字符串当成错误类型，或把绕过校验的 `model_copy(update=...)` 直接持久化。通用输出写入由调用方提供封装类型与稳定身份，读取方提供 Pydantic 验证类型；权威输出受现有 NodeOutputEnvelope 校验和关系约束。

## 锁顺序和事务责任

所有共享方法接收已开始事务的 SQLAlchemy `AsyncConnection`，调用方负责 commit/rollback。只读方法可直接查询连接。对已知 Execution 的写入一律锁 Task → Execution → NodeRun（多个时按 node_run_id 排序）→ Job（多个时按 job_id 排序）。Job 候选扫描不加锁。逐个候选尝试父行的 `FOR UPDATE SKIP LOCKED`，锁内复查 ready/过期、当前 fence/owner、取消、attempt/期限。空候选释放本轮事务再查另一候选，避免取得部分父锁后打破顺序。锁全部取得后以一次 `clock_timestamp()` 做资格比较，不能使用事务开始时间。不得持锁调远端、媒体存储或 Release 服务。原子作业终结的写入顺序为 terminal Attempt（guard 和事件由仓储处理）→ 合格 outputs/authority → NodeRun → 事件/后继/终态及必要决策封装 → completion receipt。迟到先保证 Attempt 终结，只追加响应及事件，不触碰权威输出和后继。

接入在两段独立 PG 事务之间调用原身份 Release API。尝试插入时用 `ON CONFLICT DO NOTHING` 容许两个独立唯一键冲突。然后用新的语句、按作用域以 OR 同时读两个键，校验唯一匹配、规范摘要、issuer/subject；不得退化成只比一个键。最终落库先锁 intent，再同事务建 Task、Execution、N01 NodeRun/Job、初始状态事件并回写 intent 的 Task/Execution 指针。回收 intent 按原 authenticated identity 重新鉴权。 intent 只在接入事务持有，不参与 Worker 锁链。

## 仓储调用

以下是 `workflow.repository` 模块级函数，省略每个函数开头的 `connection: AsyncConnection, scope: IdempotencyScope`。未标注事务的读取无需写锁。Transition 参数均为 Domain 枚举和模型；调用方使用领域状态机的显式 guard。

| 接口 | 语义 |
| --- | --- |
| `reserve_intent(submission, *, issuer, subject) -> IntakeIntent` | scoped 幂等保留并核对胜出记录 |
| `get_intent(request_id, *, for_update=False) -> IntakeIntent | None` | 恢复 durable submission 和原身份 |
| `list_pending_request_ids(*, after_request_id, limit) -> tuple[str, ...]` | 供宿主恢复，仅列同作用域 pending，按 request_id 的字节顺序以 keyset 分页 |
| `accept_intent(intent, task, execution, execution_policy_ref, scheduling_limits, root, job) -> LockedExecution` | 事务内首次 finalize 或验证已接入同一结果，返回历史 pin |
| `fail_intent(intent, *, failure_code) -> IntakeIntent` | 原身份重授权不能恢复时记录永久原因 |
| `get_execution(execution_id) -> LockedExecution | None` | AcceptedTask 加投影状态与 Execution |
| `lock_execution(execution_id, *, skip_locked=False) -> LockedExecution | None` | 先锁 Task，再锁 Execution。返回具名 `task, task_status, execution, execution_policy_ref, cancel_requested_at, scheduling_limits, scheduling_limits_digest` |
| `get_node(execution_id, node_run_id, *, for_update=False) -> NodeRun | None` | 还原单 NodeRun |
| `list_nodes(execution_id, *, for_update=False) -> tuple[NodeRun, ...]`、`list_branches(execution_id, *, for_update=False) -> tuple[tuple[str, NodeRun], ...]` | 依 node_run_id 排序读取，后者提供稳定范围键 |
| `get_attempt(execution_id, node_run_id, attempt_id) -> Attempt | None` | 终态存在时优先返回 |
| `insert_node(execution_id, node, *, branch_key) -> NodeRun` | 唯一 (node_id,branch_key)，重复验证不可变输入 |
| `insert_job(execution_id, node_run_id, job: NewJob) -> None` | 新 job；NewJob 字段为 job_id、ready_at、budget，必须匹配固定的节点 JobBudget |
| `insert_attempt(execution_id, node_run_id, attempt, *, job_id, fence, lease_owner) -> None` | append-only 初始 Attempt |
| `finish_attempt(execution_id, node_run_id, previous, following, guard, *, now) -> None` | 校验 Domain 状态机后 append 终态 |
| `transition_task(execution_id, task_id, previous, following, *, now, trace) -> None` | 校验并更新 Task，追加同事务事件 |
| `transition_execution(previous, following, *, now) -> None` | 校验并更新 Execution，有状态变更时追加事件 |
| `transition_node(execution_id, previous, following, *, now, guard=None, appended_attempt=None, prior_attempt=None, retry_authorized=False) -> None` | 校验并更新 NodeRun，有状态变更时追加事件 |
| `append_event(execution_id, trace, payload, *, now, attempt=None) -> ExecutionEvent` | 用锁内单调序号追加，Domain 检查晚到事件拥有匹配终结 Attempt |
| `list_events(execution_id) -> tuple[ExecutionEvent, ...]` | 按序读回完整事件 |
| `store_output(execution_id, node_run_id, attempt_id, *, output_key, output_kind, record) -> str` | 内容规范摘要，重复必须 byte-equivalent |
| `read_output(execution_id, node_run_id, attempt_id, output_key, model) -> T | None` | 按调用给定验证类型读回 |
| `select_authority(execution_id, node_run_id, attempt_id, *, output_key, output_digest) -> None` | 当前时间和 fence 由 W3.3 调用方锁内验证 |
| `read_authority(execution_id, node_run_id, model) -> T | None` | 读取被终态成功 Attempt 选定的 node.output |
| `store_engine_output` / `read_engine_output` | 和 output 接口一致，省略 attempt_id 参数 |
| `store_decision_draft(execution_id, node_run_id, attempt_id, *, job_id, fence, draft_key, record) -> str`、`read_decision_draft(execution_id, node_run_id, attempt_id, model) -> T | None` | 未取得发布权的 N06 草稿 |
| `store_finalization(execution_id, record) -> str`、`read_finalization(execution_id, model) -> T | None` | 锁内发布或读取最终封装 |
| `link_successor(execution_id, upstream_node_run_id, downstream_node_run_id) -> bool` | 原子去重，首次插入返回 true |

W3.3 拥有 Job 投影 SQL、claim/heartbeat/cancel、并发事务和完成/迟到收据。W3.4 负责发布前公开 Domain 关系校验和选择可持久的决策封装。此交接只在隔离 PG 应用角色验证，发现约束缺陷以新预发布脚本或回退隔离库处理。验收重点包括复合 FK、不可改写历史、双键分叉、复合写回滚和锁后时钟。
