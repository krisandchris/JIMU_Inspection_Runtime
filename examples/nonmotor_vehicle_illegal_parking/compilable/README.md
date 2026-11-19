# 可编译非机动车违停巡检示例

使用已安装的 WP-04 Runtime、Python 3.12 和 OpenSSL 3。从任意当前目录运行下列命令，`SCENE_PARENT` 为包含 `compilable` 的绝对目录。

```bash
PYTHONPATH="$SCENE_PARENT" python -m compilable
PYTHONPATH="$SCENE_PARENT" python -m pytest -q "$SCENE_PARENT/compilable/test_scene.py"
```

`scene.load_source()` 加载本目录的版本化源文件。`source/index.json` 声明归档路径和本地宿主能力。定义中的 `$pin` 显式转换为 `ReferenceSlot`，由 Runtime 编译器绑定实际摘要。源记录用内容寻址的 origin ID 固定原始文件摘要、文件路径与槽声明，编译器仍按既有契约验证对应输入字节的摘要。能力、信任策略与信任配置的 pin 分别根据实际配置内容生成。`source/tool_output_schema/` 定义场景 payload 和证据槽，公共 ToolResult 信封由 Pydantic 合同生成。停车区源文件按原始字节传给 `SourceAssetBytes`。编译器生成 Fact value schemas 和 Producer Catalogue。

命令在临时目录签发隔离 Ed25519 密钥，对 Bundle 逻辑根生成 detached signature，经官方接口验签和准入，注册 Release 并在合成 Region 批准、激活。随后受理附有样图摘要的本地 Task，再签发、准入和激活具有不同 Release ID 与 Bundle 摘要的下一版本。用新打开的账本及 WP-04 `resolve_execution_pins` 恢复该 Task 后核对原 Release 和 Bundle 摘要。unsigned ZIP 和逻辑根在相同锁定环境下可复现。临时签名及账本随进程退出清理，私钥在签名后立即移除。宿主能力、时间和身份均为隔离示例设置。此入口验证准入及历史配置解析。Worker 节点执行、真实 Provider 和市政业务口径属于独立验收。

## v1 输入与输出

[`assets/model-wire.v1.json`](assets/model-wire.v1.json) 给出可用于编写本地 adapter 测试的响应示例。根对象是示例目录，实际每次响应使用其中对应角色的对象。N02 只返回注册 Profile ID。N03 以独立模型能力返回原图 `BBoxXYXY`。N04 的配置输入为 `image: {media_id, sha256}` 与 `bbox: {xyxy: [x_min, y_min, x_max, y_max]}`。联合响应的三个 slot 都采用原生字符串。运行时通过调用上下文绑定 Object，对每个 known 值生成 `{"kind":"enum","value":"…"}` 后按其精确 Question Fact Schema 校验。slot 合法返回 `insufficient_visibility` 时按 UnknownPolicy 生成带原因、无值的 `unknown` Fact。缺槽和非允许 token 进入各自的错误状态。额外字段使整个响应失去关联资格。N06 的候选响应是 `{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":1.25}]}` 或 `{"categories":[]}`。每项保留模型实际返回的有限浮点值。校验使用 `CandidateCategory` 检查分数类型，分数可为负数或大于 1。

| 工具步骤 | 输入字段 | ToolResult payload 的具名字段 | 成功证据槽 |
|---|---|---|---|
| `wheel_segmentation` | 原图引用 `image`，当前 Object 的 `bbox` | `wheel_mask` | `wheel_mask` |
| `tactile_paving_segmentation` | 原图引用 `image` | `tactile_paving_mask` | `tactile_paving_mask` |
| `wheel_boundary_geometry` | 两个精确祖先的具名 mask 值，经验证的 Context `parking_area` | `wheel_boundary_relation` | `wheel_mask`、`tactile_paving_mask`、`geometry` |

两个 mask 值均为 typed Record，含 `evidence_id`、`sha256`、`source_media_sha256` 和 `coordinate_system`。原图坐标系包含 `coordinate_system_id`、`frame: "original_image"` 和 `dimensions: {width, height}`。Geometry 步骤的 `parking_area` 为 raw DTO，包含 Region 和 GeometryResource 的 ID、版本与摘要、底层资产摘要、同一原图坐标系、`units: "pixel"` 和矩形 `bbox`。完整字段及范围见 `source/tool_input_schema/`。工具的 named output 在 payload 中可独立省略，状态与原因留在 `output_states`。每个空间产物均保留对应原图片和 Evidence，两个 mask 分开记录。适配器须从受信的固定相机和已核验 Region 构造投影，并验证原图、掩模字节、坐标和引用的一致性。

[`assets/synthetic-frame.png`](assets/synthetic-frame.png) 是合成 RGB 64×48 原图。顶左为原点，x 向右、y 向下，坐标以像素边界表示。`assets/parking-area.json` 声明本场景固定停车矩形 `[8,8,40,40]`，图中青色轮像素位于矩形外侧，黄色盲道条带位于 x=[55,59)。这些像素值可供本地 CPU 测试工具识别。业务规则保持 `parked AND wheel_boundary_relation IN (outside_parking_area, blocking_tactile_paving)`，要求两条精确 Fact 和规定证据。缺证、边界容差或关系优先级没有裁决时保持不确定。Policy 明确要求扫描全部 mandatory Rules 并 `collect_all`，因此即使候选模型返回空数组，候选也无权排除该规则。
