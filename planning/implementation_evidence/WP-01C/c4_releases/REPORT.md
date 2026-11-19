# WP-01C C4 Bundle 与 Release 契约

## 范围与基线

基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。工作树已有 AGENTS、路线图、总计划、平台授权记录和 C1/C2 并行改动。本批依据已审 C4 计划和本地实施授权交付 Bundle、校验和、准入、批准、Release 及激活的纯数据模型和 CPU 定向验证。

## 文件与接口

| 文件 | 接口或证据 |
|---|---|
| `src/jimu_runtime/domain/runtime/bundle_manifest.py` | `BundleManifest`、`BundleRef`、`ArtifactInventoryEntry`、`SourceProvenance`、`SourceArtifactRef`、`CompatibilityRequirement`、`IntegrityEnvelope`、`DetachedSignatureRef`、`ChecksumEntry`、`ChecksumManifest` |
| `src/jimu_runtime/domain/runtime/releases.py` | `AdmissionReceipt`、`CapabilityReport`、`CapabilityAssessment`、`ApprovalReceipt`、`ReleaseRecord`、`ReleaseStatus`、`ReleaseRef`、`ActivationRecord`、`ActivationRef`、`ActivationScope` |
| `tests/unit/test_c_release_records.py` | 文件摘要与 Artifact 摘要分离、不可变 Bundle pin、七种 Release 状态及适用的收据组合 |
| `tests/contract/test_c_release_schemas.py` | 六个持久根的 2020-12 Schema、Wire 往返、缺版本、额外字段及四种 ActivationScope |
| `tests/negative/test_c_release_invalid.py` | 路径和 Pointer、重复或重叠清单、校验和顺序、兼容区间、能力状态、收据关联及激活引用负例 |
| `tests/fixtures/wp01c/releases/` | 四份确定性合成 JSON，收据记录位于 `release.json` |

六个独立持久根为 `BundleManifest`、`ChecksumManifest`、`AdmissionReceipt`、`ApprovalReceipt`、`ReleaseRecord` 和 `ActivationRecord`。余项作为引用、嵌套值、枚举或命名联合供 C5 登记。收据与 Release 的关联、路径与 Pointer 的联合约束、列表去重及状态语义的跨字段校验由 Pydantic 执行。

## 设计取舍与待决事项

- Artifact 内容摘要用 B 的 `ContentRef.digest`，校验和表使用对应文件的 `sha256`，Bundle 身份固定为 `BundleRef`。集合化 JSON 可以为不同 Artifact 使用同一路径、不同 Pointer。规范化路径沿用 B 的字符限制和 512 字符上限。Pointer 最长 1024 字符。部署级归档、文件数量和体积限制在 WP-02 配置。
- 规范 02 §3 同时提到兼容要求内的能力和顶层 `required_capabilities`。契约在顶层存一份 B 的 typed requirement 列表。`CompatibilityRequirement` 单独声明包含下界、排除上界的 Runtime 版本区间。
- `ReleaseRecord` 从已有 Bundle 摘要的 `compiled` 开始，按状态保存匹配的技术收据和业务收据。未编译草稿在该持久根边界之外。激活是独立的四种作用域联合，可保留前次激活引用。Region 作用域用稳定 Region ID，Scene 用 B 的版本化 vocabulary member。权威身份、作用域优先级、跨记录冲突与回滚前置条件由 WP-02 决定。
- 签名声明固定外部证据的身份、版本和 SHA-256。实际签名格式、算法、轮换、撤销及信任来源仍待 WP-02 决定。`jcs-sha256-v1` 只是 D07 候选标识字段。当前证据不涉及规范化、摘要计算、验签、包内容存在或可信性。

## 验证

`TEST_PASSED`。CPU 环境中 59 项通过，零跳过。Ruff lint、Ruff format 检查及两个源码模块的严格 mypy 均退出 0。第一次定向 pytest 为 39 项通过、13 项失败，暴露 `Literal[RuntimeRecordKind]` 的 wire 字符串需要 B 的 `TypedRuntimeRef` 解码器，并修正一处错误断言。应用修正后全量定向复测通过。

每条最终命令、开始和结束时间、退出码、依赖版本、工作树状态、锁及产物摘要见 `verification.json`。终端输出及 `pytest.xml` 与本报告同目录。

`RUNTIME_NOT_RUN`。定向运行覆盖模型和离线 JSON Schema。没有调用服务、数据库、Provider 或 GPU。归档安全、产物路径目录匹配、完整能力清单与真实部署匹配、Bundle 图/字节/签名、批准或激活权威来源及持久事务由 WP-02 验收。

## 兼容、安全与验收

本批增加新的 wire 契约，复用 B 的 ArtifactKind、版本、摘要、外部能力、审批角色和记录引用。没有存量数据转换、数据库迁移、执行、部署或 Git 发布操作。局部回退移除上表新增源码和证据即可，后续若有依赖则同步移除相应接线。

C4 Bundle/Release 定向门禁 **PASS**。WP-01C 整包仍需 C5 注册、生成重复性、全量回归和独立技术验收。交接 C5 统一完成 Schema 入口，后续 WP-02 实现 Bundle 包装与准入、可信验签和 Release 管理。
