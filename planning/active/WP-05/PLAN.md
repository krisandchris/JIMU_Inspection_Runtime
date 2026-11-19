# WP-05 非机动车违停巡检

状态为分批实施。纯谓词求值只依赖已封存的 WP-01/02 合同，可以独立开发。场景适配器、Worker 与端到端验收以经独立验收的 WP-04 产品快照为上游。用户已指示继续本地开发，真实环境授权后补。

## 入口、交付与边界

从可信提交的一张 RGB 图片、设备上下文和已准入的场景 Bundle 开始，通过正式 Task intake、PostgreSQL WorkflowWorker 和固定的六个业务节点产出可查询的 Decision。每个结果可沿用户范围、Execution、Attempt、证据摘要及所用 Release 回查。输入图片应从受控资源存储按哈希和大小取得。外部地址、调用方提供的模型响应或未验证尺寸都不能代替输入证明。

场景包以 examples/nonmotor_vehicle_illegal_parking 的三道 Question、三个工具步骤和示例规则为联调目标。原文件保留作背景资料。获准投产的巡检口径、边界容差、标注真值及质量阈值属于单独的业务验收。区域坐标系、停车区几何、单位、版本或证据不全时，记录缺口并维持不确定结果。新增场景资产必须可被现有 WP-02 编译、验签、准入和按 Release 重开。

主开发会话默认使用 gpt-6.2-sol / xhigh，主控负责接口、审查、封存与验收。专项实现子任务按项目约定使用 gpt-6.2-sol / medium。只读 planner 使用 gpt-6.2-astra / xhigh，R2/R3 实际代码差异交原生 code review。开始时记录会话实际生效设置。WP-06 负责正式回放、评测和坏例管理；WP-07 负责对外服务和部署。本包只建设两者后续可消费的运行证据。

## 实现顺序和写入归属

| 分工及主要目录 | 直接依赖 | 交付与接口 |
|---|---|---|
| 场景资产与配置，examples 下独立的可编译场景目录 | WP-02，WP-04 冻结的能力与部署声明 | 精确绑定 Entity/Profile、Facet、Question、工具 Schema、Producer、策略、RuleSet、Prompt、几何和模型角色。逐字节编译，替换其中任一依赖必须触发相应的回归或拒绝。 |
| 公共表达式解释及 N06，src/jimu_runtime/inspection/rules 与对应 tests | WP-01B/C 的 AST、已验证类型、Producer/Authority 和 Decision 契约 | 使用相同操作符表；比较、集合与逻辑的 T/F/U；分别实现元数据适用性、必需证据检查、规则条件。输入明确限定到当前 Execution/Profile/Object，按已准入的版本解析字段与 Fact。 |
| N01–N04 场景适配，inspection/perception 与 tests | WP-04 网关、响应证据、场景资产 | N01 固定 Context 后由内部 Gate 筛选。N02 仅输出已注册 Profile 和有效 Question 集，N03 独立定位并原样继承。N04 每个 Object 正常路径一次联合请求，逐题验证并生成有权威来源的 Fact。禁止从 fixture 或预置结果补回失败的模型输出。 |
| N05 与坐标证据，inspection/geometry 与 tests | WP-04 工具 Registry、资源准入、子步骤完成记录 | wheel_segmentation、tactile_paving_segmentation 按配置执行，wheel_boundary_geometry 读取其固定产物及已验证 Region。保存原图、掩模、变换、工具产物及摘要；以源图坐标计算，限制尺寸/几何复杂度。掩模缺失、损坏、坐标不匹配、边界情况按显式配置处理。 |
| 工作流接入，inspection 和 workflow 的公共 worker 接口 | 上述四项及 WP-04 journal | 由主控指定单个集成人维护共享文件。正式路径使用真实适配代码，测试以受控回环模型 endpoint 和 CPU 工具提供物理输入输出；保留 WP-03 的 mock 回归入口。 |
| 独立验收，planning/implementation_evidence/WP-05/independent | 冻结后的产品、资产、测试、锁文件 | 从新进程运行端到端、错误注入、历史重开、独立输出核对；对实际发布路径及导入边界检查。真实 Provider、真实标注集和目标主机另记验收状态。 |

