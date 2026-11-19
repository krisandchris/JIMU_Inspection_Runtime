# WP-07 独立本地验收

使用主控提供的 product.zip 和外部给定的 product_sha256.json SHA-256。运行器逐成员校验 ZIP，再在独立私有目录安装锁定依赖。WP-01B 历史测试只从已知 SEAL.json 校验并复制一份 after_sha256.json。解包后的 Compose shell 入口设置为 0755。其他产品文件设置为 0644，以供运行非 root 用户的容器读取挂载的配置。

```bash
bash planning/implementation_evidence/WP-07/independent/run_acceptance_v2.sh PRODUCT_MANIFEST MANIFEST_SHA256 --all
```

| 验收组 | 实际验证 |
|---|---|
| `--static` | Ruff 检查与格式检查，strict mypy，Schema 重导出两次，按 WP-06 封存清单核对 347 份公共 Schema 及索引，Observability Schema 对照。 |
| `--pytest` | 全量 tests 和场景示例，启用产品自带的 `JIMU_RUN_COMPOSE_TEST=1`。PostgreSQL 的授权、审计及备份恢复测试使用测试专属实例，Compose 端到端使用测试专属项目。 |
| `--runtime` | 以封存包构建离线镜像，在仅回环的本地监控项目验证 PostgreSQL、Prometheus 和 Grafana。用另一个隔离环境安装从封存包构建的 wheel，执行 Recovery 组预先交付的组合恢复探针。 |
| `--all` | 在同一份经校验的解包树执行上述三个验收组。 |

运行器保存每条命令、UTC 起止时间、退出码、JUnit XML、输入摘要和组合恢复的结果文件。临时数据库、端口、卷及测试凭证由实际测试和探针拥有，产品镜像从缓存的固定基础镜像离线生成。运行器仅连接本机 Unix Docker socket。验收完成后复核封存、解包树和探针输入的摘要。

监控探针沿用集成预检的 monitor_smoke.py，其来源摘要为 `f992fc907a7edf311540a9350232ed5c0306dfec4c9cfde95c34bfabc9db4700`。组合恢复探针使用工作包 recovery 目录当次交付的版本，源文件摘要随每次运行记录。测试范围覆盖 local synthetic PostgreSQL、CPU 与回环 Provider。pytest 子进程使用标准 `umask 022`，测试的凭证和临时私有目录各自显式限定权限。旧版 `run_acceptance.sh` 随对应执行证据保留。本地运行证据、工作包接受决定和真实目标环境验收分别记录。
