# WP-02 B2.1 字节和清单验证

**B2.1 局部验收 PASS · TEST_PASSED · RUNTIME_NOT_RUN**

## 范围和变更

在获准的 WP-01C 冻结基线上实现 ZIP、JCS、定位和完整性复算入口。基线工作树为 detached HEAD `2552affeb69fbc2594fe70724f895b0db0967d47` 加 WP-01C 交接文件。产品变更仅在 `bundle/{__init__,archive,integrity}.py`，并增加三套测试和固定向量。文件摘要见 [sha256sum](sha256sum.txt)。冻结 Domain 和已生成 Schema 未改。回退可移除 B2.1 新增产品文件、测试和证据，适用在消费者接入前。

- `read_bundle` 从 ZIP 原始字节独立校验实际展开量、包和成员预算、规范路径、JCS、文件清单、制品摘要和 §4 逻辑根。ZIP 读取接受单磁盘 ZIP32 的 STORED、DEFLATE 与标准数据描述符。读取不把成员解压到文件系统。
- `canonicalize_json` 规范化编译输入，`read_canonical_json` 比对准入存储字节。最大可配置 JSON 容器深度为 128。按批准的整数范围拒绝越界整数，同时拒绝 RFC 8785 输出中无法按相同整数范围复读的固定形式浮点数。浮点非有限和非零数下溢亦拒绝。
- `compute_root_digest` 从原始 manifest 解码值移除指定两个字段，保留其余原始值。合成测试特意使用带毫秒零值的 UTC 时间戳验证原始文本参与摘要。完整 ZIP 的字节摘要与逻辑根是两个值。
- `resources/assets/data/` 下文件为不透明字节，即使文件名以 `.json` 结尾。B2.2 继续负责清单制品的身份和版本、声明覆盖与制品闭包。签名信任与持久化分别由对应批次联验。

固定向量的根摘要为 `67acd2672c04f8e36c12f94ebf633e05d415180e4f0f8b3a115f6aac4e2c7a2a`，由独立固定 ASCII 内容和 SHA-256 长度帧预先算得。检查另一种 ZIP 压缩方式、顺序、注释和签名列表得到同一逻辑根，换载荷或被摘要覆盖的时间文本会失败。反例另外覆盖重复键、非法数字、无效 UTF-8、格式不规范、安全路径、RFC 6901 解析、ZIP 文件种类和搅动后的本地头，以及不实的展开量元数据。合法 DEFLATE 边界由 65,537 字节和 131,072 字节样例覆盖。

## 执行记录

环境为 Linux x86_64 6.8.0-149-generic，glibc 2.39，CPython 3.12.3，uv 0.9.15，zlib 1.3，Pydantic 2.12.5，rfc8785 0.1.4，pytest 8.4.2，Ruff 0.14.14，mypy 1.18.2。全部运行于本地 CPU 锁定环境，未跳过定向测试。

```text
uv run --locked --no-sync python -m pytest tests/unit/test_wp02_integrity_json.py tests/contract/test_wp02_integrity_vectors.py tests/negative/test_wp02_integrity_rejection.py -q --junitxml=planning/implementation_evidence/WP-02/integrity/pytest.xml
exit 0
........................................................................ [ 96%]
...                                                                      [100%]
- generated xml file: /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/planning/implementation_evidence/WP-02/integrity/pytest.xml -
75 passed in 0.21s
```

```text
uv run --locked --no-sync ruff check src/jimu_runtime/bundle/__init__.py src/jimu_runtime/bundle/integrity.py src/jimu_runtime/bundle/archive.py tests/unit/test_wp02_integrity_json.py tests/contract/test_wp02_integrity_vectors.py tests/negative/test_wp02_integrity_rejection.py
exit 0
All checks passed!
```

```text
uv run --locked --no-sync ruff format --check src/jimu_runtime/bundle/__init__.py src/jimu_runtime/bundle/integrity.py src/jimu_runtime/bundle/archive.py tests/unit/test_wp02_integrity_json.py tests/contract/test_wp02_integrity_vectors.py tests/negative/test_wp02_integrity_rejection.py
exit 0
6 files already formatted
```

```text
uv run --locked --no-sync mypy src/jimu_runtime/bundle/__init__.py src/jimu_runtime/bundle/integrity.py src/jimu_runtime/bundle/archive.py
exit 0
Success: no issues found in 3 source files
```

[JUnit 结果](pytest.xml)

运行环境、迁移与外部信任未涉及本批。本地合成证据用于代码验收，端到端候选由 WP-02 独立验收。下一工作为已派工的 B2.2 接入和后续批准批次。
