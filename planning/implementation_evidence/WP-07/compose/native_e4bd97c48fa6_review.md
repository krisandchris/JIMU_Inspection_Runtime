STATIC_REVIEW_ONLY：所贴 diff 明确将合成 Provider 绑定到回环地址，但未将 Docker 操作固定到本机，数据库首次初始化也不能从部分成功状态安全重试。沙箱未能启动只读命令，未独立核对冻结基线或未提供的 compose.yaml，因此不对实际端口发布、网络隔离和 secret 挂载作肯定判断。TEST_NOT_RUN / RUNTIME_NOT_RUN；未调用外部服务。

Full review comments:

- [P1] 在首次 Docker 调用前固定并校验本机 daemon — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/build.sh:12-13
  `docker image inspect` 已经会连接当前 Docker context 指向的 daemon。若用户选中了 SSH/TCP 远程 context，测试尚未检查 `127.0.0.1` 就已接触外部服务；后续构建和 `down --volumes` 也使用同一套用户配置。Python socket 补丁不约束 Docker 子进程。应让构建、Compose 和清理命令统一使用已验证的本机 socket，无法确认时在首次连接前退出。[AGENTS.md:41](AGENTS.md#L41)

- [P1] 让数据库首次初始化可在部分成功后安全重试 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/deployment/database.py:76-82
  `autocommit=True` 使每条 DDL 单独生效。若创建 owner 后遇到已有 app 角色或进程中断，重跑会因 owner 已存在而永久拒绝继续；若中断发生在 `CREATE DATABASE` 与写入实例注释之间，重跑又会把刚创建的库判为未知库。应在写入前检查所有冲突，并为不能同事务完成的建库步骤保存可验证的归属和恢复状态，而不是放宽对未知角色或库的检查。[AGENTS.md:135–136](AGENTS.md#L135-L136)

- [P2] 复用数据库时验证应用角色的实际权限 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/deployment/database.py:68-71
  已有库只要实例注释和 owner 名称匹配，`provision()` 就直接返回。若 app 角色后来被授予 owner 成员资格或 `SUPERUSER`，这项检查仍通过，运行中的 `database_ready()` 也只核对用户名而不会发现越权。应验证 app 角色属性及其有效成员资格，对权限漂移报错，而不是自动修改已有角色。

- [P2] 避免将数据库明文密码放入可记录的 DDL — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/deployment/database.py:82-85
  `sql.Literal` 能正确转义密码，但发往 PostgreSQL 的 `CREATE ROLE` 语句仍包含完整明文。启用 DDL 语句日志时，owner 和 app 密码会离开 `/run/secrets` 进入数据库日志。可在客户端生成目标角色的 SCRAM verifier 再提交 DDL，避免可重用的明文密码出现在语句中。[AGENTS.md:119](AGENTS.md#L119)

- [P2] 按目标镜像平台构建离线 Python 依赖 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/build.sh:20-22
  `uv sync` 按宿主机解释器、操作系统和架构选择依赖，随后脚本把整个 `site-packages` 原样复制进 Linux 镜像。即使 Python 都是 3.12，在 macOS Docker 主机或宿主与目标架构不同时，构建也可能成功，而原生扩展无法在容器内导入。应在固定基镜像内用离线缓存安装，或离线准备并校验匹配目标 ABI 和平台的 wheel。[AGENTS.md:174](AGENTS.md#L174)

- [P2] 显式设置非 root 运行用户读取构建产物所需的权限 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/Dockerfile:9-11
  在 `umask 077` 的宿主环境中，`cp` 新建的 `synthetic_host.py` 可以是 `0600`，中间目录也可以是 `0700`。Docker `COPY` 保留这些模式并默认使用 root 所有权，而镜像最终以 `10001:10001` 运行，导致构建成功却无法打开入口脚本。应显式规范镜像内代码和依赖的读取、目录遍历权限，不要因此放宽 secrets 或数据目录权限。

- [P2] 在 fixture 归档不完整时中止镜像构建 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/build.sh:29-30
  这里的 `/bin/sh` 管道只返回解包端 `tar` 的状态。若打包端因某个 fixture 文件不可读而报错，它仍可能输出结构有效但缺文件的归档；解包成功后，`set -e` 不会阻止镜像被打上正式标签。应分别检查打包和解包结果，避免将缺失场景资源的镜像报告为构建成功。

- [P2] 限制回环 Provider 的连接线程并提前设置超时 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/synthetic_provider.py:78-79
  `ThreadingHTTPServer` 为每个已接受连接创建线程，而 `settimeout(3)` 要到请求头解析完成、进入 `do_POST()` 后才执行。本机压测或异常客户端留下未完成的请求头时，线程可一直等待并持续累积；出站 transport 的 `max_concurrency=1` 并不限制监听端。应在读取请求行前设置超时，并在创建处理线程前限制并发。[AGENTS.md:126](AGENTS.md#L126)

- [P2] 为入口 HTTP 服务设置并发上限 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/synthetic_host.py:577-580
  `uvicorn.Config` 未设置 `limit_concurrency`。例如数据库变慢时，并发访问无需认证的 `/health/ready` 会让每个请求都创建等待数据库的 ASGI 任务；连接池只限制数据库连接数，两秒超时也不限制同时等待的任务数。应给单机服务设置明确的入口并发预算，使超额请求尽早返回，而不是与同进程 worker 争用资源。

- [P2] 用 Uvicorn 的实际启动状态判断服务是否成功启动 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/ops/compose/synthetic_host.py:586-588
  `config.loaded` 表示配置已加载，不表示 ASGI lifespan 启动成功。若应用报告 `lifespan.startup.failed`，`Server.serve()` 可以正常返回且 `config.loaded` 已为真，入口进程因此以成功状态退出，依赖失败退出码的恢复机制不会触发。应保留 `Server` 实例并检查其 `started` 状态。

- [P2] 在仍有未完成工作时验证重启恢复 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp07_compose_local.py:244-245
  测试取得最终 Decision、trace 和 evidence 后才重启，之后也只重读旧结果并重发同一个幂等请求。即使重启后的 worker 无法接管未完成任务，这些断言仍可能全部通过。应保留现有持久性检查，另在可确定的非终态边界中断服务，验证同一 Execution 能按租约和 attempt 契约继续推进，并核对最终收据与 trace，而不假设模型只调用一次。[AGENTS.md:93](AGENTS.md#L93)

- [P2] 让测试凭据清理独立于 Docker 清理结果 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/tests/contract/test_wp07_compose_local.py:269-271
  如果 `compose down` 超时、daemon 不可用或后续卷断言失败，`finally` 会在到达 `unlink()` 前退出，将含管理员密码与 Bearer token 的文件留在 pytest 临时目录中；清理异常还可能遮蔽原始故障。应从创建凭据时就注册独立的文件清理，并分别报告 Docker 资源清理失败。

- [P3] 校验 secret 文件结尾而不是接受合法前缀 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/deployment/database.py:22-25
  若文件以 512 个合法字符和一个换行开头，后面还有内容，`read(513).rstrip("\n")` 会静默接受前 512 个字符。这样应用使用的是截断凭据，而读取整个文件的其他组件可能使用不同密码。应在允许的长度和可选末尾换行之后确认已到 EOF，否则拒绝该配置。