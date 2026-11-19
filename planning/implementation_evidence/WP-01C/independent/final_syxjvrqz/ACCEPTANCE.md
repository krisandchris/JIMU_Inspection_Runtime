# WP-01C 独立工程验收

**工程验收 PASS / TEST_PASSED，服务运行证据 RUNTIME_NOT_RUN。** 验收对象是主控冻结的 608 个产品文件，清单 SHA-256 为 a1aec5dc27cc99fa86c1efcfdfc9c026951e865b3e47a5f2a16eeaef4049a4d2。Git 基线为 detached HEAD 2552affeb69fbc2594fe70724f895b0db0967d47。独立运行开始、16 个命令各自前后、整轮结束及额外 G8 探针前后，产品新增、删除、变动均为 0。冻结范围包括 src、tests、scripts、schemas/generated、pyproject.toml、uv.lock，排除缓存、字节码、计划与证据目录。独立 tester 仅写入独立证据及临时目录。[主控冻结](../../primary_freeze_a1aec5dc27cc/snapshot.json) · [独立开始](pre_run.json) · [独立结束](post_run.json) · [执行结论](run_result.json)

C2/C3 交付 Rule/RuleSet、封闭操作符与三输入域静态类型检查。C4 增加版本化的 Bundle/Release、接入、媒体、执行、节点 I/O、感知、调用、适用性、Coverage/Decision、Replay 和有界记录关联。C5 扩充 Schema 索引与离线测试。B 的 162 个已发布 Schema 文件保持原字节，新增 C 根及索引扩展。没有数据库迁移。外部记录真实性、持久历史完备性及部署安全留待后续验证。[产品路径与摘要](../../primary_freeze_a1aec5dc27cc/product_sha256.json) · [C 契约盘点](../../../../active/WP-01C/CONTRACT_INVENTORY.md)

## 命令和环境

Linux x86_64、Python 3.12.3、uv 0.9.15、Pydantic 2.12.5、pytest 8.4.2、jsonschema 4.25.1、Ruff 0.14.14、mypy 1.18.2、rfc8785 0.1.4。锁摘要 10d7ad828a27a9ea35326ad95f73becedfdc9237bde8fc746a119833191062eb。使用已由实施者按原锁创建的 CPU 环境和 --no-sync 参数。逐命令精确 argv、UTC 起止、耗时、原始 stdout/stderr、退出码和产品摘要见记录。[环境](environment.stdout) · [锁定环境安装](../../cpu_setup_CwzLXKcJ/uv_sync.stdout) · [安装退出码](../../cpu_setup_CwzLXKcJ/uv_sync.exit) · [命令元数据](commands.json)

| 命令 | 独立结果 |
|---|---|
| uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q，附独立缓存、basetemp、JUnit 参数 | exit 0。2813 passed，0 failed/errors/skipped，223.34 秒。[stdout](pytest.stdout) · [JUnit](pytest.xml) · [统计](junit_summary.json) |
| uv run --locked --no-sync ruff check . | exit 0。[stdout](ruff_lint.stdout) |
| uv run --locked --no-sync ruff format --check src tests scripts | exit 0，203 文件。[stdout](ruff_format.stdout) |
| uv run --locked --no-sync mypy src/jimu_runtime | exit 0，84 源文件。[stdout](mypy.stdout) |
| uv run --locked --no-sync python scripts/generate_schemas.py --output，分别指定两个临时目录 | 两次均 exit 0，每份各 347 Schema 加 1 index。[导出一](export_first_sha256.json) · [导出二](export_second_sha256.json) |
| diff -qr 两份导出 | exit 0，stdout/stderr 均为空。[元数据](export_diff.json) |
| uv run --locked --no-sync python scripts/generate_schemas.py --check | exit 0，工作树生成物匹配。[stdout](export_check.stdout) |
| B 合并树的逐文件兼容检查 | exit 0，162 份 Schema 在工作树与两次导出中均保持原字节。B 原 index 摘要 ce0bc5b36eb5b8a3dac04adc2f3d97c558f0a10c4487678f018cbafcd7a476b0，C index 摘要 843f08cd7810b6313113f1dc1b8444f63a23a148b268d0a37a4ef26ceaf8cdb0。[检查记录](legacy_b_schema_bytes.stdout) · [精确 argv](legacy_b_schema_bytes.json) |
| 独立跨 Profile G8 探针 | exit 0，两个 Profile 可对同媒体、同坐标框、同 Entity 建立独立对象、GroundingRecord、ObservationBatch 与 Facts。交换 Profile 或移用另一批 Fact 均被拒。[代码](probe_same_target.py.txt) · [stdout](probe_same_target.stdout) · [元数据](probe_same_target.json) |

B 的历史附加命令 mypy scripts/generate_schemas.py 曾有 7 项实际诊断。本次使用批准的 mypy src/jimu_runtime。C 的 exporter 单文件 mypy 为 TEST_NOT_RUN。Exporter 的实际 CLI、双验证、字节比对与 Ruff 结果见本轮日志。[B 原始诊断](../../../WP-01B/execution_03/logs/e2/initial_mypy.json)

