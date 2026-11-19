# WP-05 封存独立验收

本地验收覆盖可编译违停场景、Task intake、正式 Worker 的六节点、PostgreSQL 权威链、原件与具名工具证据，以及重启和更换 Release 后的结果。独立 runner、输入及日志由本目录维护。产品实物由主控封存，基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`。清单包含 930 个产品文件，其中 91 个场景目录文件。与 WP-04 相比新增 146 个、修改 34 个、删除 0 个文件。 [封存目录](../primary_freeze_124bfcc1984c/) [文件差异](frozen_run_0djpMMzw/product-delta.log)

- 产品清单 SHA-256 `124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad`，ZIP SHA-256 `3f794542a79de241dd0ad1c2abb13596202931d5a84ab323cb08cf381cc77ac6`。
- 从封存 ZIP 解包，在 Linux x86_64、系统 CPython 3.12.3、uv 0.9.15 下使用离线锁定环境。运行探针从另一个环境安装非 editable wheel，wheel SHA-256 `405da1f31fa2c9efeebd485ee5e6a77ceb0e93e6ffe820712c82a907d91dbb6e`。独立 PostgreSQL 16.14 镜像 SHA-256 `4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae`，使用回环 HTTP 和真实 CPU 子进程。 [环境](frozen_run_0djpMMzw/versions.log) [Wheel](frozen_run_0djpMMzw/wheel-sha.log) [数据库及迁移](frozen_run_0djpMMzw/installed-intake.log)

```bash
bash planning/implementation_evidence/WP-05/independent/run_acceptance.sh \
  planning/implementation_evidence/WP-05/primary_freeze_124bfcc1984c/product_sha256.json \
  124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad all
```

## 运行证据

21 个 Task 到达终态，实际观测到 76 次回环 POST、52 次 CPU 工具 `execve`，并通过独立读回核对 52 份具名 sidecar。终态有 15 个合成违规及 6 个 uncertain。合计仅统计完整运行探针，入库用的两次回环自检另外留证。 [运行日志与 CPU 追踪](frozen_run_0djpMMzw/)

| 条件 | 独立结果 |
|---|---|
| 实际入口与版本 | 原始 PNG 摘要、签名、Bundle 激活、Task 幂等、跨 client 和错误媒体拒绝。PG 新建数据库实际应用 001–008，重复执行无迁移。 [入库](frozen_run_0djpMMzw/installed-intake.log) |
| 零 Object | Profile 无命中、成功定位无对象、越界定位返回、HTTP 超时分别保留诊断和派发差异。四条都 uncertain，CPU 启动为零。 [零对象](frozen_run_0djpMMzw/zero-objects.log) |
| 单 Object 和多 Object | 单对象违规。双对象完整场景分别为违规和无违规。部分覆盖按规则保留已有违规，另一个对象 uncertain。20 个双标签像素进入两张掩模，几何结果为 `blocking_tactile_paving`。 [业务判断](frozen_run_0djpMMzw/business-verdicts.log) |
| Question 与候选 | 缺槽生成为 error 和 `perception.question_missing`，显式 `insufficient_visibility` 生成为 unknown。坏整包无权威且只重发一次。候选为空、未配置、HTTP 503 或真实超时时仍逐条扫描强制 Rule。 [缺槽、重试和候选](frozen_run_0djpMMzw/missing-and-retry.log) |
| 历史 Release 与撤销 | 原进程真实 SIGKILL。另启进程将 B 的新 Task 写入新的 Device、Camera 修订。旧 A 按原 Release、Context 和原图继续。当前旧部署已撤销的路径保存真实拒绝回执且零物理派发。 [N01](frozen_run_0djpMMzw/restart-N01.log) [N04](frozen_run_0djpMMzw/restart-N04.log) [撤销](frozen_run_0djpMMzw/restart-N01_REVOKED.log) |
| 跨进程 N04 重试 | 首批坏响应已经落库才终止进程。新 Worker 只有一次额外的观察 POST，保留 `previous_batch_ref`，只选择成功的一轮。 [N04 整批恢复](frozen_run_0djpMMzw/restart-N04-retry.log) |
| N05 恢复和签名预算 | 首个子步骤已提交而第二步尚未分配时终止进程，旧工具无需重发。第二步已分配但未发生物理发送的窗口中，预算为 1 时恢复得出 uncertain；经重新签名批准的测试策略版本 1.0.1 允许新 child Attempt 且旧 child 对应零物理发送。 [已提交首步](frozen_run_0djpMMzw/restart-N05.log) [预算一轮](frozen_run_0djpMMzw/restart-N05_ADMITTED_NO_RETRY.log) [预算两轮](frozen_run_0djpMMzw/restart-N05_ADMITTED_RETRY.log) |

金标仅用于终态后的独立对照。审计以实际收到的 HTTP body、提供方 request ID 和 Scope 对账 PG，重新读取原始和规范化响应、原始图片、联合模型提示和三 Question Schema、两张掩模及几何证据。具名掩模按整张像素缓冲区对照，校验派生坐标及几何证据引用的两个具体祖先。已知结果的 guard 只能引用本对象已选 Attempt 的事实和受控证据。跨 Scope、跨 Object 替换均受到校验。 [独立审计](audit.py) [独立输入和金标](SCOPE.md)

## 静态及回归证据

Ruff lint、428 个产品与场景文件的格式检查、182 个文件的 strict mypy 和 `git diff --check` 均通过。Schema 检查、两次导出、与冻结 ZIP 对照均通过。347 份 Schema 和索引与 WP-04 逐字节一致。 [Ruff](frozen_run_0djpMMzw/ruff.log) [格式](frozen_run_0djpMMzw/ruff-format.log) [mypy](frozen_run_0djpMMzw/mypy.log) [Schema](frozen_run_0djpMMzw/schema-wp04.log)

63 项谓词定向测试通过，0 失败、0 错误、0 跳过。字段类型、比较、逻辑与上下文要求的既有 WP-05 合同覆盖由该套件提供。 [谓词 JUnit](frozen_run_0djpMMzw/predicates.xml) [命令与退出](frozen_run_0djpMMzw/predicates.log)

历史 WP-01B Schema 契约依赖从封存 seal 锚定的测试清单，正式 runner 核对后只复制到临时解包路径。开发预检及整改记录另存。 [历史输入定锚](frozen_run_0djpMMzw/history-before.log) [预检](PREFLIGHT.md)

完整 `python -m pytest` 显式包含 `tests` 和可编译场景目录，3,880 项通过，其中 24 项为场景示例测试。0 失败、0 错误、0 跳过，耗时 1,289.39 秒。整条 `all` 于 2026-11-18 07:37:10 UTC 退出 0。43 份逐项日志均以 `exit=0` 结束。完成后再次核对 930 个产品文件、ZIP、独立输入清单和临时历史输入，摘要一致。 [全量 JUnit](frozen_run_0djpMMzw/pytest.xml) [命令与退出](frozen_run_0djpMMzw/pytest.log) [产品后验](frozen_run_0djpMMzw/verify-after.log) [独立输入后验](frozen_run_0djpMMzw/probe-inputs-after.log) [历史清单后验](frozen_run_0djpMMzw/history-after.log)

## 验收结论

**本地独立验收 PASS，RUNTIME_VERIFIED。** 数据库迁移仅在新建隔离数据库执行。轮次审计和附加工件访问按 project/client 与精确身份校验。外部 Provider、目标 GPU、代表性实拍质量、部署认证和用户业务验收均为 UNVERIFIED。
