服务查询显式使用 project/client scope，迁移显式授予 SELECT、INSERT；评分是对已持久化 Decision 和选中 Fact 的计数，不是模型准确率或物理调用证明。历史归属校验和见证固定存在缺口，同键并发返回已有结果也依赖未声明的事务隔离条件。STATIC_REVIEW_ONLY：仅审查所给 diff，未独立核验冻结基线，未调用外部服务；TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P1] 按 Decision 所属 Object 和 Profile 核验 Fact 引用 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact_source.py:437-440
  `selected_ids` 汇总了整个 Execution 的 Fact。若同一 Execution 中对象 A 的 Decision 错引了对象 B 已选中的 Fact，这里的成员检查仍会通过；后续 proof 检查也只确认 Fact 属于所引 Selection，没有确认该 Selection 属于 A。这样会把证据归属错误的 Decision 计入评分。应按 Decision 的 Object、Profile 核验 explanation 和 rule proof，并增加双对象交叉引用的负例。[AGENTS.md:60](AGENTS.md#L60)

- [P2] 将重放所见的 journal 见证绑定到准备好的请求 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact.py:243-245
  `prepare_request()` 读取见证后丢弃结果，`run()` 再读一次并写入 evidence。若两次读取之间只有 `send_admitted` 或 `fence_qualified` 发生变化，所有 `source_artifacts` pin 仍可匹配，旧请求却会为另一组派发见证生成报告。评分计数未必变化，但请求没有固定报告引用的这部分历史。可在准备阶段固定有序见证的摘要，在首次创建 run 时校验；同键重开已接受结果则不必重读历史。[AGENTS.md:101](AGENTS.md#L101)

- [P2] 明确同键并发处理所需的事务隔离级别 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact.py:221-225
  当传入的 PostgreSQL engine 使用 `REPEATABLE READ` 时，第二个事务可在等待 advisory lock 前取得快照。即使第一个事务已提交，第二个事务的 `_read_key()` 仍看不到新行，随后会重复评分并在插入时失败，而不是返回已接受的 run。唯一约束防止了重复落库，却没有保证调用幂等。应显式使用此算法所需的 `READ COMMITTED`，或在冲突后用新事务读取并比对请求。

- [P2] 将 eligibility 引用与实际读出的 Context Snapshot 对齐 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact_source.py:326-331
  这里确认了 gate 与 coverage 引用同一个快照，却只要求实际读出的 `context` 非空。若前两者指向 X、实际读出的是 Y，这段核验没有发现错配，加入输入清单的也是 Y。应将 `context.context_snapshot_id` 与 `coverage_scope.context_snapshot_ref.record_id` 直接比较，以确保重放固定的是本次 eligibility 判断引用的 N01 快照。[AGENTS.md:57](AGENTS.md#L57)

- [P2] 按 Profile 核对 coverage 中的 Object 归属 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact_source.py:363-367
  集合推导把所有 Profile 的 `object_refs` 展平成一个集合。两个 Profile 的 Object 被对调时，全部 ID 和对象总数都不变，这项检查仍会接受错误的 coverage。应以历史 grounding 记录为准，逐 Profile 比较对象集合，而不只比较整个 Execution 的并集。[AGENTS.md:59](AGENTS.md#L59)

- [P2] 核对归档 ModelInvocation 与存储行的 Attempt 归属 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact_source.py:234-239
  `_archived_models()` 用查询所得 `(nid, aid)` 读取归档，但验收条件只有 invocation ID、输出键和 manifest digest。若 invocation 及其匹配的 manifest 被归档在错误 Attempt 下，摘要仍能匹配；后续 journal 核验比较的是 invocation 自报的归属，不是这里读到的存储位置。应同时比较 `(record.node_run_ref.record_id, record.attempt_ref.record_id)` 与 `(nid, aid)`。[AGENTS.md:93](AGENTS.md#L93)

- [P2] 在逐条读取历史输出前执行清单上限检查 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/replay/artifact.py:141-144
  对超过 1024 项的历史，`prepare_request()` 会先逐条读取并校验输出、构造全部 pin，再查询 journal，最后才报 `artifact_replay_inventory_limit`。因此这个上限只限制返回值，不能限制一个注定被拒绝的请求占用的数据库连接时间和查询次数。应在收集规范化记录标识时尽早截断，并在读取 journal 前拒绝超限历史。

- [P2] 在零调用回归测试中独立观测本地工具执行 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp06_artifact_replay.py:335-337
  `calls == dispatched` 能观测测试 HTTP peer，但 `scored.tool_calls` 是服务直接写入的 `Literal[0]`，不是工具执行计数。如果重放路径误调用本地工具而未新增 journal 行，现有断言仍可通过。应在历史生成结束后，给工具执行入口加独立计数器或一经调用即失败的 guard，覆盖 `prepare_request()` 和 `run()` 两步。[AGENTS.md:90](AGENTS.md#L90)