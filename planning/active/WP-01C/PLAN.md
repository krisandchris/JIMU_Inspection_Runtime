# WP-01C 实施计划（待批准）

基线为 [PR #2](https://github.com/krisandchris/JIMU_Inspection_Runtime/pull/2) 实际合并提交 `2552affeb69fbc2594fe70724f895b0db0967d47`。其树与实现提交 `0e2725712e820c08c8d7ed8d83773c3c4acb0ef9` 相同。WP-01B 用户已验收。B 的 1907 项测试、162 份 Schema 是历史基线。C 本阶段为计划审查，TEST_NOT_RUN / RUNTIME_NOT_RUN。

C1–C5 的开发子智能体统一使用 `gpt-6.2-sol`，思考档位为 `medium`。实施者和独立 tester 各用各自的会话，按[项目调度规则](../../../AGENTS.md#development-subagent-model)启动。

## 实施范围和交付顺序

WP-01C 提交规则定义、静态类型检查及总计划要求的剩余纯数据契约，按 [总计划 §5、§7、§9.2、§15](../../WP-01_IMPLEMENTATION_PLAN.md) 完成 WP-01 验收。以下工作可按批交付，用户对本计划的一次实施授权覆盖包内整改和复测。

| 批次 / 前置 | 写入责任及拟交付文件 | 验收条件 |
|---|---|---|
| C1 / 合并基线 | Sol 对总计划 §5 每个符号与 R2 补充表逐项建立 `contract_inventory`，映射实际 A/B 源符号、Schema 根或嵌套归属、fixture、缺口及责任批次 | 明确同义名称和有限描述符与持久记录的差别。缺少规范字段须登记 Finding |
| C2 / C1 | Sol 负责 `domain/rules/` 的 RuleDefinition、RuleSetDefinition、封闭 OperatorDescriptor 签名表、TypeCheckResult；直接引用现有 `conditions.py`、FactSelector、RulePolicy、ConflictPolicy | 内容引用精确，required/optional 依赖一致，保留 mandatory 全量及 collect_all 契约；共享 AST wire 的 B fixture 可原样读取 |
| C3 / C2 | Sol 负责 `domain/validation/rule_types.py` 的纯检查接口。输入为条件数据、显式 ConditionInputDomain、有限类型/Producer目录及使用上下文；输出推导类型与含路径的诊断 | 检查三个既有输入域 rule_applicability、rule_condition、tool_condition。Producer activation 复用 tool_condition，按使用位置约束。同 Plan 局部引用仅可解析祖先的映射输出。未知类型、错误域、错误单位/枚举身份、Record 路径、无效参数个数、依赖不一致均报错 |
| C4 / C1，可与规则批并行 | Sol 按模型族在 `domain/runtime/` 及节点契约模块补齐 §5 余项：Bundle/Release 数据、Intake/Context、Execution/Attempt、Perception、ToolExecution、Evidence、ModelInvocation、Applicability/Coverage/Decision、Replay、N01–N06 I/O。沿用已有模型与配置策略 | 逐根映射版本与 pin；纯数据关联和状态转移 fixtures 覆盖 G8–G12。N02 选择调用证据、N03/N05 工具来源、N04 有效集合、祖先 UNKNOWN 与 not_evaluated、空目标/部分覆盖、Required Evidence Guard 与 predicate truth 分别验证 |
| C5 / C3+C4 | 实施 Sol 维护 `scripts/generate_schemas.py` 与 `schemas/generated/`；各批独占相应测试和 fixtures。冻结交付后由另一位 Sol tester 执行最终离线验证 | 所有持久根 strict，Draft 2020-12 可离线解析；重复生成结果一致；每个已确认根有有效与无效 fixture；全量回归与 G0–G12 逐项引用证据 |

## 类型检查和边界用例

封闭签名覆盖 eq/ne、lt/le/gt/ge、in、all/any、not。输入类型、单位和枚举身份须满足 §7 的签名；根为布尔谓词。测试 enum 成员、跨 schema 同名枚举、同单位数值、异单位、bool 冒充整数、混合列表、空逻辑集合、Record 可选字段、非法 path、深度/大小边界、缺失 Producer、不同使用域下的同一 selector。用已声明的 predicate/trace 结果验证三值、required guard 和层级适用性的数据约束。Rule 求值、工作流调度、Bundle 编译/验签、真实模型与数据库分别留在后续路线图包。

## 验证与停止点

各批运行对应 unit/contract/negative 及整改回归，整包在冻结源码上用 `uv run --locked` 执行 pytest、Ruff lint、维护代码格式检查、mypy src/jimu_runtime 和 Schema `--check`，另核对两次导出的字节相等及 Domain 导入边界。记录基线、环境和锁摘要、精确命令、退出码、JUnit、生成清单与变更前后摘要。保留 WP-01B 额外脚本 mypy 的历史诊断。独立 Sol Challenge 及主控源码/原始结果审查均完成、各必需门禁通过且无未关闭阻断 Finding 后，提交工程验收报告。

## 技术自审、兼容性与回退

风险集中在把 selector 存在误作类型正确、把有限 descriptor 当成完整运行记录、以及相同表达式越过输入域使用。通过 C1 逐符号核对、消费位置绑定、独立负例与已测 B Schema 回归约束。新增文件可按批回退，已发布 B 历史证据及不可变 wire 作为回归基准。若实现需要新的操作符、业务默认值或改变既有契约，单列决策，只阻塞对应依赖。实施授权和后续 Git 发布分别记录。

## 定向 Challenge 整改与测试写权

合并基线上的独立 Sol 复核确认三种输入域及模块划分，要求补充测试责任与命令。实施 Sol 独占各自分配的源码、fixture、测试及生成文件。各批交还写权后，由独立 tester 仅在临时目录和新的 C 验收证据目录写入输出，冻结产品快照。tester 报告失败并保留原始日志，实施者定向整改并复测；冻结新快照后 tester 重跑下列全量门禁。主控读取源码、Diff、JUnit、日志和摘要后裁决。每批定向复测仅执行受影响文件，不能替代最终全量结果。

拟在 C 实施获批后于仓库根执行；`run_dir` 为新建且独占的证据目录。每条命令独立记录前后产品摘要、stdout/stderr、退出码、环境和 UTC 时间。Schema 临时目录在运行结束后由 tester 收回。

```bash
set -eu
mkdir -p planning/implementation_evidence/WP-01C
run_dir=$(mktemp -d planning/implementation_evidence/WP-01C/final_XXXXXXXX)
schema_tmp=$(mktemp -d)
export UV_CACHE_DIR="$schema_tmp/uv"
export RUFF_CACHE_DIR="$schema_tmp/ruff"
export MYPY_CACHE_DIR="$schema_tmp/mypy"
export PYTHONDONTWRITEBYTECODE=1
uv run --locked --no-sync python -m pytest tests/unit tests/contract tests/negative -q -o "cache_dir=$schema_tmp/pytest-cache" --basetemp="$schema_tmp/pytest-tmp" --junitxml="$run_dir/pytest.xml"
uv run --locked --no-sync ruff check .
uv run --locked --no-sync ruff format --check src tests scripts
uv run --locked --no-sync mypy src/jimu_runtime
uv run --locked --no-sync python scripts/generate_schemas.py --output "$schema_tmp/first"
uv run --locked --no-sync python scripts/generate_schemas.py --output "$schema_tmp/second"
diff -qr "$schema_tmp/first" "$schema_tmp/second"
uv run --locked --no-sync python scripts/generate_schemas.py --check
```

由实施者在交还写权、冻结快照之前准备好 CPU 环境。若新实施目录还没有锁定的 CPU 环境，按已有 `uv.lock` 执行 `uv sync --locked` 并记录版本。全部命令在本计划阶段均未执行。
