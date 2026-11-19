# WP-05 本地工程验收

**Code PASS · Test PASS · 隔离 Runtime VERIFIED。** 基线为 detached Git `2552affeb69fbc2594fe70724f895b0db0967d47` 上已验收的 WP-04 产品清单 `924eb788eab11bed400ec1f3602a9a30d3df7b43cba79e8601901eb7921f7495`。WP-05 最终清单 `124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad` 覆盖 930 个文件，较 WP-04 新增 146、变更 34、移除 0。可重开 ZIP 的 SHA-256 为 `3f794542a79de241dd0ad1c2abb13596202931d5a84ab323cb08cf381cc77ac6`。[文件清单和差异](../../implementation_evidence/WP-05/primary_freeze_124bfcc1984c/snapshot.json)

六节点流程现可受理一张合成 RGB 图片，按固定 Bundle 发现 Profile、独立定位、每 Object 联合观察、依预绑定 ToolPlan 持久化真实 CPU 工具结果，并生成带 Fact 权威、证据和覆盖记录的 Decision。空候选仍执行 mandatory Rule。部分覆盖时保留有证据的违规。错误、弃判、无命中及缺证分别保留原因。N04 重试、进程终止、旧 Release 重开和工具子步骤恢复均有实际回执。[节点及原生审查记录](EXECUTION_STATUS.md)

独立验收由纯封存 ZIP 构建并安装 wheel。21 个合成 Task 到达终态，其中 7 条覆盖 SIGKILL 和恢复。主控逐条解析终态审计，核对到 76 次实际回环 HTTP POST、52 次 CPU 子进程启动，独立验收人对账了 52 份工具证据。实际使用隔离 PostgreSQL 16.14 和可验证本地原件。两次导出及与 WP-04 的逐字节比较均确认 347 份公开 Schema 及索引保持一致。[运行探针、版本和限制](../../implementation_evidence/WP-05/independent/FROZEN_REVIEW.md)

主控直接解析全量 JUnit，3,880 项通过，0 失败、0 错误、0 跳过，含 24 项场景测试。另单独执行的 63 项谓词测试已计入全套。Ruff、格式、strict mypy（182 个源文件）、历史清单定锚及封存后复核通过。43 份实际命令日志均以退出码 0 结束。全量 JUnit SHA-256 为 `89cd6929c14e4e375fc31968683a62e6c57d2acd61c0c8c2d80d519d9e3fa331`。[全量 JUnit](../../implementation_evidence/WP-05/independent/frozen_run_0djpMMzw/pytest.xml)

早期快照的 7 项失败保留在预回归记录中。它们覆盖内部回执与公共 Schema 边界、临时测试目录缺少历史清单、旧步骤重开、证据落盘失败和仅 N04 允许的显式验证重试预算。正式测试复测了这些路径。新增 007/008 迁移只在隔离数据库演练，证据作用域、不可变引用、终态清理和晚到响应均复核。运行中的原有 Task 需要能读取其固定消息版本的 Worker。[整改与兼容说明](../../implementation_evidence/WP-05/worker/RECHECK.md)

代表性实拍数据、真实 Provider、目标 RTX 4090 容量、真实模型质量与生产就绪状态为 UNVERIFIED。用户业务验收待确认。未执行 Git 发布。WP-06 的本地组合以本次实际产品清单为基线。
