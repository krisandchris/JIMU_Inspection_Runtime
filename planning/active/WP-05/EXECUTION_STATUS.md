# WP-05 本地实施记录

产品基线是 WP-04 经独立验收的冻结清单 924eb788eab11bed400ec1f3602a9a30d3df7b43cba79e8601901eb7921f7495。正式验收需从受理图片到保存 Decision 走通完整六节点，并在新进程重开。当前验证针对下列切片。

| 切片 | 已取得的证据 | 后续核对 |
|---|---|---|
| 规则谓词 | 主控核对 668 个上游原件、7 个新增文件及实际差异。原生审查两项已复核修复。63 个新增用例通过。[测试和交接清单](../../implementation_evidence/WP-05/predicates/README.md) | 与同次 N06 选择的 Facts、证据检查及 staged Decision 联测。 |
| 可编译场景 | 场景原始 87 文件经原生审查整改。现有候选响应包含模型实际提供的 category_id 和 score，显式不可见与缺题分开；重开用例先激活不同 Release B，再按原 Task 检查 A。主控在同一版本运行 24 个场景和 18 个几何测试，42 项通过。[文件清单](../../implementation_evidence/WP-05/scene/after_review_sha256.json) · [JUnit](../../implementation_evidence/WP-05/scene/scene_geometry_after_review.xml) | 在正式 Worker 中实际运行整条 Task、获取 tool sidecars 和物理调用回执。 |
| 媒体与证据检查 | 主控从 WP-04 封存基线逐文件核对，添加 Pillow 12.3.0 及作用域绑定的媒体适配、迁移 007、真实像素验证和 Rule guard。新建 CPU 环境运行媒体、guard 及历史迁移定向回归，52 项通过。[JUnit](../../implementation_evidence/WP-05/media/first_integration.xml) | 接线时同一事务快照核对任务、权威 Attempt、各命名证据及实际字节。 |
| 工作流 | N01–N04、几何 ToolNode、N06 与共享 Worker 各有单一写入负责人。独立验收人维护合成金标与回环响应，运行证据存放于 implementation_evidence/WP-05/independent。 | 逐节点验证真实输入、失败与重试、证据追踪、无候选时 mandatory 扫描，完成冻结后的全量回归和独立验收。 |

原生 code review、主控测试、独立流水线验证各自留证。真实 Provider、目标 GPU、实拍标注与模型质量待已约定的后续授权。Git 发布另行授权。

独立验收人在具有独立 SHA-256 的非 editable wheel 上运行了正式六节点入口。已确认一张合成图在空候选下仍被登记为违规，PG 权威记录 6 条，实际 4 次 HTTP POST、3 次 CPU 工具启动，3 份工具原件均可读回。单独的 Release/进程恢复用例中，N04 首批坏响应后只整批重发一次；N05 在第一步持久化后重开不会重派该步骤。交叉核验合成图中 20 个双标签像素同时进入两张独立派生掩模，得到 blocking_tactile_paving。另一组独立轮次确认 no_hits、no_target、无效定位及定位超时产生 uncertain；运行期撤销旧部署后观察到零次新物理派发。这些轮次使用各自记录的预检快照；WP-05 正式整包验收尚待最终冻结。[首个完整案例](../../implementation_evidence/WP-05/independent/wheel_preflight_8q7xlIe9/workflow.log) · [盲道掩模](../../implementation_evidence/WP-05/independent/wheel_preflight_3ZYeSVrI/overlay.log) · [N04 整批恢复](../../implementation_evidence/WP-05/independent/wheel_preflight_3ZYeSVrI/restart-N04-retry-03.log) · [N05 子步骤恢复](../../implementation_evidence/WP-05/independent/wheel_preflight_3ZYeSVrI/restart-N05-pre-admission.log)
