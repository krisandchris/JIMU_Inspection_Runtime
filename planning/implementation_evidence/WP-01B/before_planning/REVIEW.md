# WP-01B Planning — Primary Review 与 Sol Challenge 处置

基线：main / `51a10416d882fe85e5a1cfc5440eac6498fc48f4`。
状态：`STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN`。
Primary：原生会话实测 `gpt-6-astra / xhigh`；四只读任务：实测 `gpt-6.1-sol / medium`。

## 1. 主控独立检查

Primary完整读取本地规范/计划、A的7个源码文件，直接检查refs/enums/TypeDefinition/ValueContract/CPU配置，以及Schema与import边界测试。通过git和标准库字节读取核对HEAD、全部tracked文件、既有两套local-only证据摘要、历史JUnit。不是只接收Sol文字后就宣布通过。

| 实际检查 | 结论与限制 |
|---|---|
| HEAD/branch/remote/worktree | main、指定SHA、origin目标正确；143 tracked字节等于HEAD，初始11个已知未跟踪文档。未fetch/pull/reset/restore/switch/创建分支 |
| A接口复用 | 直接核对ContractModel、25ArtifactKind/26RuntimeRecordKind、ref wire、七type和值/深不可变；没有重复实现A计划 |
| 数值F01 | 直接核对`test_json_numeric_mathematics_do_not_define_python_coercion`和A证据§7；保持双阶段，不修改A代码 |
| 历史测试 | 解析两份历史最终JUnit：各324 tests、0fail/error/skip；只是读取证据，本轮没有pytest |
| Policy依赖 | Primary在委派前发现必填AST与B禁AST冲突；四Sol审查独立报告相同问题。列B-F01，未以Any占位 |
| Fact生成 | 对照Domain§12.1与Bundle职责、base.stable_json限制，区分Pydantic接口Schema与F04 payload/JCS产物；列B-F02 |
| Authority | 直接核对26RecordKind和Domain§13.2，三组result mapping/资格/支持域尚不足冻结；保留B目标，列B-F03，不因子报告建议而擅自后移 |
| 未冻结字段 | 对照Domain field lists/Policy Catalog/总计划§5–6，分开业务实值OPEN与wire未定义；列B-F04，不改已批准规范 |
| import测试 | 直接核对既有ImportFrom解析确有扩展后的漏边风险，列B-F06测试增强提案；未改A测试 |
| 实施范围 | PLAN target/blocked/support/deferred区分，完整16Policy和八目标未被虚报完成；G0明确blocked；无Rule/Bundle/服务实施 |
| 并发写入 | A–D都只读；文件由Primary串行写入，所有必要子任务已completed |

## 2. 只读任务与原始结果

| 任务 | 输入/输出 | 结果文件 |
|---|---|---|
| A Ontology / Profile / Facet / Question | 规格与A代码；精确交集/Context/ref模型/未冻结字段 | reviews/A_CONTRACT_REVIEW.md |
| B Fact Schema / Producer / Authority | 规格与A代码；JCS边界/碰撞/authority/result引用 | reviews/B_CONTRACT_REVIEW.md |
| C Policy / Tool / Model | 16Policy、Tool mappings、Remote Primary、Prompt/Schema支持 | reviews/C_CONTRACT_REVIEW.md |
| D tests | 现有测试/证据、S/D/A矩阵、import边界、未来命令 | reviews/D_CONTRACT_REVIEW.md |

原始首轮回复逐字保存。部分子任务自身无法验证model而写UNVERIFIED；Primary从本机原生session_meta/turn_context安全白名单直接确认实际值，证据在orchestration_verification.json，不改写其原回复。

## 3. 对具体PLAN的Challenge与整改

首轮被审查 PLAN-1 SHA-256：`fe633537098c658c2fa7701fff045916ec076f928cd9e2886a817fcb6ea62417`。
首轮 FINDINGS SHA-256：`e4306de5231e15d145d1021b55db071034eaaac161f0c5d2fd9ce3203672ca6b`。
C、D完整读取草稿并给出实质问题，原文见 `reviews/C_PLAN_CHALLENGE.md`、`D_PLAN_CHALLENGE.md`；首轮行号对应上述草稿，不是最终文件。

