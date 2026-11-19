# WP-01C C4 入参、上下文和证据验证

基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。实现范围为 WP-01C C4 中的 Intake、Context、Evidence 及感知坐标基础类型。与其他 C4 模型族共用工作树，本批仅写入下列路径。

- `src/jimu_runtime/domain/runtime/{intake,evidence,media}.py`
- `tests/{unit,contract,negative}/test_c_intake*.py`
- `tests/fixtures/wp01c/intake/`
- `planning/implementation_evidence/WP-01C/c4_intake/`

## 契约与设计

- 持久根为 `TaskSubmission`、`AcceptedTask`、`MediaAsset`、`ContextSnapshot` 和 `EvidenceAsset`，每根显式要求版本、拒绝额外字段。fixture 分别验证提交中的单图、接收时解析的 Release/Bundle、RGB 媒体、五种上下文来源和证据资产阶段。
- 时间同时记录内部 UTC 时间、带偏移的原始时间字符串和时钟来源。两个时间须代表同一时刻。原始时间的小数精度限制为微秒，源时区名称按原值保存。
- 上下文使用已有的精确 Schema 引用和封闭 Value 类型。快照内禁止重复或有前缀重叠的值路径。来源必须与快照的 Task、Device、Camera 或 Region pin 一致。声明式派生记录实际实现版本、摘要和输入 selector。
- 图像尺寸沿用 `AssetDimensions`。逐像素坐标支持小数，边界使用左上原点的像素边缘。证据记录正向仿射变换及其实现版本，可验证从原帧到派生帧的链。MediaRef 钉住图像内容摘要。存储引用使用逻辑 store ID 和相对 object key，finalized 阶段额外记录被核验对象的版本和摘要。
- 执行/对象证据归属与项目/任务访问域分别显式记录。保留策略使用精确 EvidencePolicy 引用和保留词表成员。原图与派生证据可用 `EvidenceAsset.validate_original_media` 按实际传入的 MediaAsset 核对来源 ID、摘要、尺寸及适用的 MIME 和大小。

## 验证结果

**TEST_PASSED**。定向 pytest 52 项通过，无跳过。[测试命令及输出](pytest.log) · [JUnit](pytest.xml)

Ruff lint、格式检查和三份源文件的 mypy 均通过。所有命令退出码为 0。[Ruff](ruff.log) · [格式](ruff-format.log) · [mypy](mypy.log) · [依赖、基线及锁摘要](environment.log)

**RUNTIME_NOT_RUN**。本批数据校验需要调用方提供真实的 registry 和 policy 解析、媒体解码、存储摘要/版本核验及访问权限执行。没有连接持久化服务或执行上传，未引入迁移、依赖或权限配置。示例场景原有 JSON 仍作为规格示例，五个新根使用独立 fixture。

**定向验收 PASS**。生成并审查 Schema、运行全量回归属于 C5 整体验收。可按上述文件边界回退本批新增文件。
