# WP-02 独立冻结验收第 1 轮

工程结论 `FAIL`，冻结产品 668 个文件。验收起止为 2026-11-18 03:02:41 至 03:08:11 UTC。全部命令前后均匹配冻结 SHA-256 `7f4005d91fca32246f3344edecc834fa3e2c7ac53214a4dfddb55d88f77457b8`。[运行汇总](frozen_r4hblz8g/run_result.json) · [实际命令及环境](frozen_r4hblz8g/commands.json)

- Code。Ruff lint、Ruff format 和 mypy 均退出 0。Schema Index 的关闭式诊断需要整改。来自其他目录的索引被拒绝，但低层 `schema_reference_source_missing` 泄漏到公开的 schema view 层，绕过了预期的 `schema_reference_index_mismatch`。证据显示低层先检查 source 对象身份，再到 view 层对 target 检查。建议在入口对整个索引的目录身份做显式检查。[失败堆栈](frozen_r4hblz8g/pytest.stdout)
- Test。`pytest` 3187 通过、1 失败、0 错误、0 跳过，退出 1，耗时 280.71 秒。唯一失败为 `test_projection_does_not_take_local_alias_target_from_a_different_catalogue`。两次 Schema 导出和 `--check` 各退出 0，347 份 Schema 和 1 份 `index.json` 在两份导出、冻结和 C 基线间逐文件相同。本轮汇总误将生成目录中的 348 个文件全部当作 Schema，故记录的 schema 汇总布尔值为 false。保留该原始记录，后续独立 runner 按 `*.schema.json` 单独计数并继续校对全部 348 个文件。[JUnit](frozen_r4hblz8g/pytest.xml) · [逐文件摘要](frozen_r4hblz8g/schemas_comparison.json)
- Isolated Runtime。`RUNTIME_VERIFIED`。独立探针退出 0，记录两版合成停车制品、两份真实本地 Ed25519 验签与精确 ZIP 读取，旧授权摘要下改源字节、空信任、缺独立来源证明、无签、带有效签名但缺 Producer、资源篡改、未授权激活、越过权限指定版本以及 SQLite BLOB 损坏均按预期拒绝。两个 Task pin、历史 Release、审批及激活记录跨切换、回滚和重开保留。[纵链标准输出](frozen_r4hblz8g/isolated_runtime.stdout)
- Production Quality。`UNVERIFIED`。真实签名信任根、生产批准人和实际激活、设备、真实模型／Tool、标注数据质量及目标机容量依有权负责人和后续 WP 的验证。
