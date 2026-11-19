# WP-07 本地 CPU Compose

该项目在一个隔离的合成租户中运行正式 HTTP API、IntakeService、inspection_worker 和两种回放服务。HTTP API 只发布到宿主 IPv4 回环。PostgreSQL 只在 Compose 私有网络中可达，合成 Provider 只在 runtime 容器的回环地址监听。三个几何步骤使用正式 Tool 路径和本地 CPU。

## 重建与验收

需要 Linux x86-64、Docker、Compose、uv、已缓存的 CPython 3.12、锁定 wheel 和下列已加载镜像。使用仓库内的 `ops/compose/local-docker.sh`，它仅连接 `/var/run/docker.sock`，忽略 Docker SSH/TCP context、远端 builder 和 TLS 环境变量。构建从目标 manylinux wheel 安装依赖，在镜像内用非 root 用户验证原生扩展，并以离线模式运行。首次尝试前请确认本机缓存。

| 用途 | 本地镜像引用 |
|---|---|
| Python | `python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |
| PostgreSQL 16 | `postgres@sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae` |

```sh
uv sync --locked --offline
JIMU_RUN_COMPOSE_TEST=1 uv run --locked --offline pytest -sxq tests/contract/test_wp07_compose_local.py
```

验收生成单独的项目、卷、数据库 UUID、镜像标签及随机凭据。针对新生成的合成 RGB 图像核验注册摘要、受理权限、结果、trace 和原始证据。测试读取 artifact 回放评分，通过实际派发计数验证不产生模型或工具调用，并在新 inference Execution 仍未完成时中断 runtime。重启后验证该 Execution 能完成，以及源任务、结果和证据保持一致。测试还模拟首次 DB 初始化在提交角色和建库后的进程中断，确认重试只接受自己的引导记录，并检查 app 角色越权时拒绝就绪和迁移。

## 手工启动

在仓库根目录执行 `ops/compose/build.sh jimu-wp07-local:<unique-tag>`，使用它打印的应用镜像 ID 核对本次构建。设置 `JIMU_APP_IMAGE` 为该唯一镜像标签，`JIMU_DB_INSTANCE` 为独立 UUID，`JIMU_SECRET_DIR` 为本次项目私有目录。该目录模式应为 `0700`，包含 `admin_password`、`owner_password`、`app_password`、`fixture_token` 四个内容各异的随机值，例如用 Python `secrets.token_urlsafe(32)` 生成。Compose 本地 file secret 是 bind mount，凭据文件须允许容器的非 root UID 读取，例如使用 `0444` 并由外层目录限制访问。同一组卷重新启动时须保留数据库 UUID 和对应凭据。

设 `PROJECT` 为从未使用的项目名，例如 `jimu-wp07-<unique-suffix>`。不设置 `JIMU_API_PORT` 时，Docker 分配随机回环端口。

```sh
ops/compose/local-docker.sh compose -p "$PROJECT" -f ops/compose/compose.yaml up -d --wait --wait-timeout 70
ops/compose/local-docker.sh compose -p "$PROJECT" -f ops/compose/compose.yaml port runtime 8080
ops/compose/local-docker.sh compose -p "$PROJECT" -f ops/compose/compose.yaml exec -T runtime \
  python /opt/jimu/host/synthetic_host.py submission
```

默认启动时，受信合成 host 通过正式 MediaStore 预先注册仓库附带的 PNG。最后一条命令输出的 JSON 携带作用域和原件摘要，提交到 `POST /api/v1/inference/tasks` 时应带 fixture Bearer token 以及同一 `X-Jimu-Project`、`X-Jimu-Client`。HTTP 受理只接受这个 host 验证过的 scope、原件及捕获上下文。设备填写的媒体摘要、设备 ID 或相机 ID 无法自行授权，也没有新增网络上传路由。

### 注册新生成的合成 RGB PNG

用受信宿主生成或取得 64×48、16 KiB 以内、与当前已签合成场景坐标相符的 RGB PNG。比如本地从合成场景图重新生成 PNG，并只改变背景像素保留几何关系。审核图像后，由宿主把 `JIMU_APPROVED_FRAME` 设为其绝对路径，并确保非 root runtime UID 可通过只读 mount 读取文件。用新项目名运行上述命令，每次都追加 `-f ops/compose/approved-frame.yaml` 到 `compose.yaml` 之后。启动时，合成 host 消费这一明确授权的文件，通过正式 MediaStore 验证 RGB 与尺寸、按固定作用域和 SHA-256 持久注册，`submission` 打印与新文件匹配的 TaskSubmission。前端客户端不能上传或选择该挂载。测试展示了这一完整过程。保留已启动项目的同一 approved image 文件直至清理。此路径仅适用于受信合成场景，不能据此推断其他图片的结果或 Provider 质量。

### 两种回放

受信操作者通过同一项目的 `exec -T runtime python /opt/jimu/host/synthetic_host.py` 执行以下本地命令。命令绑定当前合成租户和注册图像。请求体及评分读取只对能操作此本地项目的宿主开放。

| 操作 | 本地命令和 HTTP 请求 |
|---|---|
| Artifact 回放 | `artifact-request <source-execution-id>` 调用正式 `ArtifactReplayService.prepare_request` 生成已核验输入 pins。带独立 `Idempotency-Key` POST 至 `/api/v1/executions/<source-execution-id>/artifact-replay`。以返回的 ID 调用 `artifact-score <replay-run-id>` 经 `read_scoring_evidence` 验证并读取评分。 |
| Inference 回放 | `inference-request <source-execution-id>` 读取历史 pins。带独立 `Idempotency-Key` POST 至 `/api/v1/executions/<source-execution-id>/inference-replay`。HTTP 202 返回不同的 `target_execution_ref`，可通过正式 Execution 和 trace API 观察同一输入被重新执行。 |

`prepare_request` 仍是受信宿主接口。HTTP 回放使用已有 scoped API，评分读取走本地宿主的只读评分服务。

只停止服务时，使用相同项目名及配置执行 `stop`。确认整个项目属于一次性验收后，才使用 `down --volumes --remove-orphans` 删除该项目数据。

## Host 接入边界

`jimu_runtime.deployment.service.mount_service` 接收由受信 host 装配的 API app、Worker、数据库实例身份及明确的租户队列。生产场景需要独立供应身份和权限、签名信任、Provider/工具注册、数据库身份与凭据以及网络策略。这里固定回环发布，runtime 通过额外入口网络接入，数据库仅在 Compose 私网。独立一次性迁移容器持 bootstrap 权限，runtime 仅持最小权限 app role。PostgreSQL 与运行时 Bundle/Release、ResourceAdmission、媒体文件保存在独立的专属卷。指标、备份和恢复使用各自的运维入口。
