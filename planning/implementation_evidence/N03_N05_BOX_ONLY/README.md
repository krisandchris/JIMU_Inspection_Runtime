# N03–N05 原图与观察组坐标验收

基线为 main 的 `36885d61109ba1f55c42ce65b95da701dc71c326` 及本地已有开发改动。验证使用 Python 3.12.3、Pydantic 2.12.5、pytest 8.4.2 和隔离 PostgreSQL。真实推理走本地测试入口配置的 Omni-JEV、WeVis 和 Qwen，Qwen 区域请求关闭 thinking。输入来自历史合成机器狗视角样图，口罩样图使用合成人员台账。

| 实际输入 | N03 交付 | N04 实际上游请求 | N06 | 红框资产行数 |
|---|---|---:|---|---:|
| 停车，最终 Bundle 1.1.2 | 1 组、2 个成员 | 1 | violation，两个成员各自满足判定 | 0 |
| 口罩 | 1 人 | 1 | violation | 0 |
| 货物占道 | 9 个区域 | 1 | violation | 0 |

首次停车执行保存在 `parking`。模型报出两辆车已停放，漏报成员的可见依据。工具按原有规则保留 unknown，最终 uncertain。调整停车 Question 的成员输出说明后，开发复测 `parking-final` 和最终版本 `parking-112` 都取得两条有依据的成员结果。三次停车执行均保留在回执中。调试过程中一份带有字面花括号的候选提示词在 Bundle 编译阶段被拒绝，随后改用正式支持的文本格式。

每个新执行都查询项目、客户端及执行 ID 对应的 PostgreSQL marked_frame_media 行数。校验程序比对原图 SHA-256、N03 组框及成员框顺序、N04 的实际派发回执、N05 的上游 Object 和 ROI、停车几何工具报告以及 N06 Decision 的 Object ID。货物样图的区域回答与已有逐区盲评一致 4/9。该样本的任务级命中不能作为逐框准确性验收。

## 离线复核

`receipts.zip` 保留 Worker 执行结果、逻辑请求回执、物理请求批次以及停车组的几何报告。可从仓库根目录重新核对，无需启动模型。

```bash
python3 planning/implementation_evidence/N03_N05_BOX_ONLY/verify.py
```

## 运行入口

以下命令中的 `$OUT` 为新建输出目录，`$IMG` 为 `planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images`。本轮在已锁定的项目测试环境执行，使用 `PYTHONPATH=src:.`。

```bash
python -m examples.live_inspection.parking.run "$OUT/parking" "$IMG/parking.png" --qwen-disable-thinking
python -m examples.live_inspection.scenes.run_mask "$OUT/mask" "$IMG/mask.png" --synthetic-roster --qwen-disable-thinking
python -m examples.live_inspection.goods.run "$OUT/goods" "$IMG/goods.png" --qwen-disable-thinking
```

## 自动化验证

- 主回归运行 `pytest -q --tb=short tests/contract/test_grouped_inspection_worker.py tests/contract/test_n03_marked_frame_media.py tests/contract/test_live_region_bundles.py tests/contract/test_live_parking_members.py tests/unit/test_region_observation_runtime.py tests/unit/test_p1_grounding_groups.py`，46 项通过。
- N04 逐对象、跨对象、重试及 loopback 专项 59 项通过。N05 几何、恢复、负向及停车输入专项 32 项通过。各组有重叠。
- 完整工具执行器与混合 ToolPlan 回归运行 `pytest -q --tb=short tests/contract/test_wp04_tool_executor.py tests/contract/test_wp05_mixed_tool_plan.py`，结果保存在 `tool-tests.txt`。
- 独立归档测试覆盖旧图读取权限、介质替换、损坏与恢复。新执行测试拦截归档读写方法，并校验错误媒体在网关实际派发前被拒绝。
- `mypy src/jimu_runtime` 检查 224 个源文件通过，`scripts/generate_schemas.py --check`、Ruff check、format 和 `git diff --check` 通过。
- 针对本轮数据流与在途兼容的原生静态代码审查未报告 P1/P2。自动化和真实联调分别验证历史输入兼容与新执行行为。
