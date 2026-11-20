# 三项机器狗视角巡检联调 · 2026-11-19

基线为 JIMU Runtime `main@36885d61109ba1f55c42ce65b95da701dc71c326`。本次用内置 ImageGen 生成三张独立的 1536 × 1024 RGB 测试图。[原图及生成提示词](images/) 是测试输入。每项只有一个合成场景，无现场标注集、设备标定或生产发布。

| 正式测试输入 | 任务结果 | 执行及证据 |
|---|---|---|
| [两辆非机动车临近停放](images/parking.png) | 合组时 `uncertain`，覆盖 `partial`。逐框对照为整图 `violation`，覆盖同为 `partial`。 | [合组任务 ID、HTTP 回执和节点状态](parking-group/verification.json) · [逐框对照](parking-per-box/verification.json) |
| [餐饮操作人员未佩戴口罩](images/mask.png) | N04 观测口鼻可辨且未戴口罩。员工归属与指定后厨区域返回 `insufficient_visibility`。N05 的空工具计划无法提交成功结果，N06 未执行。HTTP 查询返回 409 `result_pending`。 | [红框任务和 API 回执](mask/group/api.json) · [原始适配器日志](mask/group/provider/) · [持久化异常](mask/group/worker.log) |
| [货物占用人行道](images/goods.png) | N01 完成。正式 N02 调用了 Stage1，当前模型只提供人员、非机动车两个主体输出。桥返回 422 `stage1_unsupported_profile`。执行失败，汇总为 `uncertain`，覆盖 `none`，无对象级 Decision。 | [任务结果](goods/verification.json) · [N02 调用与返回](goods/provider/) · [独立模型探针](goods/diagnostics/verification.json) |

## 运行方式

通过 FastAPI 的公开路由 `POST /api/v1/inference/tasks` 受理，`GET /api/v1/inference/tasks/{id}/result` 和 trace 接口回读。API 测试客户端使用 ASGI 进程内传输。签名 Bundle、Release、PostgreSQL、媒体存储、工作流调度、Fact、覆盖与判定走 Runtime 实现。模型实际经 HTTP 调用本机 RTX 4090 上的 Omni-JEV 和 WeVisOne-VL-4B-NF4，以及内网 Qwen3_5-9B。隔离库退出后以本目录保存的回执、节点和模型记录追溯。

联调宿主把 Stage1 注册主体与 WeVis 坐标投影到 Runtime 契约，保留 Qwen 的答案正文并适配 vLLM 响应外层。在停车测试中，宿主显式替换了示例的 GeometryInput 和工具实现绑定，将工具调用送往真实分割服务，再调用现有确定性几何判断。Release 中的测试身份、相机和停车区域仅在本次运行有效。旧示例保留的 `synthetic.*` 标识与实际调用的模型身份须结合 wire 日志读取。口罩与货物的 Bundle 是为本次测试单独编译签名的隔离配置。[测试宿主源码](harness/) · [场景配置和编译检查](test-config/)

## 关键发现

1. **停车两车合组能贯穿六节点。** 两个检测框合成一个 Object。N04 实际收到[这一张红框图](parking-group/marked.png)。N05 引用相同 Object 和红框，车轮工具取原图组 ROI，盲道工具取原始全图。N06 收到同组两路事实。白线区域从合成原图选取保守内接矩形 `[344,498,606,702]`。组内有 90,626 个车轮掩码像素在该矩形之外，其中 6 个像素也落在盲道掩码中；现行几何只输出单一关系，返回 `overlapping_relations_unresolved`。权威关系 Fact 为 unknown，候选模型给出的违规类别未能通过确定性规则。掩码交点受模型和坐标影响，不能作为独立人工真值。[逐组几何与模型调用核验](parking-group/geometry-verification.json)
2. **同一停车原图逐框处理时有一个确定命中。** 两组中一组仍有上述关系冲突。另一组的车轮掩码在框外，与盲道交点为零，规则命中 `nonmotor_vehicle_illegal_parking`。说明合组粒度会改变单值空间关系的结果；如何保留观察组的“存在任一违规”证据，仍需明确关系表达或判定优先级。[逐框几何核验](parking-per-box/geometry-verification.json)
3. **合法空 ToolPlan 触发运行时缺陷。** 红框路径及未启用红框的对照均已复现。N04 的 `not_wearing_mask` 是外观事实，不能单独证明人员在指定后厨且属于员工名单。N05 以空产物提交成功，被 `workflow/jobs.py` 拒绝。隔离工作进程异常退出时 Execution 仍显示运行中。应明确记录成功的空计划摘要或定义等价的可持久化完成语义。[重复实跑日志](mask/worker.log) · [最小复现脚本](harness/reproduce_empty_plan.py)
4. **货物缺少 N02 已部署的发现能力。** 独立探针中，WeVis 在同一原图给出 9 个纸箱框，Qwen 对单个候选框返回纸箱与人行道。一次全目标分割在 4090 显存不足时返回 500，改为单个前景纸箱后取得二值掩码。这些调用来自诊断脚本，正式 N03–N06 均受 N02 失败阻塞。业务判定还需要绑定实际设备的通行区域和占用规则。[正式执行记录](goods/result.json) · [单目标掩码](goods/diagnostics/imagegen-carton-mask-0.png)

## 复跑

在仓库根目录使用含锁定测试依赖、Docker 及三项已启动服务的环境。以下 `python` 指本次 CPython 3.12 测试解释器。每次请使用空的新输出路径。

```bash
D=planning/implementation_evidence/LIVE_THREE_TASKS_20261119
export PYTHONPATH=".:src:$D/test-config:$D/harness" PYTHONDONTWRITEBYTECODE=1
OUT=$(mktemp -d)
python "$D/test-config/compile_check.py"
python "$D/harness/run_grouped_parking.py" "$OUT/parking-group" "$D/images/parking.png"
python "$D/harness/run_parking.py" "$OUT/parking-per-box" "$D/images/parking.png"
python "$D/harness/run_trial.py" goods "$OUT/goods"
python "$D/harness/run_marked_mask.py" "$OUT/mask"
```

口罩命令在当前基线上预计以非零退出，错误见已留存日志。货物命令可正常取回失败执行的终态；进程正常退出与业务规则命中分别记录。本次验证了隔离宿主与模型的真实联通和上述节点行为，未进行生产部署或精度评测。
