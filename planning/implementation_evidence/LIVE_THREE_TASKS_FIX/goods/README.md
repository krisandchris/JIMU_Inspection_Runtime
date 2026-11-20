# 人行道纸箱占用联测

同一份签名 Bundle、同一张合成原图和同一组合成标定，在独立 PostgreSQL 上执行了两次真实模型联测。有受信禁放政策时，规则命中 `sidewalk_goods_encroachment`。缺少政策时，9 个对象全部保留为 `uncertain`。两个 Execution 各产生 9 个对象级 Decision，适用范围为场景中放置的纸箱。

| 运行 | Execution | N04 观察 | N06 Decision | 覆盖 |
| --- | --- | --- | --- | --- |
| [受信禁放政策](registered-policy/verification.json) | `partial_failed` | 4 个占用、2 个不占用、2 个 `unknown`、1 个调用失败未产生 Fact | 4 个 `violation`、2 个 `no_violation`、3 个 `uncertain`。汇总 `violation` | `partial` |
| [缺少禁放政策](registered-no-policy/verification.json) | `completed` | 6 个占用、3 个 `unknown` | 9 个 `uncertain`。规则组适用性为 `unknown` | `partial` |

两次检测分别将同一原图送至 WeVis。N02 仅返回注册 Profile 命中，由 Runtime 附上签名配置中的 Question。N03 独立获得 9 组原图坐标。每组均有 9 个相同的定位框及 9 份 N05 空计划完成凭证。三个对应框在两次运行中均得到 Qwen 的 `occupies_sidewalk`，只在禁放政策有效的运行中命中规则。正向规则的证据守卫引用了同一原图字节的持久 EvidenceAsset。FastAPI 受理返回 202，执行、结果和分页 Trace 均返回 200。[交叉核验](registered-comparison.json) · [正向 API 记录](registered-policy/api.json) · [对照 API 记录](registered-no-policy/api.json)

[原图](../../LIVE_THREE_TASKS_20261119/images/goods.png) 的 SHA‑256 为 `2270ce7d8acd45780bbed034e4598e68c38fddd333d16750d2761d4c506356e8`。隔离宿主校验设备 `synthetic.device`、相机 `synthetic.fixed.camera`、区域 `isolated_sidewalk` 和两项受信摘要。签名观察提示给出原图坐标区域 `x=445..1320, y=410..980`，Qwen 评估被标记纸箱的落地或支撑是否占用区域。宿主颁发合成禁放政策，由 N01 冻结为受信上下文。两个最终运行的 Bundle 摘要均为 `e5acdc66274003b12952712a4e67d06b8b7488dbbba7200f86c006632900632c`。

## 回归与失败证据

[接入原图 EvidenceAsset 之前的首轮](with-policy-01/verification.json)与[第二轮](with-policy-02/verification.json)均完成 N01–N06，各有 6 个 `occupies_sidewalk`、3 个 `unknown`。两次均为 `uncertain`，规则守卫记录了原图 EvidenceAsset 引用不可用。该行为用于核对修订前后的证据门槛。

最终正向运行有一个 Qwen 观察被桥标记为 `qwen_completion_incomplete`。该对象未产生 Fact，按 `required_fact_unavailable` 保留为 `uncertain`。原始调用的 upstream HTTP 为 200，记录了 3,991,667 字节请求、51,038 字节响应及两者摘要。响应在 73.617 秒内返回，原 wire 没有记录 choice 的结束原因。Runtime 请求没有显式设置 `max_tokens`，模型登记接口报告 `max_model_len=16384`。[原始失败 wire](registered-policy/provider/live-a51865eff19144538e1bf88a8e9fbe0b.json)

在两次正式执行验证完成后，使用原 MediaStore 的图片、提示和 Schema 重组请求，核对 Runtime 与 Qwen 请求的两个字节摘要后，另发一次独立诊断。Qwen 返回 `finish_reason=stop`，报告 1,991 prompt tokens 和 1,565 completion tokens，原文为 `occupies_sidewalk`。新调用只补充供应商响应证据。最初调用的结束原因仍未确认，正式 Decision 保留在原 Execution。[诊断结果](failed-observation-diagnostic/diagnostic.json) · [诊断脚本](repeat_observation_diagnostic.py)

## 复现与测试

在仓库根目录使用锁定的开发依赖、Docker、已经运行的 WeVis 和 Qwen 服务。请使用空的新输出目录。

```bash
export PYTHONPATH=.:src PYTHONDONTWRITEBYTECODE=1
IMAGE=planning/implementation_evidence/LIVE_THREE_TASKS_20261119/images/goods.png
EVIDENCE=planning/implementation_evidence/LIVE_THREE_TASKS_FIX/goods
OUT=$(mktemp -d)
flock /tmp/jimu-live-three-scene-gpu.lock python -m examples.live_inspection.goods.run "$OUT/with-policy" "$IMAGE"
flock /tmp/jimu-live-three-scene-gpu.lock python -m examples.live_inspection.goods.run "$OUT/no-placement-policy" "$IMAGE" --omit-placement-policy
python "$EVIDENCE/verify_goods.py" "$OUT/with-policy" --compare-to-no-policy "$OUT/no-placement-policy"
python -m pytest -q tests/contract/test_live_discovery_routes.py
```

两次最终运行、证据核对、签名 Bundle ZIP 完整性及各轮 SHA‑256 检查均退出 0。适配器回归 **15 项通过**，包括明确无命中、未解析返回、错误坐标、未绑定 Profile、HTTP 失败和 Qwen 截断拒绝。基线为 `main` 的 `36885d61109ba1f55c42ce65b95da701dc71c326`，运行包含本轮工作树改动。实测使用 RTX 4090、NVIDIA Driver 580.178.04、CPython 3.12.3、PostgreSQL 16.14、Pydantic 2.12.5、FastAPI 0.141.1、SQLAlchemy 2.0.52、psycopg 3.3.5、Pillow 12.3.0 和 HTTPX 0.28.1。现场业务规则、其他货物类型与模型精度仍需单独评测。[回归测试日志](discovery-tests.log)
