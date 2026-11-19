# WP-02 Bundle 编译、准入与 Release

## 基线与交付边界

WP-01 工程验收已交接。沿用本地工作树，Git 对象为 `2552affeb69fbc2594fe70724f895b0db0967d47`，其上的 WP-01C 产品清单 SHA-256 为 `a1aec5dc27cc99fa86c1efcfdfc9c026951e865b3e47a5f2a16eeaef4049a4d2`。签出 Git HEAD 本身不能重建这个尚未发布的基线。[工程报告](../WP-01C/FINAL_ACCEPTANCE_REPORT.md) · [产品清单](../../implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc/product_sha256.json)

交付本地可调用的编译、独立准入、不可变制品保存和 Release 变更接口。以 [Bundle 规格 §2–12](../../../specs/02_Runtime_Bundle_Specification_V1.0.md) 和 [Domain §9–14](../../../specs/01_Runtime_Domain_Model_V1.0.md) 为合同。实现者、测试者、修复者一律 `gpt-6.2-sol` / `medium`，共享写入按下表分配。独立 tester 在冻结快照上验证。隔离测试使用合成配置、临时本地目录与明确的测试信任域。数据库工作流、API、设备接入、真实部署和业务审批的取得各归相应工作包或负责人。

## 依赖、文件与入口

| 批次与前置 | 单一写入责任 | 交付与可验证出口 |
|---|---|---|
| B2.1 字节和清单，依赖 WP-01C | `src/jimu_runtime/bundle/{__init__,integrity,archive}.py` | 严格读取 ZIP 的受限成员，按调用方显式预算检查压缩包、实际展开字节、单文件和文件数。实现 JCS、重复键拒绝、有界深度、规范词法路径、指针解析及 §4 的文件/制品/根三种摘要。正常 API 从最终字节重新求值，不依赖 compiler 内存。 |
| B2.2 制品与引用，依赖 WP-01C，完整接入依赖 B2.1 | `bundle/{artifacts,catalogue,closure}.py` | 支持全部 25 种 ArtifactKind 的规定路径、聚合/单文件定位、JSON Schema/二进制资源。按确切内容与声明身份和 ArtifactKind 解析。解析所有配置/资源/模板/Schema 依赖，独立校验双向缺项、类型、版本、摘要、歧义和环，派生反向关系；运行身份、外部能力和 IdentitySelector 分别处理。 |
| B2.3 语义编译，依赖 B2.2 | `bundle/{semantics,compiler}.py` | 公开 compile 接受经批准的有限源制品集、显式配置及构建元数据，导出可移交的 ZIP 字节和编译报告。按 §5 的 14 步重用 B/C Registry、ToolPlan、Policy 和 RuleType 检查；从 Question/Step 派生并验证 ProducerCatalogue，从类型投影重生 child value_schema。潜在适用 mandatory Rule 与必需 Tool 输入的生产方均须在对应 Profile 配置中可用，conditional/optional 来源不得无证明地满足必需依赖。 |
| B2.4 信任与能力，依赖 WP-01C | `bundle/{trust,capabilities}.py` | 外部注入只读能力清单、信任验证接口、撤销状态、审批/评测记录和 Runtime 版本。比对 required/optional 和版本特性；验证 remote primary、fallback 契约兼容与独立评测。对签名的测试使用隔离的受信/不受信身份、签名字节和撤销记录。真实信任根、生产算法策略、密钥管理和发布方授权待负责人提供。 |
| B2.5 独立准入与交付，依赖 B2.1–4 | `bundle/{admission,store}.py` | admit 只收原始包、部署预算、受信环境及运行身份。重新执行完整性、闭包、语义和能力检查，依据真实验证结果生成 AdmissionReceipt。无法解析 manifest 时返回带来源摘要和原因的结构化拒绝，不虚构 Bundle identity。临时目录中验证内容寻址持久副本、重开读取、重复提交、故障后无半成品。 |
| B2.6 Release，依赖 B2.5 | `bundle/releases.py` | 以 C 阶段 ReleaseRecord、ApprovalReceipt、ActivationRecord 为契约。技术准入、业务批准、按 scope 激活独立处理；由显式传入的作用域优先级和权限确定目标，歧义拒绝。区分幂等重试与同身份冲突，验证回滚及在途 Task 的固定版本，后续持久调度属 WP-03。 |

