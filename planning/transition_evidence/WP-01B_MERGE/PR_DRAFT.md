# WP-01B 交接草稿

拟用标题：**实现 WP-01B 领域定义与 Fact Schema Registry**

目标分支 `main`，来源分支 `dev/wp-01b-definition-fact-registry`。准备时通过 `git ls-remote` 确认远端 main 为 `51a10416d882fe85e5a1cfc5440eac6498fc48f4`，开发分支为 `c25bef668136acc651e7313ba608ec6fdefd39a5`。GitHub 对这对分支的全状态 PR 查询返回空列表。开发分支当前远端提交仅含 G1 规划，供审核的实现是本地封存的工作树。发布时需将实际实现提交 SHA 写入 PR。

## 拟用 PR 正文

WP-01B 为配置、Fact 和结果选择提供可在 CPU 上验证的领域契约。一个 Question 绑定一个主 Fact；ToolPlan 的每个 Step 可声明多个 Fact。系统保留各次 Attempt 的候选结果，显式选择 N04 整批结果或 N05 单步结果，能拒绝从不同 Attempt 拼接权威输出的输入。

- 新增 Entity、Region、Profile、Facet、Question、ModelBinding、Tool、ToolPlan 及 16 种 Policy 的定义和引用检查。
- 建立 Fact Schema 注册、确定性投影与子 Schema 摘要校验，核对 Producer 的身份、版本、Scope、Fact Key、Schema 和产出基数。
- 按已批准的两项范围决定复用共享条件 AST 数据，在 `tool_condition` 的使用位置解析同一 ToolPlan 内祖先步骤的局部 Fact 引用。Rule 求值与工作流执行属于后续工作包。

**验证。** 封存工作树上的全量 CPU 测试为 1907 passed，零失败、错误和跳过。Ruff lint 通过，维护范围格式检查覆盖 124 个文件，mypy 通过 48 个源码文件，162 份生成 Schema 和索引检查通过。Astra 和独立 Sol 的技术审查均通过。Runtime 验收为 N/A。

**已知限制。** 补充执行的 exporter 单文件 mypy 报 7 条诊断。历史全目录格式检查命中 3 个封存的审计脚本。源码 mypy 与维护范围的格式门禁已通过。此前误写原 main 工作树的 9 个新增文件已迁回，最终 170 个原文件及文件集合与基线一致。

[验收报告](../../active/WP-01B/FINAL_ACCEPTANCE_REPORT.md) · [测试与审查证据](../../implementation_evidence/WP-01B/FINAL_INDEX.md) · [封存摘要](../../implementation_evidence/WP-01B/execution_03/SEAL.json)

## 提交范围

实现和验收证据以 execution_03 的 `after_sha256.json` 和 `SEAL.json` 为准。交接核对时 1358 个清单条目和封存记录中的摘要全部相符。`planning/active/PLATFORM_GOAL.md` 和本交接草稿为封存后规划材料，独立于该次工程验收。形成提交时按清单选择文件；创建 PR 时使用提交固定的仓库链接替换上文的本地相对链接。合并后以实际 main HEAD 作为 WP-01C 具体计划基线。
