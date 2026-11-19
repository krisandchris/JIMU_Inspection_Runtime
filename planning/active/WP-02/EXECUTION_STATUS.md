# WP-02 执行状态

当前在 WP-01B 合并基线 `2552affeb69fbc2594fe70724f895b0db0967d47` 上的 WP-01C 本地工作树开展工作。WP-01C 的 2813 项测试及工程验收已有[冻结记录](../WP-01C/FINAL_ACCEPTANCE_REPORT.md)。本包六批次均已派工。处于实施、回归和独立审查阶段，尚未签发整包验收。

| 批次 | 已交付接口 | 当前工作或证据 |
|---|---|---|
| B2.1 | ZIP、规范化及逻辑根复算 | [完整性测试](../../implementation_evidence/WP-02/integrity/REPORT.md)；主控独立更换 ZIP 编码及原件字节核对根摘要及拒绝结果。 |
| B2.2 | 25 种制品、定位、离线引用及闭包 | [制品交接](../../implementation_evidence/WP-02/artifacts/RESOLUTION.md)，下游已使用。 |
| B2.3 | 源输入编译、Schema 类型视图、Producer 和规则语义 | 编译已严格核对源字节与唯一来源声明。Rule 或 RuleSet 无法绑定到任何可检查使用位置时明确拒绝。[编译证据](../../implementation_evidence/WP-02/compiler/VERIFICATION.md)。 |
| B2.4 | 可复算的签名信任和能力检查 | [隔离验签证据](../../implementation_evidence/WP-02/trust/README.md)已提供给准入负责人。 |
| B2.5 | 原始 ZIP 的独立准入、持久回执、重开和重试 | [存储故障恢复](../../implementation_evidence/WP-02/store/REPORT.md)，准入入口已接入当前信任状态复核。 |
| B2.6 | 注册 Release、作用域批准与激活、回滚、Task 固定 | 已接入本地持久层。Release 和独立验收负责人使用真实 ZIP、隔离验签及新存储重开验证。 |

独立复核发现的技术问题按模块交还原负责人，主控核对修改和回归结果。产品文件有并行写入时，最终全量验收使用共同冻结清单；目录、存储、验签及定向测试可继续独立执行。生产签发方和真实受权主体只影响生产准入。本地工程与隔离验收按[已记录的授权](../PLATFORM_GOAL.md#当前授权记录)连续推进。

## 整包验收交接

产品清单共 668 个文件，SHA-256 为 `7f4005d91fca32246f3344edecc834fa3e2c7ac53214a4dfddb55d88f77457b8`。相对 WP-01C 的冻结清单新增 60、修改 8、删除 0。[变更与基线](../../implementation_evidence/WP-02/primary_freeze_7f4005d91fca/snapshot.json)

主控复跑 Release 的契约、正向和负向测试，13 项通过，覆盖两版 Bundle 的签名准入、Task 固定、回滚和持久重开。[JUnit](../../implementation_evidence/WP-02/primary_release_j3g4uZCp/pytest.xml)

独立验收正在按上述清单检查全套测试、Schema 保真及运行链路。下游工作树可继续使用已验收的本地快照。WP-03 环境预检发现本机已有 PostgreSQL 16 镜像，可在后续测试中新建独占临时数据库，镜像 ID 为 `sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`。

首轮全量测试 3187 项通过、1 项失败。独立索引和调用方目录来自不同实例时，原实现出具了错误诊断。负责人补齐 SchemaIndex 的目录身份检查及直接语义入口的闭包身份检查，并新增无引用场景的回归。独立验收脚本也分别计算 347 份 Schema 与 1 份 index，所有 348 个文件仍逐份核对。[首轮原始报告](../../implementation_evidence/WP-02/independent/FROZEN_ROUND_1.md)

第二冻结清单 SHA-256 为 `844ed4ac127528dad9eead9fb927459fa964ce784d405cb90bd4cb6d464e2556`，668 个产品文件。相对首轮有 3 个源文件和 3 个测试文件变化。当前整包复测使用[第二冻结清单](../../implementation_evidence/WP-02/primary_freeze_844ed4ac1275/snapshot.json)。