每批独占相应的 tests/unit、tests/contract、tests/negative 下 `test_wp02_<批次主题>*.py` 和 `tests/fixtures/wp02/<主题>/`。B2.5 的实现者拥有整包集成 fixtures，B2.6 另有生命周期 fixtures。共享实现位于应用层 bundle 包，只向内依赖 Domain。主控维护唯一计划、证据和最终报告。

## 合同挑战与校验

1. 同一制品在聚合文件中的摘要与文件摘要不同。用独立预期的 M、C、字节长度帧和根向量覆盖固定输入；换 JSON 键顺序、压缩元数据、签名记录不改变逻辑根，改变任何受签元数据或资源字节会改变根。变更创建时间是构建输入变更。校验库存列出的每个逻辑成员和 checksums 指定的每个实际载荷。
2. 所有包字节按不可信输入接收。检查安全路径、链接、重复名称、重复键、无效 UTF-8、非有限/超域数字、有界大小与深度。无网络 Schema 解析，引用限本地可信资源。签名凭据来自独立信任域。编译报告和历史 receipt 均不能替代新准入的重算。
3. G1 按本包实际复用的公开接口建立 typed resolver。已批准的共享条件输入域及本地 Tool selector 在使用位置解析，不能把绑定后的反向 Plan 摘要写回 Policy。公开验收用一套完整非机动车场景合成 Bundle，包含必需的 schema、模型/工具声明及权威 Producer，覆盖 compile → bytes → 从独立输入 admit → 受权批准/激活 → 固定 Task。
4. 保留可替换的签名后端。先冻结本地验证输入/签名信封、测试算法和撤销语义；正式签名配置与密钥由信任域负责人批准。需要外部决定的生产分支单独登记。禁止用调用者布尔值或编译器签发的 `accepted=true` 作为签名证据。
5. 任一失败必须有可复核错误路径，拒绝包不能进入可激活清单。已准入的字节与历史 Release 均不可替换。正确性以离线功能和故障注入验证，服务联通性和平台质量另外验收。

## 验收、回退与调度

逐项覆盖 §12 的 11 个测试，额外覆盖 JSON Pointer 与聚合成员歧义、Schema 局部引用/重生成、无生产者/非 mandatory 来源、错 scope、跨 Plan 祖先、能力撤销和签名信任切换。使用独立固定向量及编译/准入分别拒绝的反例，防止两个入口共享错误而互相印证。同一包在两个干净的临时目录编译、准入。检验历史包和固定 Task 在激活/回退后可读取，临时并发重试不产生两次不同激活。

独立验收保存命令、环境、退出码、JUnit、产物摘要及变更前后清单。运行 `uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q`、Ruff lint/format、mypy src、Schema `--check` 和两次导出比较，同时对 347 个旧 Schema 做字节比较。若 WP-02 的新应用合同需要额外 Schema，单独登记并验证。局部实现可逐批回退，本地存储写入仅限显式传入的临时目录。工程验收和真实外部信任、Git 发布、用户验收分别记录。计划获独立技术复核后，按平台既有授权先派 B2.1/B2.2 与 B2.4 可独立的接口与测试工作。

## 独立 Challenge 后的接口约定

**B2.1 固定向量与边界。** 编译侧先规范化可接受的逻辑输入，准入侧逐文件比较实际存储的 JSON 字节与 JCS 输出。对规范化前超出可精确处理范围的数字、重复键、无效 Unicode 或超过调用方预算的深度直接拒绝并保留路径。复用锁内 RFC 8785 规范化，对有损的整数拒绝；本地锁定 RFC 8785 实现的整数区间为 [−(2⁵³−1), 2⁵³−1]，固定向量同时覆盖两端与拒绝 ±2⁵³。ZIP 元数据和 `integrity.signatures` 不参与逻辑根；以整个 ZIP 的 SHA-256 另外标识持久原件。manifest 本身只删除 §4 指定的两个叶字段后参与根摘要。

**B2.2/B2.3 重算输入归属。**

