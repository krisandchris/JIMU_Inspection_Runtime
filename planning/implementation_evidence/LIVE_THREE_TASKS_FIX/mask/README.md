# 口罩巡检复测

| 受理输入 | Runtime 汇总 | 规则证据 |
|---|---|---|
| 同帧图像 | `uncertain`，覆盖 `partial` | [对照运行](control/verification.json)，范围缺证 `applicability_unresolved` |
| 同帧图像与合成员工名册 | `violation`，覆盖 `complete` | [合成运行](synthetic-roster/verification.json)，命中 `food_service_worker_no_mask` |

两项使用同一 Bundle 摘要 `378c0cbc0f6a37b63ac0502228d58b736fad810cbe23c68f3745801cf261875a`。提交均返回 HTTP 202，执行、结果和两页 Trace 均返回 200。六个节点均成功。N04 留存同一人员的两项已知事实 `face_assessable` 与 `not_wearing_mask`。N05 提交成功完成摘要，工具执行数为 0。N06 在合成任务中使用经读取校验的原图作为规则证据。对照任务的模型也预测了违规候选类别，确定性范围门控保留了不确定结论。

两个隔离任务使用同一张 [1536 × 1024 生成图](../../LIVE_THREE_TASKS_20261119/images/mask.png)、场景定义和正式 Runtime 路径。对照任务仅提供原图。合成任务由测试宿主验证图像 SHA-256、设备、相机和 Region 后提供业务范围证据。该证据把原图全幅声明为这台合成后厨相机的校准区域，并把一名虚构员工关联到人工标定框及其身份锚点。名册只记录虚构身份及其空间关联。

N02 以 Omni-JEV 的人员发现信号触发 Profile。N03 独立调用 WeVis。带名册的任务在回传全部人员框前，要求每个框包含身份锚点且与唯一名册框的 IoU ≥ 0.65。模型框和人工框必须一一对应，失败时报告框关联失败。N04 只观测同一人员的口鼻可见性及佩戴状态。N05 的空 ToolPlan 通过可追溯的完成摘要提交。签名规则要求 N01 同时留存范围声明、精确原图摘要和精确名册版本。N04 的视觉事实绑定真实原图与已持久化的提示词和结构契约。N06 在验证这些输入后遍历全量必评规则，候选模型提供参考类别。

[场景定义](../../../../examples/live_inspection/scenes/food-service-mask.json) · [合成证据与拒绝条件](../../../../examples/live_inspection/scenes/trusted_scope.py) · [复跑入口](../../../../examples/live_inspection/scenes/run_mask.py)

[同一红框观测图](marked.png) · [对照数据库回读](control/result.json) · [带名册数据库回读](synthetic-roster/result.json)

## 复跑

仓库根目录、CPython 3.12、锁定测试依赖、Docker 和三项已经运行的模型端点。每次选择空的输出路径。两个任务按顺序持有 GPU 联调锁。

```bash
D=planning/implementation_evidence/LIVE_THREE_TASKS_20261119
OUT=$(mktemp -d)
export PYTHONPATH=.:src PYTHONDONTWRITEBYTECODE=1
flock /tmp/jimu-live-three-scene-gpu.lock python -m examples.live_inspection.scenes.run_mask \
  "$OUT/control" "$D/images/mask.png"
flock /tmp/jimu-live-three-scene-gpu.lock python -m examples.live_inspection.scenes.run_mask \
  "$OUT/synthetic-roster" "$D/images/mask.png" --synthetic-roster
python -m pytest -q tests/contract/test_live_scene_scope.py
```

任务、覆盖和 N01–N06 中间记录来自隔离 PostgreSQL 16.14，API 回执来自公开路由。模型服务由试验桥通过 HTTP 实际调用。测试用人员名册和相机区域都属于这张生成图，结论仅适用于该隔离合成场景。

## 回归核验

CPython 3.12.3、Pydantic 2.12.5、SQLAlchemy 2.0.52、Psycopg 3.3.5。使用 `PYTHONPATH=.:src` 从本工作树读取运行时代码。

- `python -m pytest -q tests/contract/test_live_scene_scope.py` 15 项通过，退出 0。测试覆盖缺域配置、裸布尔值、原图和相机身份、人员框全量匹配、候选列表为空时的必评规则、以及两份真实旧版 N04 记录的原值回读。
- `python -m pytest -q tests/contract/test_live_scene_scope.py tests/contract/test_wp05_perception_loopback.py tests/contract/test_n04_mixed_observation.py tests/contract/test_wp06_artifact_replay.py` 原轮有 32 项通过，2 项因共享 ChatContract 的测试夹具比较失败。夹具修正后，`python -m pytest -q tests/contract/test_wp05_perception_loopback.py -k unusable_model_groups_preserve_disjoint_profile_work` 复跑两项均通过，退出 0。Artifact replay 合约的 6 项都已通过。
- 两份签名 Bundle 经 `unzip -tqq` 检查，退出 0。目录内证据可用 `sha256sum -c SHA256SUMS` 核对。
