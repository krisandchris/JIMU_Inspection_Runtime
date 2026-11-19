# 冻结前探针调试记录

命令均在工作树运行。

```sh
PYTHONPATH=tests PYTHONDONTWRITEBYTECODE=1 uv run --locked --no-sync python planning/implementation_evidence/WP-02/independent/runtime_probe.py
```

| 工作树状态 | 退出码 | 观察 |
|---|---:|---|
| 探针刚组装 | 1 | 探针按位置构造只接受关键字参数的 `ReferenceSlot`，已修正调用。 |
| 生产方并行写入期间 | 1 | `producer_rules.validate_producer_rules` 调用 `_validate_rule_uses` 缺 `checked_uses`，已通报。后续工作树实物包含该参数。 |
| 语义链已接通 | 1 | 探针读取的检查字段名错误，已换为 `CompileReport.semantic_checks`。[标准错误](prefreeze_probe_attempt1.stderr) |
| 来源绑定补丁落地前 | 0 | 已走完真实本地验签、SQLite、版本切换和回滚。 [标准输出](prefreeze_probe_attempt2.stdout) |
| 来源绑定补丁落地后 | 0 | 探针适配 `SourceIdentity` 并加入留存旧授权摘要时拒绝输入原件变动的检查。[标准输出](prefreeze_probe_attempt3.stdout) |

这些运行用于调试独立探针和定位并行改动。正式验收使用主控冻结后的一轮证据。
