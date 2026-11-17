## 变更与范围

Runtime 原先只有架构文档，缺少可自动验证的基础类型。本 PR 增加严格、不可变的引用与
Typed Value 契约、独立状态枚举及 CPU-only 工程环境，为后续 Definition / Fact Registry
提供基础；范围仅为 WP-01A。

- Base: `main`，准备时 HEAD 为 `827fa77c9d6a561b0df85d7d93f50d38a912f4ee`。
- Head: `dev/wp-01a-foundation-types`，审查对象固定为
  `d3978c7b07386f9169cc2b43d3e560f908cdb9c3`。
- 73 个新增文件：19 个实现/配置/测试、54 个实施证据文件；既有文件未修改。
- 25 个 ArtifactKind、ContentRef / IdentitySelector / RuntimeRecordRef / SchemaRef、
  Version / SHA-256 Digest、七类 Value 和六套独立状态枚举。
- TypeDefinition / ValueContract 只做纯本地值约束，不包含 Definition/Fact Registry、
  Rule AST/TypeChecker、Bundle Compiler、Workflow、数据库、API、模型或 GPU 服务。

## 验证与实施证据

下面都是已执行并保存的 WP-01A 证据；最终验收材料准备阶段未重跑测试。

| 验证 | 结果 |
|---|---|
| 最终主环境 pytest | 324 passed，0 failed/errors/skipped |
| 全新 public-index CPU 环境 pytest | 同上，324 passed |
| Ruff check / format check | 退出码均 0；14 个 Python 文件格式通过 |
| 严格 mypy | 退出码 0，7 个 source 文件无问题 |
| Pydantic / JSON Schema 2020-12 | 34 份快照校验与重复生成比较通过 |
| 依赖锁 | 两环境 21 个实际安装包版本一致，CPU-only；无 GPU/模型 SDK/服务依赖 |

固定提交中的材料：

- [完整实施报告](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/README.md)
- [验证摘要](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/verification_summary.json)
- [主环境 JUnit](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/pytest_final_results.xml)
- [干净环境 JUnit](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/pytest_rebuild_public_index_results.xml)
- [依赖版本](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/dependency_versions.json)
- [SHA-256 清单](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/after_sha256.json)
- [实施 Finding](https://github.com/krisandchris/JIMU_Inspection_Runtime/blob/d3978c7b07386f9169cc2b43d3e560f908cdb9c3/planning/implementation_evidence/WP-01A/findings.json)

uv.lock SHA-256：`e25b2408a2d4c92b3a6dc157b70b33c4dbbf01dfbb752577480c64000a5bd801`。

## GPT-6 独立审查与验收

用户提供的独立审查结论为 Code Review PASS / Test Evidence PASS / Runtime Acceptance N/A。
未提供独立报告原件或 GitHub Review ID；这里不声称已经存在 GitHub 原生 approval。
Code / Test 技术结论 PASS；Runtime 范围不适用，历史执行标签为 RUNTIME_NOT_RUN。
**用户最终验收仍为 PENDING_USER_CONFIRMATION。**

### 外审 F01：已披露的数值语义限制（非阻塞）

JSON Schema integer 可接受数值 1.0，number 可接受整数 1；严格 Pydantic Integer/Number
拒绝相应的 float/int 隐式互转。实施 README §7 和
`test_json_numeric_mathematics_do_not_define_python_coercion` 已明确披露和测试。
保留已审核代码；后续 Fact Schema 必须保留结构校验 + 严格 Domain 语义校验两阶段，
不以结构 Schema 通过代替类型、闭合 Record、Schema/Scope/Producer/Fact Key 或 Authority 校验。

### 外审 F02：独立发布指令与授权证据范围

原 WP-01A 实施授权不含 Commit / Push。实施结束后，用户另发独立消息，原文为：

> 开发状态设计开发分支提交供GPT-6审核 回复信息中审核通过合并入main

这条会话原文可取得；随后实际创建开发分支、提交 d3978c7… 并发布该分支供 GitHub 审核。
Push 是当时对“开发分支提交供审核”的任务范围解释，原文没有单独逐字包含 Push；
助手的事前说明不充当另一条用户授权。若审查要求额外的逐字 Push 指令，该严格证据项为
AUTHORIZATION_EVIDENCE_MISSING，不能补造。

Git reflog 记录分支创建、提交与 update by push，远端 SHA 对齐；这些操作记录只证明
操作发生，不代替授权。没有平台导出消息文件/消息 ID/用户消息精确时间，不伪造它们。
实施证据中的“未提交/未推送”是发布前快照，保持原样。F02 范围说明仍待用户最终确认。
外审 F01/F02 与实施 findings.json 内同名编号分开解释，不覆盖历史 Finding。

本地最终验收包位于 `planning/acceptance_evidence/WP-01A/`；准备时未提交/推送，
因此本草稿不提供指向这些本地新文件的假 GitHub 链接。

## 合并门禁与后续范围

- 当前只是本地 PR 文案，尚未创建 PR；创建前重新确认 head/base，已有变动须重新审查。
- 用户最终确认 WP-01A 验收及 F02 说明后，再单独批准 PR 创建与合并；技术 PASS 不自动合并。
- 不修改已审核的 WP-01A 业务代码，不重写历史证据；本阶段无数据库迁移或服务部署。
- WP-01B 尚未规划或实施，WP-01C 也未启动。
- 用户确认实际合并后，重新读取 main 的实际 HEAD/合并 Commit 和规定文档，才开始
  WP-01B 计划。计划经独立审查及用户批准后才能编码，不能沿用 827fa77… 作为新基线。
