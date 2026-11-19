# V1 本地 API 交接

受信宿主通过 `jimu_runtime.api.app.create_app` 注入认证、准入、回放与证据读取服务，再将 ASGI 服务绑定到允许的地址。配置全部端口的应用从同一工厂两次独立生成一致的 OpenAPI 3.1.0，包含 15 个操作、278 个组件。所有本地引用均已解析，每个操作均声明 Bearer。生成时只装配路由；接口实际行为的依据为冻结包的 ASGI、PostgreSQL 和回环 Compose 测试。[OpenAPI 快照](../../implementation_evidence/WP-07/api/all_ports.openapi.json) · [服务工厂](../../../src/jimu_runtime/api/app.py)

| 调用场景 | 合同与所需权限 |
|---|---|
| 提交和查询任务 | 宿主先核验设备、客户端、Region 及新 RGB 的摘要并登记原图，调用方再提交 TaskSubmission。作用域取自已鉴权的项目和客户端，原图引用、资源归属及动作由服务再校验。相同幂等请求取回稳定受理回执。状态、取消、结果各有独立授权。 |
| 获取 Execution 与 trace | 使用获准的 Execution 身份请求。trace 返回有限字段与固定高水位。持续拉取时复用返回的 through_event_number 及上页末序号，受记录和字节配额限制。 |
| 重算评分或发起重推理 | Artifact Replay 的完整历史清单由受信宿主通过 prepare_request 取得。HTTP 受理已有精确 pins 的请求，评分内容从受信服务按 EvidenceRef + digest 重读。Inference Replay 返回新的 Execution 身份，两类操作使用独立授权与幂等键。 |
| 取证 | 权限绑定实际 Scope、Execution、Attempt、证据种类和摘要。raw、normalized 与迟到回执分别授权，大小在打开文件前核验。只读评分记录使用专属的回放服务。 |
| 准入、注册与激活 | 已配置管理端口的部署可以提交签名 Bundle、查阅结果、注册和查询 Release、按具体 ActivationScope 批准或激活。管理权由宿主核验。 |

上游批准人与最终激活权、生产凭证、原始响应可见范围由部署方配置。默认合成 Compose 只开放其示例所需端口，RGB 登记使用受信宿主入口。Bundle 单独 validate、Release 单独 accept/deprecate 仍是候选；它们需要界定副作用、权限与对新受理/历史回放的影响。公开上传及 HTTP 自助准备回放清单和读取评分的协议也留待对外集成时确定。

此 OpenAPI 对应产品清单 `2a013b38fd05125a54b1b8126932a75f2b20849d8d148efbb0b2b3793ce53d68`。JSON 快照 SHA-256 为 `8905fde409e2b746ff8e4484d887582e5264a11cf1ad42d4d52fd63b14e9199f`。实际部署可调用其已装配应用的 `openapi()` 导出对应子集。
