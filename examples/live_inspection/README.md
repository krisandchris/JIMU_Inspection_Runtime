# 隔离 live 场景复跑

三个入口使用仓库内的场景配置、生产 FastAPI 路由、独立 PostgreSQL 16 和真实六节点 Worker。测试宿主先编译、签名、准入并激活 Bundle，再通过进程内 ASGI 向 FastAPI 发送首次 HTTP POST。宿主核对 POST 返回的 Task 和 Execution Pin，并从同一个数据库读取执行、结果及完整 Trace。

## 运行条件

使用仓库锁定的 Python 3.12 依赖和 OpenSSL 3，从仓库根目录运行。Docker 必须能通过本地 socket 启动测试已固定镜像摘要的 PostgreSQL 16 容器。每次调用传入新的私有输出目录及审阅过的 RGB PNG。运行会调用现场模型端点，需使用已获授权的环境。

桥接服务监听本机 loopback。默认上游分别是 Omni-JEV 路由端口 `18101`、WeVis 检测端口 `18103` 和桥接配置中的 Qwen Chat Completions 端点。停车场景还调用 WeVis 分割，默认使用 `18103/v1/segment`。顺序执行占用同一模型环境的命令。

```bash
export PYTHONPATH=src:.
export PYTHONDONTWRITEBYTECODE=1

IMAGES=planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images
OUT=$(mktemp -d)

flock /tmp/jimu-live-three-scene-gpu.lock uv run --locked python -m examples.live_inspection.parking.run "$OUT/parking" "$IMAGES/parking.png"
flock /tmp/jimu-live-three-scene-gpu.lock uv run --locked python -m examples.live_inspection.scenes.run_mask "$OUT/mask" "$IMAGES/mask.png" --synthetic-roster
flock /tmp/jimu-live-three-scene-gpu.lock uv run --locked python -m examples.live_inspection.goods.run "$OUT/goods" "$IMAGES/goods.png"
```

停车入口签发 `1.1.2` Bundle，口罩和货物入口签发 `1.1.0` Bundle。三者使用 `JIMU_N04_REGIONS_V1` 绑定原图区域模式。N04 对同图满足合批条件的对象使用一次 Qwen 请求，按原图坐标标识对象和停车组成员。N03 只发布原图像素坐标。N05 用同一 Object 的组框和有序成员框定位目标，图像工具从固定的原图读取像素。每个 Object 独立验证并保存自己的 Facts。货物问题中的禁堆范围与对象框使用相同的 0–999 坐标。

添加 `--qwen-disable-thinking` 可显式关闭区域请求的 Qwen 思考。省略该选项时使用上游默认设置。

用 `uv run --locked python -m pytest -q tests/contract/test_live_region_bundles.py` 验证三个场景的两版 Bundle 能在同一隔离存储中签名准入。

| 场景 | 输入范围 |
| --- | --- |
| 非机动车停车 | 使用该图的合成场地停车区域和盲道局部搜索框。工具读取原图执行分割，再用像素几何作判定。ROI 和配置与该捕获图绑定。 |
| 后厨口罩 | `--synthetic-roster` 使用合成画面的手工员工位置标注和相机区域，逐个核对检测框与虚构员工名册关联。省略参数可运行缺少可信名册的分支。 |
| 人行道堆物 | 使用合成画面的固定相机和人行道禁堆范围。业务范围由宿主校验场景、相机及原图摘要后提供。传入 `--omit-placement-policy` 可运行缺少可信禁堆政策的分支。 |

三张合成画面位于既有现场试验目录的 [images](../../planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images/)。停车 ROI、口罩名册和堆物区域均来自隔离场景的人工标定。模型阶段仍由现场服务推理。人像、员工身份、政策范围和停车设施均为合成试验数据。

## 输出

- `intake.json` 和 `signed-bundle.zip` 保存 HTTP 接收后核对过的 Task、初始 Execution 及签名 Bundle。
- `api.json` 保存 POST、GET Execution、GET Result 和每一页 Trace 的状态码及响应。翻页固定首个响应中的事件上界，并校验序号连续。
- `result.json` 保存 PostgreSQL 单次运行的节点记录、工具子节点、模型端点描述符 pin 和最终结果。宿主核对公共 Execution、Result 与数据库输出一致。
- `provider/` 或 `wire/` 保存桥接器实际请求与响应的归属证据，停车的 `tool-wire/` 保存工具调用证据。Runtime 注册的模型身份标识隔离协议桥，上游模型、部署报告和归一化过程由各自的 wire 工件记录。
- `provider/region-batches/` 或 `wire/region-batches/` 保存绑定到本次 Execution 与尝试的区域批次状态，用于同一尝试的重复请求。每次复跑新建输出目录和 Execution。
- `media/` 保存隔离运行的原图和适用的工具产物。目录和证据可能包含业务图像及模型输出，应仅在相应许可范围内保留。

在开发或测试代码中可调用 `examples.live_inspection.host.run_case`。调用方提供 Bundle 编译输入、能力注册及模型端点。可选的 Tool 注册只在当前案例有效，使用准入时锁定的实现 proof。直接使用真实网络端点需显式配置网络访问，协议桥由案例入口显式启动。
