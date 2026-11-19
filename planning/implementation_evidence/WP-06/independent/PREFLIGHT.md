# WP-06 独立运行预检

从独立集成副本安装 WP-06 wheel，并另从 WP-05 正式 ZIP 安装历史 wheel。从旧 wheel 发起完成的 Execution，追加迁移后由新 wheel 按历史证据回放。验证命令保存首轮安装过程，最新一次使用同一固化副本，整个独立探针退出 0。 [首轮安装](preflight_jbgWlnYH/probe.log) [最新探针](preflight_S8ajv8ZX/probe.log) [副本产品摘要](preflight_S8ajv8ZX/product_sha256.txt) [旧包归档校验](preflight_S8ajv8ZX/legacy-archive.log)

- 旧 wheel 在新隔离 PostgreSQL 上实际应用 001–008，成功完成单 Object 的六节点。009–011 追加后，20 类历史 SQL 行及计数保持一致。再次执行迁移无新增。
- 两个 Artifact Replay key 产生两个评分记录。重复以及并发提交同 key 返回同记录。Trace 只观测到独立 Python 启动，零 HTTP、零工具启动，原 Task、Execution、job 和 dispatch 数量不变。重新连接后检查跨 Scope、错误分数 pin 和摘要。
- Inference Replay 同 key 并发仅新增一个 Execution 和初始 job。新进程用已激活 B 之后的时间点加载仍固定在 A 的请求，保留旧设备和相机。四次 HTTP、三个真实工具进程和三个具体原件逐一与 PG 对账。旧 20 类行不变。
- 离线评分以预先固定的合成标签与实际 WP-05 行构造数据集、预测、评分协议与 TrialSource。使用两种独立的合成人工角色演练复核。IoU 1，错误放行 0/1，自动放行 0/0 对应 null。演算输入和报告保存以便重算。
- 对原 Release 重新签入的一秒测试留存策略，hold 生效时实际文件在。释放后观测实际 unlink 并 SIGKILL，重开时为 pending。两个并发恢复返回同一个 absent_on_recovery completion。已过期的内容无法读取，历史元数据和原 Execution 保持原样。

最初一次处置预检发现独立查询误用 `one()`，同一几何 Evidence 出现在多个归档位置。最新探针检查多个副本的准确字节及摘要等价，并复验物理处置。历史 ZIP 校验也已定向执行，其 91 个场景成员一并核对。

## 正式历史导出入口增量

后续在一致的集成副本清单 `ee0704ab45447347f7df1ac358fb015cf797060d4b75c2f253c51114efd49cea` 上重新建包。正式 `HistoricalEvaluationExporter` 两次返回同一结果，独立在 PG 中核对 7 条派发审计、8 份 HTTP raw/normalized 引用和实际证据摘要。完整探针再次退出 0。自动放行缺历史观察，因此已标注违规数为 1，错误放行率是 null，自动放行风险 0/0 的值也是 null。处置后重新导出精确返回证据已过期，旧 Artifact Replay 分数仍可读，并无新增 HTTP。 [探针](preflight_AdmLcZsh/probe.log) [固定产品清单](preflight_tGUwuCIL/product_sha256.txt) [评分及历史导出](preflight_AdmLcZsh/independent-quality.historical-export.json)

同一副本 Ruff、455 个文件格式、199 个文件 strict mypy、Schema `--check` 和四项实际历史导出契约测试通过。早期针对原件归档的独立断言分别改为核对准确事实定义/引用和 RFC 8785 摘要。保留原失败日志，以区分产品与验收器断言问题。 [静态日志](preflight_tGUwuCIL/static.log) [历史导出 JUnit](preflight_tGUwuCIL/history-evaluation.xml) [早期断言](preflight_tGUwuCIL/probe-with-export.log)