## 规则、覆盖与对象身份

N06 接收 N05 已生成的权威 Facts。候选预测是可追踪的独立来源，漏提或空列表仍要扫描全部 mandatory Rules。Profile → RuleSet → Rule 有序门控遵循已有 decisive ancestor 约束；UNKNOWN 留下未解除义务。Strong Kleene 只用于单个 AST 内。必需 Fact 或证据缺失、未选择权威 Attempt 或 Scope 不符，不能给出确定否定。保留每条命中、排除、阻塞和未到达规则，按配置 collect_all，维持对象级和任务级覆盖区分。no_hits、定位无目标、定位失败、部分覆盖各自可回溯，部分覆盖下已证实的违规保留。跨 Profile 对象身份只按 D10 的已批准策略处理。

## 可验证验收

1. 在隔离库从真实入口提交合成 RGB 样本；从固定 Release 和实际工具注册重新加载，依次核对每个节点输入、输出、调用数、物理响应和证据字节。覆盖多对象、不同车辆类型、停放/非停放、不同区域关系和不确定案例。即使候选模型漏提，也能检出夹具中已确定的违规。
2. 为所有已注册操作符建立正负向和三值真值表，覆盖 Record 路径、单位/枚举、零、false、空值，验证已选 Attempt 与 Fact/证据组。逐条验证失败的 guard、父级 UNKNOWN、同轮多个命中、漏选候选、覆盖不全均生成契约规定的轨迹。
3. 注入非注册 Question、定位失败、空对象、联合响应缺题或多题、跨对象挪用结果、无权威 Attempt、原图/掩模损坏、变换遗漏、依赖步骤失败、重复提交和丢失 lease；核对独立证据、重试后的物理调用与权威结果选择。
4. 独立检查每个已知 Decision 的 Task/Execution → Object → Fact/Producer → ModelInvocation/ToolExecution → Evidence/原图链。重启进程、切换当前 Release 后能按旧 Execution 读取原始结果。对权限外 Scope 拒绝访问。
5. 锁定环境运行 pytest、Ruff、strict mypy、Schema 重导出及逐文件摘要检查。分别列出代码、隔离运行、目标环境、模型质量与用户验收的结论；代表性实拍标注和真实模型未到位时保存待验证项。

回退以已封存的 WP-04 版本和原六节点测试路径核对兼容性，保留历史运行和已激活 Release。场景版本的替换仅影响随后按新 Release 受理的任务。正式验收前再次核对 WP-04 最终公共接口、测试基线与场景资产，按路径记录实际文件变化。

## 定向复核后的实施约束

独立 reviewer 对照 WP-03/04 的真实路径发现几处 fixture 假设。集成负责人须逐项交付以下回归，再将场景接到正式入口。

