# 停车合组真实复测

本次通过公开 FastAPI 路由向隔离 Runtime 提交同一张[合成原图](../../LIVE_THREE_TASKS_20261119/images/parking.png)。N01–N06 均为 `succeeded`，Execution 为 `completed`，整图为 `violation`，覆盖为 `complete`。确定性规则 `nonmotor_vehicle_illegal_parking` 的谓词求值为 `true`。运行基线为 `main@36885d61109ba1f55c42ce65b95da701dc71c326`，本轮源码由清单记录。 [结果与核验摘要](verification.json) · [公开 API 回执](api.json) · [Runtime 持久化记录](result.json)

Stage1 在原图命中登记的非机动车 Profile。WeVis N03 在原图取得两个重叠的检测框，Runtime 保留原有次序并创建一个合组 Object。N04 只向 Qwen3_5-9B 发送一次联合请求，输入为原尺寸红色组框图和有序成员坐标。返回的两个成员均为 `parked`，并给出可见支架展开作为依据。 [N04 实际编译提示](compiled-n04-prompt.txt) · [红色组框图](marked.png) · [调用回执](wire/)

N05 切分两份原图成员裁剪，并将未加红框的原图用于盲道分割。固定停车位外包矩形为 `[0,470,660,730]`，保留原内接矩形 `[344,498,606,702]` 作为框内依据。两者仅适用于该合成捕获。每个成员均有两个独立、可归属的轮掩码底缘候选接地范围，其 x 坐标均大于 660。第一个成员的第三个轮掩码落在重叠框中，其候选接地点无法归属，因此该成员的盲道关系仍是未知。两个成员各自具备停车和出界事实，任一成员已足以满足组条件。核验重算了原图裁剪、WeVis 原始响应摘要、完整工具返回摘要和逐成员空间报告。 [几何报告](geometry.json) · [三次 WeVis 原始分割回执](tool-wire/)

真实尝试首次暴露几何 Sidecar 只接受旧的枚举与祖先掩码格式。当前持久化在新工具能力下核验封闭的合组证据结构，按照权威 N03 Object 和 N01 Context Snapshot 校验成员顺序与完整 ROI，并将报告和工具事实绑定至同一次 Dispatch。旧工具格式继续通过回归。 [首轮工具异常](first-attempt-tool-error.json) · [回归结果](regression.log)

## 本地复测

运行环境为 Python 3.12.12、Pydantic 2.12.5、Pillow 12.3.0、FastAPI 0.141.1、HTTPX 0.28.1、SQLAlchemy 2.0.52 和隔离 PostgreSQL 16.14。FastAPI 测试客户端使用进程内 HTTP 传输。已启动的 Stage1、WeVis 与 Qwen 服务经真实 HTTP 调用。该运行覆盖单张合成图及其固定区域，现场标定、模型总体精度和生产负载未验证。

```bash
OUT=$(mktemp -d)
IMAGE=planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images/parking.png
PYTHONPATH=.:src PYTHONDONTWRITEBYTECODE=1 flock /tmp/jimu-live-three-scene-gpu.lock \
  python -m examples.live_inspection.parking.run "$OUT" "$IMAGE"
PYTHONPATH=.:src python -m pytest -q \
  tests/contract/test_live_parking_members.py \
  tests/contract/test_dependency_boundaries.py \
  tests/contract/test_wp05_media_tool_sidecars.py \
  tests/contract/test_wp05_geometry_step.py
```

真实执行和 31 项回归均以退出码 0 完成。测试覆盖跨成员拼接、全否定、混合未知、成员换序、近重复轮掩码、缺少或越界的成员答复，以及同坐标系下停车 ROI 被交换。 [真实执行日志](run.log) · [源码摘要](source-sha256.txt) · [证据文件摘要](sha256sums.txt)
