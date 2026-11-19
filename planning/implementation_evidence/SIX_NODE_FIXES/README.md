# 六节点合并前整改

本次修复基于 detached 工作树的 `2552affeb69fbc2594fe70724f895b0db0967d47` 及其上尚未提交的 WP-07 产品。比较基线为 WP-07 封存 ZIP，SHA-256 为 `a458149665893542f29b71e48e9d4d4fbf6c9025f231a86c557a4bc59c14d1c9`。旧封存描述当时的产品。此次验证记录和增量文件清单随本报告独立保存。

## 把主体定位结果交给两个下游

在选定来源所用的 Profile 上配置 `grounding_grouping`。逐框检查可用 `{"mode":"per_box"}`。临近目标一起检查可用 `{"mode":"connected_boxes","max_gap_pixels":8}`。8 是示例值，现场配置须针对原图像素尺度选定间距；不同视角可固定不同版本的 Profile。同一 Profile 内两个矩形的最短欧氏距离小于等于该间距时连边，连通分量形成观察组。因此 A 临近 B、B 临近 C 时三者同组。逐框模式保留同坐标的重复检测，每一项有独立 Object 身份。

N03 为每个启用标记图的 Object 保存成员框、原图外接框及独立的全尺寸 RGB PNG，PNG 只绘制该组的一个红色轮廓。持久记录同时保留原图引用、标记图引用、摘要、字节数和渲染器版本。N04 将该 PNG 连同本组有效 Question 发给模型；N05 使用相同 Object 和标记图引用定位观察范围，几何计算读取原始像素。N06 以同一 Execution、Profile、Object 关联两路证据。组内不同发现是否必须归属同一个物理目标仍沿用先前的待办。

旧 Profile 省略新增字段时继续使用旧版逐框定位、原图加 ROI 的请求路径。启用新模式需要生成新 Profile 引用和固定它的 Bundle/Release。在途 Execution 继续使用其原版本。

## Question 的联合响应

模型一次提交本组各 Question 的答案。已知值支持原有枚举字符串、JSON 布尔值和声明字段的闭合 Record。不可见标记 `"insufficient_visibility"` 必须被响应契约允许。举例，布尔题的响应 Schema 可声明 `{"anyOf":[{"type":"boolean"},{"const":"insufficient_visibility"}]}`。回复按题槽、每题响应约束及主 Fact 类型验证。已知的 `false`、不可见的 `unknown`、缺失必需字段造成的 `error` 分别留存，合法兄弟题保留各自结果。

## N05 就绪与 N06 汇合

工具的输入绑定、跳过策略引用的 Question 以及步骤的祖先依赖共同决定就绪顺序。纯 N03 步骤可先执行，依赖观察结果的步骤等待本组 N04 终态并消费显式选定的 Fact。晚到或缺失权威结果走缺证路径。N05 父任务保留其创建时的输入，后取得的 Fact 和权威选择作为对应子步骤输入留痕。混合计划等候 N04 时，数据库记录本次依赖等待并释放 worker。N04 达到终态后，同一 NodeRun 在新租约下继续，按已留存的子步骤结果恢复。此类等待至多一次，单独留痕，不占用已配置的失败重试额度。服务使用两个有界的队列轮询任务，PostgreSQL 租约、Attempt 和资源预算继续控制执行。N06 等候两路终态，按证据有效性执行原有规则。

## 升级与恢复

- 新读取端兼容历史 Profile、Object 和六节点消息。新配置使用 Schema 索引中带 `supersedes` 的当前导出。导出的 `1.1.0` 表示 Schema 修订；省略新字段的旧线格式及其内容摘要保持稳定。旧版严格校验器需要切换到相应的新导出，才能读取含新字段的数据。
- 数据库按顺序追加 `012_marked_frame_media.sql` 和 `013_observation_dependency_wait.sql`。发布前应先备份并在受控环境应用迁移，再启用使用新配置的 Release。历史记录无需回填。回退运行配置时可激活之前的 Release，部署迁移 013 时先停止接单并排空旧 worker，再用同一新版处理暂停中的任务，避免旧调度器提前领取等待中的 N05。已开始的新格式 Execution 仍应由兼容 worker 收口。保留新表可供回放和恢复核验。
- 标记图文件按租户、Execution 和 N03 Attempt 暂存。权威 Object 固定后才可读取。取消、重试、文件篡改、错误客户端及备份恢复分别纳入隔离测试。

## 审核与回归入口

| 复核范围 | 实际入口与判据 |
|---|---|
| N03 到 Decision | `tests/contract/test_grouped_inspection_worker.py` 使用签名 Bundle、PostgreSQL、CPU 工具和回环 HTTP。逐像素核对模型收到的 PNG，并尝试替换原图或另一组 PNG，核查拒绝前后没有额外网络调用或 dispatch intent。 |
| N04 分题验证 | `tests/contract/test_n04_mixed_observation.py` 从真实联合请求、响应和 journal 回读检查 Boolean、Enum、闭合 Record、未知和错误分题。包括 Pydantic 生成的可见性分支、低字符串长度上限和数值精度边界。 |
| N05 并发与复用 | `tests/contract/test_wp05_mixed_tool_plan.py` 验证延迟 N04、N04 失败、重启、工具失败和条件三值。 `tests/contract/test_wp03_jobs.py` 验证等待超过原 Attempt 期限仍保留正常重试额度，过期租约失去发布权限。 |
| 存储与发布契约 | `tests/contract/test_n03_marked_frame_media.py` 核查暂存、回收和持久记录摘要与内容 pin 的各自语义。`tests/contract/test_c_schema_inventory.py` 核查历史导出和新修订。 |

本轮定向原生 diff 审查涉及持久化、N04 分题、N05 调度及子步骤真实性。已对照代码核实并处理可达问题，包含回收时匹配完整 Object 摘要、恢复时核对存储身份、区分数据库 JSON 摘要与 canonical JSON pin、支持 Pydantic 字符串常量分支、可见性标记优先处理，以及持久等待、子步骤输入和恢复资格。历史 Object 合法形式省略未设置的字段，显式 JSON null 仍按已有契约拒绝。最终原生定向复核覆盖调度及汇合关键 diff，全平台质量评测另需现场数据。

隔离验证使用 Python 3.12.3、Pydantic 2.12.5、pytest 8.4.2 和本地 PostgreSQL 16。真实 Provider、GPU、实拍准确率和生产迁移均为 `RUNTIME_NOT_RUN`。当前 N05 可从固定的 Context、同组已选 Question Fact 和已选祖先工具结果求值。依赖未绑定系统输入的条件保留缺证。

## 最终结果

最终收集清单与八份 JUnit XML 逐项匹配，4,026 项全部通过，0 跳过。产品范围 Ruff、格式检查、strict mypy、Schema 导出校验通过。347 份已发布 Schema 保持原字节。签名 Bundle、PostgreSQL 16、CPU 工具与隔离 Compose 的本地运行验收通过。

两处旧测试的准备过程也已修订。迁移回归通过 001–005 版本的真实表结构写入旧回执，再升级查询；进程中断回归先启动子进程，再发放原有 2.5 秒租约。早期并行检查曾因使用当前调度器访问旧表，以及进程启动时间超过短租约而失败。原回执、fence、租约和崩溃后权威性断言都保留。两份相应的完整测试分组已重新通过。

[变更路径及逐文件摘要](changed_files.json) · [验证结果与环境](verification.json) · [复验命令](commands.txt)
