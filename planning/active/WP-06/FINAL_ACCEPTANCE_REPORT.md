# WP-06 回放、证据生命周期与评测验收

**本地工程 PASS · TEST_PASSED · 隔离 RUNTIME_VERIFIED。** 主控核对了冻结清单、两份 JUnit 和 41 份命令退出记录。全量 3,959 项通过，零失败、错误或跳过，含 24 项可编译场景测试。79 项 WP-06 测试另有独立 JUnit。Ruff、格式检查、199 个文件的严格 mypy 通过。347 份既有公开 Schema 及索引与前两次封存一致。[独立复核](../../implementation_evidence/WP-06/independent/FROZEN_REVIEW.md) · [JUnit](../../implementation_evidence/WP-06/independent/frozen_run_EJT43OE3/pytest.xml)

| 交付 | 运行验证 |
|---|---|
| Artifact Replay | 用已安装的旧 WP-05 wheel 生成签名场景。升级后两条评分记录均可据 EvidenceRef 和 digest 重读，物理模型请求和工具启动增量均为 0。当前评分协议为 historical_outcome_counts。 |
| Inference Replay | 并发请求仅创建一个新 Execution，沿历史 Release 和输入快照实际发出 4 个回环 HTTP 请求、3 次 CPU 工具调用。源 Task、Attempt、事件及 Decision 保持一致。v1 接受相同配置 pins；跨配置版本比较需要另外版本化输入身份。 |
| Evidence lifecycle | 复核 hold 保护证据，释放后按固定策略处置；实际中断后完成对账。历史元数据和评分保留。过期证据会阻止要求该原件的新评测导出。读权限、范围和字节预算在访问媒体前校验。 |
| 评测与人工复核 | 查看独立真值与历史预测，固定样本、版本、作用域、获准 Bundle、全部实际调用和证据摘要。报告可重算，未观测的放行行为返回未知，零分母不输出概率。合成测试涵盖类别错判、漏检、弃判、抽取 Fact 与实际几何和规则结果。人工复核与真值修订的长期保管由宿主负责。 |

数据库仅在隔离环境演练从 001–008 升级至 001–011，20 组原历史记录核对一致。首次正式运行因为验收器导入排序停止；修正验收器后对同一产品重新执行完整验收。第二次完整运行的 41 份日志均为 exit 0。产品清单 `bfaf2ccec1408421609f7ce2c9a9dbe15987ba27b57ae75f4bf99ea167a154ed` 涵盖 961 项，对 WP-05 新增 31、变更 11、移除 0。ZIP 为 `548fba7da8003e5f6bf7441830f0bcfadbcf30b9c422e20c7eb970ab1e9ee2e9`。全量 JUnit 为 `52eb0415ec18d40e9007690275b0d7bcd00c23e8eac9835b6dbfe0b4322696bc`。[清单及差异](../../implementation_evidence/WP-06/primary_freeze_bfaf2ccec140/snapshot.json) · [真实隔离运行](../../implementation_evidence/WP-06/independent/frozen_run_EJT43OE3/replay-runtime.log)

真实样本、正式误放门槛、付费 Provider、目标 GPU、生产数据处置及业务验收为 UNVERIFIED。Git 发布待授权。后续本地运维组合验收使用该封存基线。
