两处非法 operand 输入会绕过约定的输入异常；所附 `all`、`any`、`not` 分支符合 Strong Kleene 三值真值表。STATIC_REVIEW_ONLY：本地只读访问受沙箱阻断，未能独立核验冻结清单或 WP-02 类型检查器对跨类型比较、producer 唯一性及引用关系的保证；TEST_NOT_RUN，未调用外部服务。

Full review comments:

- [P2] 在读取 context_fields 前校验 Mapping 类型 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/rules/_operands.py:186-189
  `evaluate(context_fields=[])` 会通过长度检查，随后在 `.items()` 处抛出 `AttributeError`，而不是与非法 `facts` 容器一致的 `PredicateInputError`。这样，调用方即使处理了约定的输入异常，也无法处理这个错误批次。应先确认 `context_fields` 是 `Mapping`，再检查长度和 selector。

- [P3] 在预算检查前识别缺少 value 的 KnownFact — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/rules/_operands.py:210-213
  对于通过 Pydantic `model_construct()` 等路径产生、缺少 `value` 的 `KnownFact` 实例，`isinstance` 检查会通过，但读取 `raw.value` 会在 `_FACT.validate_python` 之前抛出 `AttributeError`，绕过 `predicate.invalid_fact_record` 错误路径。应在预算检查前显式识别字段缺失，同时保留对已提供值先做预算检查、再递归验证的顺序。