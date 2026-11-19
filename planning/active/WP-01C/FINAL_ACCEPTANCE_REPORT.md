# WP-01C 与 WP-01 工程验收

本次检查基于 WP-01B 的 main 合并提交 `2552affeb69fbc2594fe70724f895b0db0967d47`，在独立的本地 detached 工作树完成。受测产品快照含 608 个文件，清单 SHA-256 为 `a1aec5dc27cc99fa86c1efcfdfc9c026951e865b3e47a5f2a16eeaef4049a4d2`。[冻结快照与摘要](../../implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc/snapshot.json)

## 实际交付

- C 复用 A 的严格基础类型、B 的 Fact Registry、Producer、Authority 与共享条件 AST，补齐 Rule/RuleSet 声明、封闭签名与三种输入域的静态类型检查。
- 新增六节点的输入输出、适用性、覆盖与判定记录，补齐 Release、接收/执行、媒体、调用审计与两类回放的数据契约。代码通过显式传入的实际记录核对来源、归属、Attempt 和选定修订。[契约盘点](CONTRACT_INVENTORY.md)
- 生成 347 份 Draft 2020-12 Schema 和索引，其中 B 的 162 份发布字节与 Git 基线相同。相对合并树新增 304 个产品文件，修改导出脚本、B 回归测试的版本范围和生成索引三个文件，删除 0 个。原始示例及历史审计目录保留。工程调度与状态文档独立于产品快照。[变更路径及修改前后摘要](../../implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc/changed_product_files.json)

## 实测

独立 tester 在 Python 3.12.3、已有 uv 锁定的 CPU 环境中运行计划命令。完整 pytest 的 JUnit 记录 2813 项通过，0 失败、0 错误、0 跳过。Ruff lint 通过，203 个维护文件通过格式检查，严格 mypy 检查 84 个源文件通过。两份临时导出逐字节一致，仓库 `--check` 通过。验收前后 608 个产品文件均与快照相符。[逐条命令](../../implementation_evidence/WP-01C/independent/final_syxjvrqz/commands.json) · [JUnit](../../implementation_evidence/WP-01C/independent/final_syxjvrqz/pytest.xml) · [整轮结果](../../implementation_evidence/WP-01C/independent/final_syxjvrqz/run_result.json)

主控另行运行 74 项跨模块用例，核对了多绑定 Profile 发现、同一 Object 的覆盖结果、可选 Fact 的权威来源、同 ID 内容替换与修订历史。原始示例中的任务与判定 JSON 由 Pydantic 和生成 Schema 双重拒绝。所给 YAML 片段不能直接用作完整 manifest JSON。[集成复测](../../implementation_evidence/WP-01C/primary_integration_NMBXibrR/integration_commands.json) · [原始示例结果](../../implementation_evidence/WP-01C/primary_integration_NMBXibrR/starter_examples_result.json)

## 兼容、安全与交接

- N02 多次调用按 Profile 的绑定保留。Coverage 关联选定的节点输出、完整调用集合和实际 GroundingRecord。单个 Profile 的问题选择记录仍引用负责该 Profile 的发现调用。N06 的必需及已使用的可选 Fact 都要对齐声明和选定来源。[决策证据](../../implementation_evidence/WP-01C/c4_decisions/README.md)
- 系统输出可附加显式权威见证。已有审计事件可读取，缺见证的记录无法单独证明 Fact 权威。静态检查依据调用方提供的有限历史。真实存储的历史完整性与 CAS/fencing 由持久工作流保证。[运行记录与权威校验](../../implementation_evidence/WP-01C/c4_runtime_authority/README.md)
- 几何资源支持带来源 pin、有界二维单环多边形。推理回放允许使用新的 Release 或模型配置，同时保留原 Task 输入摘要。模型检验仍负责 JSON Schema 无法表达的数值语义和关联关系。首个契约版本没有生产迁移；后续不兼容 major 应以显式版本和独立读取路径保留历史数据。[几何与回放](../../implementation_evidence/WP-01C/c4_geometry_replay/HANDOFF.md)
- 外部集合有显式工程上限。准入层仍需约束原始字节、压缩展开、签名与信任来源，部署环境单独审查真实密钥、权限和预算。原有 B 补充 exporter 脚本 mypy 诊断与本次必测的 src 全库 mypy 分列。[集合边界](../../implementation_evidence/WP-01C/c4_release_limits/README.md)
- 此包不含数据库迁移、服务执行或 GPU/云推理。测试使用确定性数据。真实场景效果、恢复、供应商兼容性与部署条件均待各自工作包验证。工程验收与用户验收、Git 发布分别登记。

## 验收决定

| 轴 | 结论 | 依据 |
|---|---|---|
| Code Acceptance | PASS | 主控检查源码、产品差异、权威校验及跨模块整改。 |
| Test Acceptance | TEST_PASSED | 冻结产品完成全部 CPU 门禁。 |
| Independent Review | PASS | 独立 tester 对 G0–G12 逐项核验，I-01 至 I-03 已闭合，有可复核的负例。 |
| Runtime / Quality | RUNTIME_NOT_RUN / UNVERIFIED | 本包验收领域和静态契约；真实服务、推理质量和部署另行实测。 |
| Overall Engineering | PASS | WP-01A/B 的已交付契约与 WP-01C 收口。 |
| User Acceptance / Git | 待单独确认 / 本包未发布 | 本地技术交接按平台持续授权进行。 |

[独立验收报告与逐项证据](../../implementation_evidence/WP-01C/independent/final_syxjvrqz/ACCEPTANCE.md)的 SHA-256 为 `2937d9587f1ef47939bc3a03352432e1a9a5d30730f59cf09d0a4c6f39157b09`。推荐的下一个工作包是 WP-02，输入为上述 Git 合并基线及已验收本地产品快照。

G1 的[冻结后逐项对照表](../../implementation_evidence/WP-01C/CONTRACT_CROSSWALK.md)列出 C1-01 至 C1-05、F01 至 F05 的落地模块、根或嵌套宿主、正负样本及归属边界。