| Challenge项 | Primary处置 | 文档状态 |
|---|---|---|
| C-1 / D-C01：Tool import尚未交付Producer模块，顺序与DAG冲突 | 拆`tool_outputs.py`唯一低层输出声明/映射，B.5a先交付，Tool/Producer共享；DAG/文件owner/顺序同步 | RESOLVED_IN_PLAN |
| C-2：review_metadata输入形状缺冻结入口 | B-F04增加ReviewMetadata/TransitionAssessment字段要求、非持久/可信边界及report不覆盖transition/trigger/scope负例；B.1支持输入、B.6跨模型集成 | RESOLVED_IN_PLAN；字段本身仍待B-F04审批 |
| C-3 / D-C02：describe_type丢optional祖先 | 改为闭合ResolvedFieldType候选，保留type/path/optional祖先；补required叶位于optional父的测试 | RESOLVED_IN_PLAN；字段冻结仍B-F04 |
| D-C03：完整成员不等于可信batch或Attempt有资格 | B-F03增加trusted envelope/association、Attempt→NodeRun→Execution、selection reason资格、late/timeout限制与负例 | RESOLVED_IN_PLAN；资格取值仍B-F03待批 |
| D-C04：Fact ID重绑定可绕复合键冲突 | 同时约束fact_id→不可变candidate与candidate复合键，补同ID异key/bytes、异ID同key异bytes负例；唯一域/bytes判重规则待冻结 | RESOLVED_IN_PLAN；B-F03不虚报关闭 |
| Primary补查：schema descriptor/review输入的文件owner晚于消费者 | 支持输入移至B.1，Tool局部验收与B.6跨模型语义验收分开 | RESOLVED_IN_PLAN |

最终 PLAN-1.1 SHA-256：`38ec63dab5c9c50e4be175eefc7877816ec86a5a714ecb7da4ed7ba69ffedc8f`。
最终 FINDINGS SHA-256：`7f57f9a745384526428b4a22164874c8615ac2175eda1a410aeb9b72a5e7de11`。

C、D随后重新读取并核对这两个最终SHA：
- C：`PASS_STATIC_PLAN`，仅定向整改通过，无新增阻塞性计划缺陷；B-F01–04仍OPEN，不能实施。
- D：整改充分，整体规划`CONDITIONAL`，B-F01–04/G0继续阻塞。

这两个结论不冲突：计划自身缺陷已整改，而待用户裁定的范围/契约缺口未被代理批准。原始最终回复见 `reviews/C_FINAL_RECHECK.md`、`D_FINAL_RECHECK.md`。后续仅写审计封套，不再改被复查的PLAN/FINDINGS字节。

## 4. 自审结论与下一门禁

| 验收轴 | 当前结果 |
|---|---|
| 多智能体配置与只读任务完成 | PASS（实际native spawn/状态/会话元数据证据）；未测试worktree创建 |
| 当前计划交付完整性/边界/Challenge整改 | PASS_STATIC_REVIEW，目标、DAG、Schema/文件/测试/验收与阻塞清楚 |
| WP-01B实施准备 | **CONDITIONAL / G0 BLOCKED**：B-F01–04尚待范围裁定与字段冻结 |
| Code Acceptance | NOT_IMPLEMENTED，不能写PASS |
| Test Acceptance | TEST_NOT_RUN；A历史324passed单独保留 |
| Runtime Acceptance | RUNTIME_NOT_RUN / NOT_APPLICABLE_TO_THIS_PLANNING |
| 用户实施批准 | PENDING_USER_APPROVAL |

实际只运行只读Git/文件查阅、Codex版本/配置协议探测、原生工程只读子任务、Markdown写入、标准库字节/历史XML审计。没有运行项目pytest、Ruff/mypy、项目Schema生成/校验、服务或模型/GPU；没有安装/升级依赖、改平台/架构规格/历史证据、Git Commit/Push/PR/Merge/部署。

只读探测限制也保留：`--strict-config`不支持`codex features`那次探测未验证配置；后来使用支持的app-server `--print-startup-config`实际exit0。导出Codex protocol到临时目录是读取工具配置规范，不是JIMU Schema功能测试。Worktree能力仅验证工具/CLI入口及现有inventory，未创建工作树冒充隔离验证。

下一步需要用户裁定B-F01的AST分期、B-F02的局部生成/JCS职责、B-F03的Authority最小支持范围，并一次性冻结B-F04相关字段；原因来自已批准规格与本轮明确分期限制之间的真实依赖缺口。没有重新要求D01–D15批准，也不逐文件求批。
本轮到此停止；不开始B代码或C。用户业务裁定、具体实施授权、Git操作授权彼此独立。
