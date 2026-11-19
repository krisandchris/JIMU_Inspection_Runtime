# W3.5 Worker 持久纵链

**TEST_PASSED · RUNTIME_VERIFIED（隔离 PostgreSQL 与合成节点）**

经真实接入接口创建的 Task 固定 Release、Bundle、执行策略及调度预算。Worker 从已授权作用域扫描待领取、过期、取消和待发布的工作。每次处理先查原 Task 对应的 Activation 和批准记录，再按历史 Bundle 字节验证 pin。六节点消费 PostgreSQL 中通过校验的上游记录。租约、Attempt、事件、权威产物和后继写入均受数据库事务保护。N06 草稿可跨进程保留，决策与共享 Coverage 在最终事务发布。

## 实现和契约证据

- `workflow/worker.py` 提供单 Execution 处理、作用域分页轮询、取消、最终封装查询和 typed async 节点实现注入。轮询有界，一个 Execution 的历史来源打开错误会作为该 Execution 的诊断返回，允许检查同页中的其他 Execution。
- `worker_source.py` 通过受信宿主读取媒体和 registry pin，打开原始 Activation 的已批准 Release。`worker_nodes.py` 执行业务节点。`worker_validation.py` 通过公开领域校验器校验输出和确切归属。
- `worker_records.py` 的版本化阶段消息含准确的输入、结果和外部制品索引。合成观察请求字节有显式上限，回读校验大小及摘要。`worker_state.py` 只读取作用域内匹配 Attempt 和权威 envelope 的版本化记录。`worker_schedule.py` 依照纯 planner 同事务发布后继；无调用的跳过或阻塞只记录 NodeRun 和原因。
- `worker_completion.py` 在作业校验通过时持久化类型化制品、Context Snapshot pin 和真实 Invocation refs。N06 草稿提升和覆盖、决策、任务及执行终态写入为单一可回滚事务。
- 使用现有 WP-03 迁移、应用角色和数据表。所有修改均位于独立 Runtime，未改动外部需求平台。宿主提供可信作用域和合成媒体。没有真实模型凭据、真实模型 API 或 GPU 调用。

## 已实际运行

运行环境为 Python 3.12.3、Pydantic 2.12.5、psycopg 3.3.5、SQLAlchemy 2.0.52、pytest 8.4.2、Ruff 0.14.14、mypy 1.18.2。每个 PostgreSQL 测试使用 [W3.1 隔离环境](../postgres/environment.json) 创建的 PostgreSQL 16.14 实例并在退出时清理。

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `.venv/bin/python -m pytest -q tests/unit/test_wp03_worker_postgres.py tests/negative/test_wp03_worker_fencing.py tests/contract/test_dependency_boundaries.py` | 0 | `11 passed in 41.01s`，零跳过。 |
| `.venv/bin/mypy src/jimu_runtime/workflow/worker.py src/jimu_runtime/workflow/worker_{poll,source,records,nodes,validation,state,schedule,completion}.py` | 0 | 九个 Worker 模块严格类型检查通过。 |
| `.venv/bin/ruff check src/jimu_runtime/workflow/worker{,_nodes,_records,_state,_source,_schedule,_poll,_completion,_validation}.py tests/unit/test_wp03_worker_postgres.py tests/negative/test_wp03_worker_fencing.py tests/fixtures/wp03/worker/support.py` | 0 | 作用域内 Ruff 检查通过。 |

覆盖真实接入、单 Object、两个 Profile 和三个 Object、两个并发 Worker、共享决策和 Coverage、空 eligible、无命中、无目标、上下文阻塞、Tool 部分失败、跨作用域拒绝、错误重试、续租、取消、真实晚到制品及来源暂不可用时的分页公平性。

## 限制及下一验收

模块验收 PASS。模型、设备、GPU、性能和业务准确率 RUNTIME_NOT_RUN。进程终止和新进程恢复见[独立 PostgreSQL 预检](../independent/PREFLIGHT.md)。后续 WP-04 继续遵守包边界。