| 输入 | compile 与 admit 分别如何核对 |
|---|---|
| Definition、Profile 成员、Region 和 Resource | 以 Bundle 中列出的精确 locator 读取字节，从已验证的 kind/id/version/digest 建立目录。枚举所有可能的 Profile/Region 使用位置，按声明的上下文/Region 元数据类型校验。后续真实 Task 的设备和相机数据不参与编译期求值。 |
| SchemaDescriptor、Prompt 与 Schema 资源 | 在明确支持的 Draft 2020-12 受限生成形状上，从实际 schema 投影和已 pin 的 Definition/映射独立构造类型视图。语义注解如需生成源，必须可从包内已覆盖内容或带版本的受信规范恢复，复算对应 Schema 的内容摘要；不能佐证的 Schema/类型视图返回具名诊断。解析本地资源、指针及 `$id`，校验模板变量、生成格式、对外声明和实际使用点。若生产路径依赖现有 dev 组的 jsonschema，应按现有锁版本声明为运行依赖并验证干净无 dev 安装。 |
| Context、Region、枚举与单位 | 按绑定的 context schema、Region 属性、词表、Policy 中的读域与字段路径解析每个使用点。类型、单位、枚举和标量字面量关联需要声明可追溯依据；禁止猜测默认单位或把任意 Context 视图当作已验证。 |
| Producer 与外部能力 | 从 Question 主输出及 Tool step 映射派生目录；系统 Producer 对照外部可信实现和包内要求，验明版本。mandatory/optional 来源取自消费者策略和配置；缺少来源只有在显式可选策略允许时才可降级。逐项补验 WP-01 检查器留下的 deferred 事项，编译报告按检查名标出完成情况。 |
| 受信使用环境 | Admission 接收 host 注入的版本、注册表快照、真实能力与评测引用。SourceProvenance 始终作为声明，环境策略若要求签发方或来源证明，则在缺失或不匹配时拒绝。禁止向需求平台补取配置。 |

**B2.4 签名测试域。** 本地验证以调用方注入的命名测试 profile 显式选择算法、签名前像、证据格式和撤销时间规则；正式信任策略需要外部批准，测试域不能注册到正式环境。签名者先取得已冻结逻辑根，在独立 registry 登记 detached 签名字节，再把其不可变引用附到 manifest。独立 admit 按引用核验取得的证据字节摘要、签名实际覆盖的根、签发者权限、算法约束、撤销状态及显式多签策略。缺签、签在其他根、证据不存在/不可读取、过期或撤销、未授权签发者均须有拒绝测试。签名验证不能由伪造的 receipt 或 caller boolean 代替。

**B2.5/B2.6 持久与授权。** 先暂存并核验原始副本，再原子发布；成功回执关联不可变包字节 SHA-256、逻辑根、验证策略/信任快照和操作身份，重开后可读。相同根的新 ZIP 仍独立准入。覆盖旧 receipt 重投无签同根包、撤销后重投、同 bundle ID/version 换根、故障中断和孤儿收回。持久交付按操作键、参数摘要和期望前驱比较，已完成事务的结果查询可返回原结果，不同参数复用操作键或竞争陈旧前驱则拒绝。每次新的 admit 即便字节与先前完全相同，也独立读取当前信任/撤销状态并全量重验，不能复用旧准入结论。Local 文件存储覆盖单一共享目录内的并发与重开；真实系统持久调度另行验证。

审批和激活操作以受信 principal 对所请求的 action 与目标 scope 做鉴权，须能在本地可信记录查到实际准入和业务批准。单独构造合法形状的 AdmissionReceipt/ApprovalReceipt/ActorRef 不能激活。scope 比较需要调用方提供完整、无重复且无歧义的优先级；测试配置用 device、region、scene、global 的显式顺序，真实环境可另行配置。scene 采用可信 Region/设备解析得到的完整词表成员引用。测试相同优先级冲突、跨 scope 回滚、显式 requested_release_id 绕过授权、撤销对新 Task 的政策，保留原接受 Task 的版本 pin。

上述约定放行已明确的本地字节、闭包和测试接口工作。任何需要新外部 wire、真实密钥或改变业务语义的分支单独登记，工程团队同时推进独立批次。
