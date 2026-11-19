# WP-01C C4 六节点契约

## 范围与改动

本批实现 WP-01C PLAN C4 的 N01–N06 数据契约、感知记录和观察批次，复用 A/B 的 Fact、Producer Authority、ToolPlan 与定义引用。代码位于 `src/jimu_runtime/domain/nodes/`、`runtime/perception.py` 和 `runtime/observations.py`。三个定向测试文件位于 `tests/{unit,contract,negative}/`，固定样例及其他 C4 fixture 的有限复用位于 `tests/fixtures/wp01c/nodes/cases.py`。改动新增契约，既有 A/B 持久化 wire 未改动，无数据迁移。

- N01 以有无可用 ContextSnapshot 表达成功和必需上下文失败，绑定已接收 Task、原图、外部登记版本与 Context Schema。
- N02 以独立 skipped 分支表达空 eligibility。实际 no_hits 要求在非空 eligible 集合完成发现，ProfileHit 只携带 Profile/Question 选择及调用证据。调用信心分数保留原始有限数值。
- N03 维持每个 Profile 的 grounding 结果、原图坐标与独立 Object ID。模型与 Tool 来源分别引用对应实际审计，跨记录检查确认准确继承 N02 选择。
- N04 使用一份联合请求配置和一个 Object/Attempt 的实际 ModelInvocation。可信响应建立每个有效 Question 的唯一主 Fact，单个无效 Question 留 error 记录，整包解析或关联失败保留诊断。整批重试需显式引用先前批次，保留独立 Attempt 和模型调用 ID。ObservationBatch 可投影到已有的有限 Producer Authority 输入形状。
- N05 固定 Profile ToolPlan，检查供给的观察 Fact 与选定权威修订，并按已声明步骤验证 ToolExecution 与产出的 Fact refs。输入及通用 envelope 均排除 Candidate、Decision 或规则适用性作为工具来源。
- N06 分开记录提议及 Rule 结果，核对真实 Object、适用性修订、ExecutionCoverage 和完整 mandatory 范围。Fixture 中猜测失败时 mandatory Rule 仍有确定性结果。

## 验证

**TEST_PASSED**。Python 3.12.3 下定向测试 46 项通过，零跳过。Ruff 检查、格式检查和 mypy 均退出 0。确切命令、版本、锁摘要与输出见 [verification.log](verification.log)。Schema 用 Pydantic 生成并由 Draft 2020-12 检查，另验证序列化和 Python 实例复验。C5 负责全库生成索引及离线全量验证。

**RUNTIME_NOT_RUN**。样例使用有限的预建引用和合成模型证据。调用计数的测试范围是给定完整的 Object Attempt 引用集合。实际调度、持久历史完备性、服务跨步限制、真实模型调用/质量与 GPU 容量留待后续运行验证。没有新的权限、凭证、外部调用或迁移。

C4 所属六节点契约的定向验收为 **PASS**。WP-01C 的全局接受仍由 C5 与独立审核门禁确定，之后再按授权检查 WP-02。
