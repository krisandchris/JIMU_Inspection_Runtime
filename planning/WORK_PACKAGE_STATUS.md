# 工作包状态

全平台当前范围、授权与各包交付要求见[平台工作包与授权台账](active/PLATFORM_GOAL.md)。本表保留主要交接点。

| 工作包 | 工程与交付状态 | 证据 |
|---|---|---|
| WP-00 | 架构审查到 R2.1.1 | [整改历史](review_evidence/) |
| WP-01A | 已验收并合并 | [基础类型交付](implementation_evidence/WP-01A/README.md) |
| WP-01B | 八个模块已验收并合并，PR #2 的 main 提交为 `2552affeb69fbc2594fe70724f895b0db0967d47`。1907 项历史测试通过，162 份 Schema | [工程报告](active/WP-01B/FINAL_ACCEPTANCE_REPORT.md)、[用户验收记录](transition_evidence/WP-01B_MERGE/USER_ACCEPTANCE.md) |
| WP-01C | 工程验收 PASS，2813 项测试通过，G0–G12 独立复核通过；用户验收与发布待单独确认 | [最终工程报告](active/WP-01C/FINAL_ACCEPTANCE_REPORT.md)、[冻结快照](implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc/snapshot.json) |
| WP-02 | 本地工程验收 PASS，3191 项测试通过；隔离编译/验签/激活/回滚/Task 固定通过 | [验收报告](active/WP-02/FINAL_ACCEPTANCE_REPORT.md) |
| WP-03 | 本地工程验收 PASS。3363 项测试通过，独立 PostgreSQL 跨进程六节点合成整链已验证 | [最终报告](active/WP-03/FINAL_ACCEPTANCE_REPORT.md) |
| WP-04 | 本地工程 PASS。3631 项全量测试、10 个独立运行探针通过。真实 Provider、GPU 和质量待验证 | [最终报告](active/WP-04/FINAL_ACCEPTANCE_REPORT.md) |
| WP-05 | 本地工程 PASS。3,880 项全量测试、21 条隔离六节点场景、7 条进程终止恢复路径通过。真实推理与实拍质量待验证 | [正式验收](active/WP-05/FINAL_ACCEPTANCE_REPORT.md)、[独立复核](implementation_evidence/WP-05/independent/FROZEN_REVIEW.md) |
| WP-06 | 本地工程 PASS。3,959 项全量测试，真实隔离数据库的两种回放、证据处置和基于冻结真值的评分通过。真实业务质量待验证 | [正式验收](active/WP-06/FINAL_ACCEPTANCE_REPORT.md) |
| WP-07 | 本地工程 PASS。3,988 项全量测试通过，隔离 Compose、Prometheus/Grafana 与跨实例恢复已实测 | [正式验收](active/WP-07/FINAL_ACCEPTANCE_REPORT.md)、[真实环境交接](active/WP-07/FIELD_VALIDATION_HANDOFF.md) |

WP-01B 工程报告在用户验收和 Git 交付前封存，当时保留的 exporter 补充类型检查限制见原报告。后续用户验收与合并以交付记录为准。