## G0–G12 结论

下表 case 名可在原始 JUnit 的 classname/name 中检索。协议与有限关联的验证适用于本次提供的 CPU 内存记录。

| 门禁 | 工程判定 | 证据与适用范围 |
|---|---|---|
| G0 | PASS | WP-01A 记录用户在 827fa77 对历史 D01–D15 的批准。WP-01B 保留 AST 与本地 selector 的用户裁决。C 当前授权范围覆盖已审计划和 CPU 测试，实际计划 SHA-256 为 7ed36fda92904fc447e081a4a33b311be718016705b6891d5bc51d66c678da99。[A 批准](../../../WP-01A/README.md#1-acceptance-and-authorization) · [B 细化](../../../../active/WP-01B/FINAL_ACCEPTANCE_REPORT.md) · [当前授权](../../../../active/PLATFORM_GOAL.md#当前授权记录) |
| G1 | PASS | C1 的 §5 清单与实际注册角色可追溯。独立 AST 范围检查、每个 C 持久根的注册 fixture、根和分支的正负离线验证均执行通过。嵌套组件与有限验证视图标明归属。 |
| G2 | PASS | JUnit 与 pytest stdout 一致，2813 通过，失败、错误和跳过均为 0。 |
| G3 | PASS | B Registry/Fact/projection 与 C Rule 声明、RuleType 全部执行。覆盖 10 个签名、三输入域、错误类型、单位/枚举、Record 路径、必需及可选依赖和 AST 边界。强三值真值求值留待后续。 |
| G4 | PASS | 两份各 348 文件的导出逐字节一致，仓库生成物检查一致，B 162 份保留。测试实际包括 Draft 2020-12、本地引用、Pydantic/JSON Schema 双验证。F04 另有独立 value_schema child JCS 文本和摘要、父身份变化和生成限制样本。 |
| G5 | PASS | Ruff、format、mypy、domain 导入与循环边界、CPU-only 锁测试通过。同锁环境安装 exit 0，当前独立运行复核了实际版本。GPU 环境按独立部署门禁验证。 |
| G6 | PASS | B 兼容 suite 和合并树字节比较通过，版本、闭合字段、有限策略和引用、摘要方向测试通过。没有历史数据迁移。外部信任仍需真实验证。 |
| G7 | PASS | 精确命令、逐步结果、JUnit、锁与产品摘要已落盘。独立测试停在 WP-01C。 |
| G8 | PASS | test_c_decision_coverage 与负例区分 no_hits、空 Objects、no_target、failed、partial，并保留有效子集违规。test_c_decision_sources 覆盖两种绑定的 hits/no_hits/partial，拒绝输出交换、外绑定和 grounding 污染。独立探针给出跨 Profile 同图同框的 Object/Fact 身份证据。 |
| G9 | PASS | test_c_execution 转移矩阵及负例覆盖终态、未开始的停止状态和实际 Attempt。Decision tests 检查 guard、predicate T/F/U、已选 authority、mandatory 账本与 collect_all。 |
| G10 | PASS | 已执行 B 25 ArtifactKind、16 typed policies、Prompt/能力/Facet/Question、Producer 和 authority 套件。C runtime_authority 将实际 Execution/Release/Manifest、Object、NodeRun/Attempt、QuestionSelection、ObservationBatch/ToolExecution 绑定到 B 有限校验。guard 专项拒绝同 ID 的不同 Fact 值和状态。 |
| G11 | PASS | F01–F05 实际测试覆盖层级 gate、Remote Primary 与带评测引用的 fallback、实际调用身份和 selection mode、独立 value_schema、N03 grounding/N05 Plan Step origin。实际 provider 切换、Tool 调度与调用预算仍由后续包验证。 |
| G12 | PASS | 六类 Applicability 记录、父层顺序、父 U 后的 NE 投影、guard/T/F/U 分立、上游失败与无 N06 决策均有数据证据。Coverage sources 依据实际 Execution/N01 Snapshot、选定 N02/N03 及 N06 Object 范围，拒绝错误 Snapshot digest、外 Object 决策及 blocker。 |

## 预冻结发现复核

- I-01 已有实际记录入口。runtime_authority 重建并比对 B 有限视图及选定 revision，coverage_sources 核对 Rule Guard 的 Fact 内容和选定 authority。同 ID 变化的负例通过。
- I-02 已绑定选定 N02 输出、N03 GroundingRecord、实际 NodeRun/Attempt、Object 与 N01 ContextSnapshot。N06 blocker 的上游 Object 和 Attempt 来源有专项正负例。
- I-03 已支持 Coverage 保留 N02 多次调用并匹配选定输出。两种绑定上的 HITS、NO_HITS 和 PARTIAL 实际测试通过。

完整 Bundle 编译和验签、规则求值、动态 activation、工作流和持久化、线上 Tool 或 Provider、真实数据、GPU、质量和生产部署均为 RUNTIME_NOT_RUN 或留待后续门禁。WP-02 的准备和工程裁决由主控按授权处理。
