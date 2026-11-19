# WP-03 独立验收范围

工作树在 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 上，初查已有 16 个已跟踪文件修改、302 个未跟踪文件。代码范围为已验收 WP-02 之上的 WP-03 工作流、SQL 迁移、依赖锁及其测试。以主控的 WP-03 产品冻结清单作为最终验证对象。早期预检证据单列。

遵循现行授权记录，仅在本地开发树及全新、专有的 PostgreSQL 16 容器中执行。固定本机镜像 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`，只发布随机 loopback 端口，分离迁移 owner 与应用角色，使用一次性凭据。清理只涉及本次创建的容器和临时合成资料。真实 API、GPU、生产数据库、业务数据和 Git 发布均在隔离验收之外。

| 门槛 | 独立观察证据 | 失败条件 |
|---|---|---|
| 迁移与权限 | 实际 PG16 版本；owner 应用迁移后重跑；应用角色尝试 CREATE、历史 UPDATE／DELETE／TRUNCATE、改写迁移登记；错误 SQLSTATE | 应用身份越过权限；未经识别数据库接受迁移 |
| 接入与 pin | 本地 WP-02 编译、签名、准入、批准及激活；公开 Intake 重复提交；作用域拒绝；现有接入中断回归；SQL 载入原 pin | 绑定改版、重复写入、跨作用域读取 |
| 抢占、续租和追溯 | A、B 为各自启动的 worker 进程；读取 PG 真实时钟与 fence；A 续租后 B 仍无法领取；过期后旧 A 丢失执行权，B 拿到更大的 fence；迟到只进审计；核对历史 Attempts | 双重权威终结、用本地时钟代替 PG 时钟、覆盖原 Attempt、迟到输出进权威结果 |
| 六阶段正常场景 | 两 Profile、至少两个 Object；逐节点及实际对象连接：N01 Snapshot → Gate → N02 Hit/Question → N03 Object → N04 联合请求/Question Fact → N05 ToolExecution/Tool Fact → N06 Decision/Coverage；所有 pin 自同一 Execution | 缺阶段、脱链或串用记录；合成结论被当作真实业务验证 |
| 空、局部和失败场景 | 真实 PG 记录空 eligible、no_hits、no_target、部分 grounding 的生命周期、覆盖与判定；无调用分支无 Attempt | 单纯空检得出 no_violation；低层未见伪造调用；丢失有效子集 |
| N06 延迟发布与恢复 | 在 N06 stage 提交后终止进程，SQL 证实 draft 已存在且还没有 N06 authority/最终报告；新进程发布后核对 Draft/Authority/Coverage/Decision 的 exact refs，结束后再次启动检查稳定性 | 提前发布、失去 draft、发布两次、孤儿引用、虚假 exactly-once |
| 冻结回归 | 全量 CPU/PG pytest、Ruff、strict mypy、独立生成目录逐字节核对、冻结清单前后复验 | 失败、未解释跳过、生成漂移、产品字节变化 |

有界风险包括本地 Docker 可用性、调度时序和未冻结接口。时序探针以数据库时间、进程管道及锁屏障确证。若预检失败，保留原始退出和输出并区分环境、探针和产品问题。

## 复现与证据

- `probe_wp03.py` 由父进程通过管道驱动多个独立 Python worker。采用真实 `SIGKILL`、数据库连接 ID 和 PG 时间。进程通信及夹具配置不写数据库连接凭据到证据。
- 正常成功路径用两个 Profile 和每个 Profile 两个不同 Question。覆盖核对读取 SQL 中实际持久化的 Envelope、Fact、ToolExecution、Decision 和 Coverage，并独立计算 RFC 8785 SHA-256；关联核对使用精确作用域和键。
- 第一次模拟 N02 返回后被终止，第二次执行新调用，PostgreSQL 只选择一个 Attempt。独立冻结验收同时检验三份持久 N06 草稿的延迟发布和切换活跃 Release 后按原 pin 恢复。命令输出可确定真实 PG 与合成 Provider 的范围。
- `run_acceptance.sh` 接收主控冻结清单路径与 SHA-256，依次记录清单前后校验、独立 PG 探针、全量 pytest、Ruff、strict mypy、当前 Schema 检查、独立临时目录生成与逐字节对比、产品与 WP-02 差异。每个步骤单独留存命令、UTC 时间及退出码。
