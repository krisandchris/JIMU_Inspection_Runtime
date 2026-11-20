# N04 单图九框提示词与输出长度对照

使用历史合成货物图、N03 的九个框和同一人行道区域，向内网 Qwen3_5-9B 串行发送四次真实请求。每个方案一次，seed 为 42。客户端测量请求发出到完整响应的时间。四次均正常结束，通过各自的 JSON Schema 校验，返回所有九个框的结果。

| 请求 | HTTP 秒 | 输入 token | 生成 token | 其中思考 token | 四个有占道证据的框判对 | 五个证据不足的框返回不确定 |
|---|---:|---:|---:|---:|---:|---:|
| [原提示词，默认思考](original_default.json) | 43.47 | 2327 | 8311 | 8063 | 4/4 | 0/5 |
| [精简提示词，默认思考](compact_default.json) | 17.62 | 1838 | 3867 | 3679 | 3/4 | 0/5 |
| [精简提示词，关闭思考](compact_direct.json) | 1.18 | 1840 | 256 | 0 | 4/4 | 0/5 |
| [证据码输出，关闭思考](evidence_code_direct.json) | 0.68 | 1860 | 103 | 0 | 4/4 | 0/5 |

前三种共用原响应 Schema。第四种同时改变提示词和模型返回的 Schema，每框输出一个证据枚举，由实验脚本确定性映射回原答案。第四种的耗时变化不能单独归因于其中一个改动。证据码仅适用于允许这三个答案的实验问题。

参考来自[独立 AI 盲评](../N04_ORIGINAL_MULTI_REGION/blind_reference.json)。该图没有可确认的不占道框，无法估计这类样本的正确率。四次请求共用同一图像，未控制缓存和服务排队。单次耗时不能估计生产时延分布。N05、N06 和 Runtime 主流程均未重跑。

[原始输入](../N04_ORIGINAL_MULTI_REGION/measured/goods-input.json) · [精简提示词](compact.txt) · [证据码提示词](evidence_code.txt) · [请求与评分脚本](probe.py)

调用方式为仓库根目录下的 `.venv/bin/python planning/implementation_evidence/N04_PROMPT_LATENCY/probe.py` 和同一命令附加 `--evidence-code`。脚本保留已有结果，复跑需使用新的输出目录副本。此次实验仅使用本地模型服务。
