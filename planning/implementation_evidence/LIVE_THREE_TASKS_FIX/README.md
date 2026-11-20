# 三项巡检任务全链路复验

本轮在 main 工作树（基线 `36885d61109ba1f55c42ce65b95da701dc71c326`）修复了停车成员关联、无工具执行时的 N05 持久化，以及观察 Fact 到原图的证据引用。复验使用上一轮相同的三张机器狗视角合成图，任务经 FastAPI 提交，Worker 在隔离 PostgreSQL 执行 N01–N06，最后从结果和分页 Trace 接口回读。N02 的人员和车辆发现使用 Stage1；纸箱发现和 N03 定位使用各自独立的 WeVis 调用。Qwen 提供 N04 观察和 N06 候选，正式判定由固定 Bundle 的 Rule 完成。

| 原图与配置 | N06 整图判定 | 证据 |
|---|---|---|
| 非机动车，两辆合为一个观察组 | 违规，覆盖完整。一次 N04 联合观察，两个成员各自满足停车与出界条件 | [停车报告](parking/README.md) · [逐成员车轮证据](parking/geometry.json) |
| 后厨人员，有同帧合成名册 | 员工未佩戴口罩，覆盖完整 | [口罩对照报告](mask/README.md) |
| 同一后厨图，缺名册 | 不确定，覆盖部分。已观察到未戴口罩，员工适用性缺证 | [无名册执行](mask/control/verification.json) |
| 人行道纸箱，有合成禁放政策 | 违规，覆盖部分。9 个对象中 4 个违规、2 个未违规、3 个不确定 | [有政策执行](goods/registered-policy/verification.json) |
| 同一纸箱图，缺禁放政策 | 9 个对象全部不确定，覆盖部分。30 个节点执行全部成功 | [无政策执行](goods/registered-no-policy/verification.json) |

停车的空间特征按 N03 有序成员编号计算。同一成员的观察和车轮接地点形成违规见证。一个确定命中足以使整组命中；其余成员的未知不能推翻该结果。若所有成员都没有命中且仍有成员无法判定，整组保持未知。车轮底缘是这一视角的接地点近似。近重复轮胎掩码和属于其他成员的共享像素不能充当第二个独立车轮。N03 红框、N04 Fact、N05 几何和 N06 Decision 保留同一 Object 引用。

空 ToolPlan 产生可持久化的完成摘要。它记录真实的零次执行，并允许 N06 继续。N04 按 EvidencePolicy 从已完成入库的原图生成 EvidenceAsset，将观察 Fact 与原图、提示词和响应 Schema 绑定。口罩使用宿主登记的同图员工名册，货物使用相机和区域绑定的政策。宿主按原图摘要与设备位置校验；有相同图片的两个运行仍有各自独立的 Execution。

货物阳性运行中，两个框可见性不足，另一个框的 Qwen 调用未交付完整回答。Worker 将该对象保留为不确定并记录 `partial_failed`。对未完整回答的原请求进行一次独立诊断后，Qwen 返回了 `occupies_sidewalk`。原 Execution 的九条 Decision 保持原值。当前货物配置覆盖纸箱，区域和名册均为本组样本的合成资料。现场准确率尚未评测。

## 复现和校验

[三个运行入口](../../../examples/live_inspection/README.md) · [原始样本](../LIVE_THREE_TASKS_20261119/images/) · [任务与结果交叉核对](matrix.json) · [测试记录](regression.json) · [变更文件摘要](source_sha256.json)

`verify_matrix.py RUN IMAGE EXPECTED [--rule RULE_ID]` 核对源图摘要、受理结果、签名包 ZIP 可读性、六节点出现次数、分页 Trace 连续性，以及 N06 匹配与对应对象的 Decision。各场景另行核对模型响应、签名 Bundle 的准入记录及持久化证据。

直接回归覆盖成员混配、越界/缺失/未知成员、重复掩码、旧 Bundle 的请求格式、空与全跳过计划、旧记录回放、来源摘要与人员或区域不匹配。集成过程中运行了较广的单元、负向和契约套件。共享开发环境一度引入了错误的 editable checkout 和一个跨进程等待超时；已在正确 checkout、隔离快照或单独回归中复验。图像层依赖边界发现的问题已修复并复测。Compose 部署验收未启用。

独立规划审查及原生代码审查指出了来源错配风险，整改包括有效输入 Schema 引用解析、历史 N05 成功记录回放、N03 权威成员顺序、停车区域、独立车轮识别，以及受信配置校验。SHA256SUMS 覆盖本目录交付文件（清单自身除外）。上一轮证据另行逐项核验了其原有的 106 项摘要。

`RUNTIME_VERIFIED`（隔离环境，合成图，真实模型） · `PRODUCTION_UNVERIFIED`