- 提取 worker_schedule 的 Profile Gate、worker_validation 的证据核对以及 worker_completion/worker_coverage 的权威与覆盖投影。保留历史 mock 数据的读取与验证。正式路径从固定 Policy 和输入得到结果；在总线事务中只运行确定性计算。
- 使用 BundleCompileInput、SourceArtifact、ReferenceSlot、SourceAssetBytes 构造首个场景包，由现有编译器派生 ProducerCatalogue。首个夹具用 all_declared 及显式注册的 N03 模型定位；其他策略只按已有契约和声明能力运行。Domain 中的 unknown 通过 Fact status 表达。新资产包含两种分割输出及几何步骤的类型化映射。运行按既有 D10 Profile 隔离语义检验。
- N01 写入解析过来源与版本的 Task、设备、相机和 Region 元数据并保留缺项。N04 初版用已验证的原图和 Object bbox；若传裁图，须验证派生图、原图、变换链及其范围。联合响应先做有界信封和 Object 关联校验，再对题槽独立校验；缺题、单题坏值保留合法兄弟。重复键及伪造 Object 使关联无效。批次权威显式选择该次 Attempt，保留失败和 fallback 的物理记录。
- 图像、掩模、原始受限响应等大对象由宿主配置的内容存储读写。实现原始字节有界解码、摘要校验、Scope 和尺寸匹配、先暂存后数据库引用、失败孤儿补偿、跨进程重开。按最小权限读取，日志仅留诊断与引用。对境外或真实 Provider 的授权与存储授权分别处理。像素坐标与区域单位的转换只能用已验证标定；无法证明同一平面时产生不确定几何结果。
- DecisionMessage 与终态验证支持候选未配置、不可用及超时的确定性扫描路径。为三种情况分别计数实际调用并检查 mandatory Rule 全集，保留候选调用失败证据。不得为未派发候选生成虚构的 ModelInvocation。

### 断点重开与场景验收轴

- 为相同设备/相机 ID 更换修订，在 N01 完成、N04 完成、N05 部分完成三个位置分别终止并重开服务；期间激活 Release B。恢复时仍以 A 的 Snapshot、范围和输入字节重建，校验历史能力证明和当前撤销状态。被撤销的能力记录拒发，不得替换成 B；新任务单独验证 B。WP-03 旧数据库历史行可读取，缺失证明时不授权新的真实派发。
- 从安装产物的正式入口启动，在缺少 Provider/Tool 配置时明确诊断。对照 PostgreSQL 物理调用审计、宿主回环实际收包、CPU 工具实际启动及版本，证明每个 Node adapter 的来源。单个坏题、整批关联失败、候选不可用、工具拒发和已发丢失响应分别单测。完整条件包含 F AND U 的谓词结果与不完整 Evidence Guard 同存，缺证据时保持 uncertain。
- 真实环境获批后记录样本、标注协议、独立复核人、Bundle/模型/工具版本与每个分母。按 N02 Profile 召回、N03 定位 IoU/召回、N04 分题准确率与覆盖率、N05 几何一致性、N06 规则一致性、任务级精确率/召回率与错误放行率报告。分别分层报告无目标、困难样本、未能判断和调用失败，保留样本量与不确定性。质量阈值须由业务方确认。

针对前轮、证据及依赖重建增加三项回归。

1. N04 全批失败时保存实际 Attempt、调用和失败依据。新进程按相同有效 Question 集重试整批，读取前一批并检查 retry reason。每轮仅一次正常发送，仅成功且显式选择的一轮可提供权威 Facts。拒绝拼接两轮、引用另一 Object 或使用已丢失 fence 的输出。
2. 从配置的 ToolPlan input_bindings、当前 Object、Context 和精确选中的祖先结果重建实际输入。按声明分别验证缺失阻断或允许省略。跨 Object 的同形掩模必须拒绝；重新选择已完成子步骤前需重验全部输入摘要。
3. 将受限响应原件与解析后的内容分别标识，验证前者与该次 dispatch 的完整 HTTP body 摘要一致。对完整但无效、超限、中断与疑似含凭据的响应分开留证。未保留的原件不得用重新序列化的内容补齐。

### 首批独立开发与集成条件

已独立核对 10 个上游 Domain/Bundle 接口及 668 文件的 WP-02 封存 ZIP，摘要清单 844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556。主控依据本地持续授权放行单个谓词求值。独立目录 JIMU_WP05_predicates_nw7hqmx7 使用解包后校验的 WP-02 原件。单一负责人仅写 inspection/rules 包及专属测试。交付为已验证类型和精确引用下的 ConditionExpr 求值并返回现有 PredicateRecord，涵盖 10 个操作符和 Strong Kleene 逻辑；不决定 authority、coverage 或 Decision，也不连接 Worker。Git 工作树中 WP-04 产品文件由其原维护者独立冻结。此批先独立测试与代码复核，经核对的新增文件在 WP-04 冻结后集成，再执行整包回归。

