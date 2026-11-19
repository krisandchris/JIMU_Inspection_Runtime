# WP-01C I-01 运行记录归因桥接证据

**I-01 归因切片通过** · `TEST_PASSED` · `RUNTIME_NOT_RUN`

## 范围

按 C1-02 对接实际运行记录和 B 有限归因验证。公开入口验证一个 Execution 的显式历史切片，并向 Coverage 提供由真实记录生成的描述符和该切片内未被继承的选定修订。N01 选定 Context Output 和 N06 Object 上游引用的 Coverage 连接由 Coverage 所属实现维护。

## 交付

- [runtime_authority.py](../../../../src/jimu_runtime/domain/validation/runtime_authority.py) 绑定 Execution、Release、Bundle Manifest、Profile eligibility 和精确版本、摘要。Object、NodeRun、Attempt、QuestionSelection、ObservationBatch、ToolStep、SystemEvent 由实际记录投影进已有 B 入口。已选输出绑定对应的 Attempt 与结果，候选 Fact 内容绑定实际 Fact，原始字节由 B 校验继续使用。
- ToolStep 的投影需要实际版本和明示输出见证。选定观察结果关联原始 ModelInvocation、固定版本和 Object Questions。缺字段、错误范围或跨记录矛盾均使投影失败。
- [executions.py](../../../../src/jimu_runtime/domain/runtime/executions.py) 为 SystemOutputEvent 增加可选实际输出见证。旧事件继续可读。消费归因时，实际事件须明确见证作用域、到达资格和输出或可选缺席。
- [确定性 fixtures](../../../../tests/fixtures/wp01c/runtime_authority/cases.py) 构造 Question、Tool、System 三类实际记录和独立 B claims。[定向用例](../../../../tests/unit/test_c_runtime_authority.py) 验证真实投影和当前修订，[契约用例](../../../../tests/contract/test_c_runtime_authority_schema.py) 验证严格 Schema 和旧事件，[负例](../../../../tests/negative/test_c_runtime_authority_invalid.py) 覆盖同 ID Fact 内容替换、身份和 pin 冲突、缺失的见证、B 候选字节矛盾。

## 实际检查

锁定 CPU 环境为 Linux x86_64、Python 3.12.3、uv 0.9.15、Pydantic 2.12.5、pytest 8.4.2、jsonschema 4.25.1、Ruff 0.14.14、mypy 1.18.2。完整命令、锁摘要、临时路径、时戳、输出和退出码见 [commands.json](commands.json)，源文件冻结摘要见 [sha256.json](sha256.json)。

| 检查 | 退出码 | 结果 |
|---|---:|---|
| 归因定向、B 邻接、执行契约和依赖边界 | 0 | 375 passed，0 skipped。[JUnit](pytest.xml) |
| Ruff lint | 0 | All checks passed |
| Ruff format | 0 | 6 files already formatted |
| mypy | 0 | 2 个目标源文件无问题 |

定向实现期间校正了跨类型的内容引用比较、测试的 Tool payload 和推断类型。最终日志覆盖完成后的源文件。先前 c4_execution 的原始证据继续对应原始版本。

## 边界

适配器校验调用者提供的实际记录之间的一致性，复用 B 的字节、Schema、producer 和修订验证。报告保留 B 的延期项。真实历史完整性、Bundle 真实性与签名、外部行为证据、执行和持久化仍需相应阶段的验证。旧事件通过新增的可选字段保持兼容，无数据迁移。整包生成及冻结快照验证由 C5 汇总。
