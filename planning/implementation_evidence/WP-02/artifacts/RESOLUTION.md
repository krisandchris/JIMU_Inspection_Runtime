# WP-02 B2.2 制品与闭包

本地实施依据 [WP-02 计划](../../../active/WP-02/PLAN.md)和[当前授权记录](../../../active/PLATFORM_GOAL.md#当前授权记录)。变更覆盖应用层制品目录、离线依赖闭包、运行依赖和边界测试。冻结 Domain、公开 BundleManifest 及 Domain Schema 合同沿用原版本。

| 输入 | B2.2 来源和证明 | 后续语义或信任检查 |
|---|---|---|
| 25 种制品 | manifest 精确 ContentRef、包内规范路径和 locator 指向的规范字节。定义根使用冻结 Pydantic 模型。聚合对象或数组可嵌套，定位到的成员有唯一、互不重叠的路径。具名空包装、额外叶和没有登记的载荷拒绝。 | B2.3 校验可调用绑定及静态类型。 |
| 静态资源 | 包内 descriptor 是制品；resources/assets/data/ 下被声明的原字节由 descriptor 的大小和 SHA-256 校验，同时归入文件清单。以 JSON 为扩展名的原始数据保持字节。 | 调用方从验证后的 descriptor 正常使用数据。 |
| Draft 2020-12 | 固定 Draft 元 Schema 验证语法，离线目录解析任意已声明本地绝对 ID、相对引用、JSON Pointer 和 anchor。Schema ID、anchor 及目标均检查歧义。value_schema 复用已冻结的生成身份。其他 Schema 的 URI 作包内别名，manifest 的 kind/ID/version/digest 与 locator 仍是权威定位。 | B2.3 独立重生生成 Schema，校验资源可投影的有限形状及各消费者实际用途。 |
| RawDtoSchemaDescriptor | 已验证的 context、attribute、response、Tool input、object、decision Schema 提供字节与闭包。 | B2.3 从支持的原始 DTO 投影重构。 |
| TypedValueSchemaDescriptor | 已验证的 value、attribute、response、Tool input Schema 提供字节与闭包。 | B2.3 从支持的 typed value 投影重构，按生成规范重生 Fact payload。 |
| ToolInputRecordDescriptor | 实际 Tool input Schema 和 Tool 声明的精确 ContentRef。 | B2.3 从闭合的混合输入属性和每个字段的 typed/raw 投影重构。 |
| ToolResultSchemaDescriptor | 实际 Tool output Schema 和 Tool 声明的精确 ContentRef。 | B2.3 从规范化结果信封、证据槽位和 Tool 输出声明重构，并比对实际使用。 |
| Context、Region、枚举和单位 | Context/Region 及字段 schema 精确引用、Geometry/Fact 等实际单位声明能在验证模型中遍历，对所有引用的词表成员比对精确词表。 | B2.3 在每个使用点证明字段类型、读域、词表语义和单位。Fact 单位来自冻结定义，Context 和字面量单位需实际声明或可追溯的受信规范。 |
| Producer、外部能力和运行记录 | manifest-rooted ProducerCatalogue 为冻结 Pydantic 定义，作用域和引用保留在该模型内。SystemProducer、Tool implementation、Context derivation、部署和评审要求记录为独立外部引用。 | B2.3 重算 Question 和 Tool producers，核对 SystemProducer 的 manifest 声明。B2.4 使用受信注册表、实现版本和评审报告证明能否运行。Task 图片、设备和实际 Invocation 属执行期输入。 |
| Applicability 与反向关系 | IdentitySelector 对 Profile 或 Region 的精确版本单独解析，不加入摘要依赖边。Question 和 ToolPlan 的反向所属由 Profile 正向引用派生。 | B2.3 按具体 Profile/Region 和 Policy 使用点校验，LocalToolFactSelector 在所属 Plan step 验明祖先与映射。 |

公开接口为 `build_catalogue(manifest, payloads, max_bytes=…, max_depth=…)`、`ArtifactCatalogue.resolve`、`ArtifactCatalogue.resolve_selector`、`validate_closure`、`BundleClosure.schemas.resolve_reference`。闭包失败包含拒绝代码、载荷路径、模型/Schema 字段路径。

运行依赖沿用现有锁值 `jsonschema==4.25.1`。其直接导入限于 `bundle.artifacts`。`rfc8785` 沿用 F1 导入许可并为 `bundle.integrity` 加入精确许可。Domain 到 Bundle 的直接或经 reexport 依赖由静态门禁拒绝。

TEST_PASSED 见 [命令和输出](commands.txt)与 [JUnit](tests.xml)。RUNTIME_NOT_RUN。这里的运行环节限于 CPU 进程、内存包和独立无 dev 安装。无迁移、远端 Schema 查询或生产信任证据。