### 可编译的本地违停场景

WP-04 已封存 784 个产品文件。场景配置依赖该版本的能力声明、具名工具输出约束和 WP-02 编译/准入接口。主控已在独立目录 JIMU_WP05_scene_vy589yvx 解包并逐字节验证产品，再从 Git 核对四个原始示例；独立运行套件同时在封存工作树验证网关和 CPU 工具。场景负责人只写 examples/nonmotor_vehicle_illegal_parking/compilable/，将新配置作为独立产品输入，经正式编译、签名、准入和重开验证，示例可从其他工作目录运行。测试环境临时生成签名凭据。与六节点 Worker 的集成在 WP-04 本地验收后开展。

三道 Question 均属于首个示例固定的问题集。示例违规规则同时检查权威 parking_state 和 wheel_boundary_relation。vehicle_type、parking_position 提供观察证据。几何资源使用有版本的本地合成坐标；真实区域、容差、边界重合或多种关系并存时的业务裁决沿用业务确认流程。

WP-05 产品封存将显式收录 examples/nonmotor_vehicle_illegal_parking 中的场景、资产及专属回归。WP-04 冻结清单只覆盖 src、tests、schemas/generated、scripts、依赖声明和锁文件，复用它检查上游字节时另列示例清单。独立 runner 需显式执行 examples 下的测试。N04 的联合响应 Schema 通过真实编译入口核对三个具名槽、额外字段与局部失败。负向覆盖具名工具输出无法独立省略、上游 mask 身份不匹配、枚举 Producer 错配、几何资产篡改和签名失配。

### 单个已适用 Rule 的必需证据检查

另一名负责人在独立副本 JIMU_WP05_guard_n58owpts 的 inspection/rules/evidence_guard.py 和专属测试中构造既有 RequiredEvidenceGuard。输入为固定 Catalogue 中该 Rule/EvidencePolicy、当前 Execution 和 Object、同次验证的有限权威历史及按来源取得的 Evidence。调用方负责在同一数据库快照读取历史头。构造器用 validate_runtime_authority 核验原始记录，按声明 selector、scope、精确生产者、结果与 Attempt 检查每个 Fact 和指定 Record 字段。相同 Fact 的不同字段各留 proof，Fact/selection 引用去重。已消费的 optional 输入单列。Evidence 来自被选结果、指定的工具输出或原图，核对归属、空间坐标、内容可用性与摘要。保留证据生产时的 retention 身份。最终输出完整或带原因的 guard；数据来源损坏须有明确诊断。此批仅依赖已冻结的 Domain/Bundle 接口，正式数据库读取和节点接线在 WP-04 验收后进行。

### 受控媒体与证据字节

在独立 WP-04 冻结副本中实现 inspection/media、追加迁移 007 和专属测试。媒体口负责原始 JPEG/PNG 的单帧 RGB 完整解码、尺寸/像素/字节上限、确切摘要、存储版本和 project/client 所有权；掩模需核对二值像素和原图变换。Pillow 仅供本地 CPU 解码，按已审的窄版本范围锁定；数据库端口沿用现有 PostgreSQL。上传原图在 Task 前可登记，后续通过原有 IntakeHost/WorkerHost 取得。新派发的原始、解析及派生字节按可信 dispatch 和角色关联；成功子步骤的事实和证据元数据仍使用既有 Attempt 归档。新增内容并发与崩溃恢复、同身份异内容、跨 client、错误尺寸、掩模互换、EXIF 和不完整响应均须有回归。迁移只在隔离数据库实际运行。共享 Workflow、Gateway 和 Tool 调用点由集成批次单写。
