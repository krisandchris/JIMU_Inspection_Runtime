# 场景计划定向复核

纯谓词求值已按独立前置放行。工作流接入依赖正在集成验证的 WP-04。两位只读审查者分别核对了实际 Worker、模型和工具契约。

| 审查者 | 发现 | 收口依据 |
|---|---|---|
| 独立工程审查，gpt-6.2-sol / medium | Gate 调度、N04 请求证据、Authority、Coverage 及 Finalization 仍有 WP-03 mock 假设 | 指定正式接入单写者，保留历史消息读回，真 Worker + 回环收包 + CPU 工具 + PG 验证。 |
| 同上 | N04 整包 Schema 可能误吞单题错误；N06 可选候选失败会误阻断 mandatory Rule | 区分外信封和题槽，处理整批失配及单题错误。测试候选缺省、超时、不可用和 F AND U 同缺证 guard。 |
| 同上 | 示例 YAML 缺少可编译产物，原图与区域几何缺少可验证转换、掩模与实际字节保存 | 使用现有 BundleCompileInput、能力注册、资产摘要及区域标定；首个场景采用 all_declared、模型定位。 |
| 原生 planner，gpt-6.2-astra / xhigh | 中途重开与已完成的历史查询是不同路径；更换同 ID 宿主配置必须有重复可验证结果 | 增加三个断点、换 Release/宿主修订、旧执行原证据重开、撤销拒发、旧版缺证记录回归。 |
| 同上 | 原始 wire、解析后的内容、批次权威与工具局部输入需要各自的可追溯凭据 | 增加整份实体摘要、整批重试及跨轮混用拒绝、精确祖先掩模和多步骤复用输入校验。 |
| 同上 | 真实环境、标注质量与本地成功应单独报告 | 列明实际端点和进程观察、数据分层与各节点指标；需要业务阈值时只等待质量验收。 |

主控已检查对应模块和规格并将修订并入 [PLAN.md](PLAN.md)。在开工前需检查 WP-04 冻结后的接口是否与计划相符；接入测试以实际入库的证据和物理收发为准。

原生只读 planner 增量核对后确认单谓词求值不依赖实际 Provider、Tool、DispatchJournal 或 WP-04 的任何产品接口。主控已验证封存清单及全部解包字节，并在单独目录放行这项开发。使用处必须绑定受检的同一表达式、输入和 Schema；仅有 PassedTypeCheck 标志不足以授权解释任意表达式。缺证 guard 和业务 Decision 的验收保持独立。

WP-04 冻结后，planner 独立核对已有四份示例、产品编译与准入入口及工具字段独立省略要求。确认场景配置仅依赖已封存的产品。旧 compiler 测试夹具只有一个布尔 Question 和一个布尔工具，并且依赖仓库内测试路径。新场景应交付独立可加载的源文件、构造 BundleCompileInput 的最薄入口和受控几何字节。验收应从独立目录打开源包，逐项核对三道 Question、两份掩模到几何步骤的绑定，以及实际签名和重开。示例只给出 parked 与两种 wheel_boundary_relation 的合取，真实业务区域的容差、阈值和冲突优先级待现场确认。

独立核对已到达 Rule 的 guard 依赖后发现，validate_runtime_authority 验证有限历史并给出 selection heads，validate_decision_guard_authority 只核对提交的原件和头。构造器还需关联每个具体 selector、selected_result_ref、selected_attempt_id、所消费的 Fact 及 FieldPath。终态 Decision 的既有校验器检查 guard 中的类型、作用域、proof 和 finalized 状态。缺失的可用性保证来自受控内容读回和确切生产链。整合时用同次一致读的数据库输入限制历史切片；纯计算可以独立实施。

对单谓词实现的实际差异已完成原生审查，主控独立读取并核对两项输入错误的修复。整改后的 63 项新增测试、格式、Ruff 及 strict mypy 通过。后续集成人使用[已封存的七文件交接包及日志](../../implementation_evidence/WP-05/predicates/README.md)，并在最终集成快照上重新验证。

媒体接线复核定位到三个需要实际读写的边界。IntakeHost.verified_original 已能分派作用域，WorkerHost.worker_source 已提供 MediaAsset，现有 HTTP 发送端只复核格式标识、长度及摘要。上传的原始图在受理前需要独立的 project/client 身份绑定、真实像素解码和有界读取。已选 Tool 步骤的输出可沿原有 Attempt 数据追溯。受管 CPU 工具的 stdout 仅含结果 DTO，二值掩模需经可信适配器实际取回、记录 EvidenceAsset 并供下游重新读取。几何步骤使用上游两种掩模的原始 retention 和 producer，必须用确切前置步骤、作用域、所选结果和命名证据槽证明复用；其新增几何证据继续按本步 Policy 校验。模型 HTTP 回包的完整原件、局部解析结果与省略原因分别绑定到已有 dispatch 记录，覆盖取消、失败和迟到。
