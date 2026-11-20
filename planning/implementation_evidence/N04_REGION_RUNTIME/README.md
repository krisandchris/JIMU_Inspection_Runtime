# N04 原图多区域接入验收

工作树基线为 main 的 `36885d61109ba1f55c42ce65b95da701dc71c326`，包含此前联调修改。本次仅变更签名标记启用的 N04 请求路径、投影桥、场景配置、契约说明及对应验证。未发布部署。隔离运行经真实任务 API、PostgreSQL 16、六节点 Worker、现场 Stage1、WeVis 和 Qwen3_5-9B，输入为此前三张合成图。

## 正常路径

取 N03 已选对象和 N02 有效题集。用原图坐标生成短区域 ID，公共题目和相同分配只写一次。由一个兼容的已签名 ModelBinding 和显式注册支持该协议的端点处理。逐对象的本地网关请求携带同一批次和原图，投影桥持久化发送意图，一批一次调用上游 VLM。对象保留独立的 NodeRun、Attempt、Fact 和 Authority，逻辑请求通过 `provider_request_id` 引用同一物理记录。九框仍有九次本地网关请求。物理跨进程去重依赖实例共享同一份持久批次目录。

## 现场结果

下表取加固后的桥接器 1.2.1。秒数为单次 N04 物理 HTTP 往返时间，未计 N03、N05、N06 或本地网关往返。

| 场景/配置 | N03 观察组 | 物理 N04 请求 | N04 秒 | N06 |
|---|---:|---:|---:|---|
| 停车修订后，同图连续两次，关闭思考 | 每次 1 组、2 辆车 | 各 1 | 1.027 / 0.851 | 两次违规 |
| 口罩，关闭思考 | 1 | 1 | 0.471 | 违规 |
| 货物，关闭思考 | 9 | 1 | 0.942 | 9 个对象判违规 |
| 同图货物，上游默认思考 | 9 | 1 | 5.317 | 9 个对象判违规 |

停车 Bundle 1.1.0 的一次复跑在描述中识别到脚撑，却把停在区域外解释为 `unparked`，下游判无违规。停车 1.1.1 将物理运动/停放状态与车位位置分别描述，原版和两次修订后运行均完整保留。货物的独立 AI 图像参考支持 4 个占道、5 个证据不足。1.2.1 桥上的两种思考配置均为 4/9 相符，五个遮挡框全部给出确定判定。此前未加固轮次的快模式为 2/9。样本没有人工真值、明确不占道对照及独立现场验证集，不能用于估计生产准确率或延迟分位数。

[逐次小结](summary.json) 包括初次三场景、默认思考对照和审查后所有运行。每个目录保留签名 Bundle、API 读回、完整节点结果、模型桥日志和物理记录。`reviewed/parking` 为漏判轮次，`reviewed/parking-one` 和 `reviewed/parking-two` 为新提示词的两次运行。停车的桥日志在验收目录统一为 `provider/`。运行 `python planning/implementation_evidence/N04_REGION_RUNTIME/verify.py` 可从这些文件验证原图 SHA、物理派发次数、每个 Object 的请求 ID、Fact、Decision 归属，并按同一原图像素框匹配历史盲评。

## 工程验证

使用 CPython 3.12.3 和 `uv sync --locked --offline` 创建独立环境。执行命令均从仓库根目录运行。

- `python -m pytest -q tests/unit/test_region_request.py tests/unit/test_region_observation_runtime.py tests/contract/test_live_region_projection.py tests/contract/test_live_region_bundles.py tests/unit/test_wp04_http_wire.py tests/contract/test_wp04_http_contract.py tests/negative/test_wp04_http_negative.py tests/contract/test_region_observation_worker.py tests/contract/test_dependency_boundaries.py`，152 passed。停车提示词修订后再跑 `tests/contract/test_live_region_bundles.py`，3 passed。
- `python -m pytest -q tests/unit/test_wp05_worker_observation.py tests/unit/test_wp05_perception_contracts.py tests/unit/test_wp04_http_wire.py tests/contract/test_wp05_worker_inspection.py tests/contract/test_wp05_perception_loopback.py tests/contract/test_n04_mixed_observation.py tests/contract/test_grouped_inspection_worker.py tests/contract/test_dependency_boundaries.py`，71 passed。末轮独立复跑区域和旧分组 Worker，4 passed。
- `python -m pytest -q tests/contract/test_c_schema_inventory.py`，70 passed。`python scripts/generate_schemas.py --check`、`mypy src/jimu_runtime`、相关文件 Ruff check、format --check 和 `git diff --check` 通过。测试套件之间有重叠。
- 现场调用方式见[三场景入口](../../../examples/live_inspection/README.md)。`--qwen-disable-thinking` 仅影响显式启用的 N04 区域请求。
- 原生代码审查通过内联实际 diff 完成。已整改完整物理响应校验、已知区域坏值隔离和高分辨率窄框量化。混用新旧或不兼容绑定时在发模型前拒绝，保留用户要求的单次物理请求约束。独立复核另发现签名 input Schema、成员 1 和跨对象证据策略问题，已补入校验和回归。中间测试反映缺少字段的 fixture 和旧版“单框坏值使整批失败”断言，修正后二者均已复验。
