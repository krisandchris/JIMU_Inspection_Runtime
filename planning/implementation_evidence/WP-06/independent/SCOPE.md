# WP-06 独立验收范围

独立验收只写本目录。上游产品以 WP-05 正式封存 930 文件清单 `124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad` 为准。待主控提供 WP-06 最终清单后执行正式 runner。集成副本的运行结果作为预检留存。 [WP-06 计划](../../../active/WP-06/PLAN.md) [WP-05 封存独立验收](../../WP-05/independent/FROZEN_REVIEW.md)

| 验收 | 独立证据 |
|---|---|
| WP-05 历史升级 | 逐项核对旧 ZIP，装入独立环境的非 editable wheel。实际应用 001–008 并经正式六节点从合成 PNG 产生 Task 和 Decision。应用 WP-06 009–011 且重跑为空，核对 Task、历史 Execution、Authority、任务与执行事件、job、attempt、工具边界及派发与媒体行。 |
| Artifact Replay | 在安装好的 WP-06 中以两个不同 key 回放，并发重复其中一个 key；只保留两个权威运行。隔离子进程 strace 检查真实 execve，HTTP 回环核对零新请求，PG 全局执行、job 和派发数量不变。重连读取准确的 scoring EvidenceRef 和 digest，确认跨 Scope 以及错摘要拒绝。 |
| Inference Replay | 在激活不同 Release 及相同设备的新修订后，通过正式入口本地受理。并发同 key 只新增一个 Execution 和初始 job，保留同 Task、原 Bundle、原 Context 及历史能力。执行新六节点，独立核对 POST、工具进程、子步骤证据和两组 Attempt 不相交。 |
| 评分及人工复核 | 标签来自固定合成原图、已知像素与独立金标。预测来自实际持久表和可读原件，评测配置来自签名 Bundle，演练独立合成复核身份。核对每个指标的分子/分母、重算与零分母 null，保存合成演算输入与报告。 |
| 证据共享制约及处置恢复 | 以真正重签 Bundle 的一秒合成策略验证历史留存。持有 review hold 时阻止处置；释放后在已提交处置意图并 unlink 的窗口 SIGKILL。重开后看到 pending，通过两个并发恢复求得相同 completion 并能区分 expired，原有 Evidence 元数据可读。共享引用和晚到/缺失/损坏的所有情形另由同一封存产品的契约及隔离数据库测试覆盖。 |
| 封存与回归 | 同一个主树清单覆盖 Git tracked/untracked 产品与公有场景文件，ZIP 每文件核对。pytest 从临时解包运行，加签的历史 WP-01B fixture 单独定锚，完整收集 tests 与 24 个示例用例。Ruff、strict mypy、两次 Schema 重新导出和与上包的 347 Schema 及索引逐字节核对。 |

本地探针沿用 WP-05 已验收的停车场景、受控 Provider 与实际 CPU 工具。真实 Provider、现场图片、代表性标签、业务评分阈值、目标 GPU 和生产数据处置均为 UNVERIFIED。
