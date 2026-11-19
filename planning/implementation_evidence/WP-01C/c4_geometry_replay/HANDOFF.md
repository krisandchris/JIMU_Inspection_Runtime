# WP-01C C4 几何资源和回放交接

本批局部验收 **PASS**。证据标签为 **TEST_PASSED / RUNTIME_NOT_RUN**。验证基线为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47`，包含共享 C4 Execution 接口及已授权的本地改动。

## 交付与合同

- `domain/definitions/geometry.py` 提供 `GeometryResourceDefinition`。根记录明确 schema_version、资源 ID 和版本。坐标系与单位使用和 B Fact Schema 相同的 `VocabularyMemberRef`，来源使用带版本与 digest 的 `StaticAssetContentRef`。Region 仍使用现有几何资源引用。
- 几何采用单个无洞二维 polygon 外环，3 至 256 个唯一顶点，环隐式闭合。坐标由严格整数或有限浮点数表示，范围为 ±(2^53−1)，与 B 的 JCS 整数支持域一致。用精确算术拒绝全共线顶点，以免引入依赖单位或坐标尺度的容差。业务坐标系、单位及通过空间系统解析来源的方式由实际配置确定。
- `domain/runtime/replay.py` 提供明确 kind 的 `ReplayRequest` 与持久 `ReplayRun` 分支。Artifact 模式 pin 源 Task/Execution/配置、输入记录和摘要、评分协议 ID/version/digest，并保存具备独立身份及 digest 的评分 Evidence。Inference 模式 pin 原 Task/Execution、新 Release/Bundle/runtime/workflow/model/tool 配置与目标 Execution，要求保留原 Task 输入摘要，并由不同 Execution 标识承载。传入具体 ExecutionRecord 时可检查源或目标 ID、Task 及完整 pins。
- 定向测试由 `tests/{unit,contract,negative}/test_c_geometry_replay*.py` 实现。C5 可取 `tests/fixtures/wp01c/geometry_replay/` 中 geometry、artifact/inference replay request 和 artifact/inference replay run 的正负样本。额外的不同输入摘要样本用于 Pydantic 跨字段检查。

## 验证

[verification.json](verification.json) 记录实际命令、Python/依赖版本、UTC 时段、退出码、stdout 和 stderr。[JUnit](pytest.xml) 记录 420 项测试，含 37 项本批用例及 383 项 B Ontology、C Execution 回归用例，零失败、零跳过。定向 Ruff lint、五个文件格式检查、两个源文件 mypy 全部退出 0。[SHA256SUMS](SHA256SUMS) 封存源码、测试、fixtures 和结果。

开发期间定向命令 `PYTHONDONTWRITEBYTECODE=1 uv run --locked --no-sync python -m pytest -q tests/{unit,contract,negative}/test_c_geometry_replay*.py` 首次退出 1（30 通过、4 失败）。四项使用 Python 对象入口读取 JSON 时间字符串，触发符合现有 UTCDateTime 规则的严格校验。修改用例从 JSON 入口读取 wire fixture 后，同命令通过。开发期间还修复了 Pydantic 字段级 discriminator 前置校验触发的 Schema 构造错误，以及一个 Ruff B010 诊断。

## 限制与后续

本批验收涵盖数据形状和纯校验。几何外环的拓扑、自交、空间精度、Region 几何归属及 Vocabulary/资产实际内容解析留给相关消费者或后续工作包。Bundle 编译需要完成引用与摘要验证，WP-06 需要验证已存产物与源 Execution 的关联、真实评分证据及零模型/工具调用。推理回放的执行创建、权限、持久化及真实调用留给后续实现。未变更依赖、部署、迁移或历史证据。下一步为 C5 Schema 登记、整包回归与独立验收。
