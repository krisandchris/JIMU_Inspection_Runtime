# WP-02 独立验收矩阵

状态 `STATIC_REVIEW_ONLY`。以下是规格、实现与用例的定位索引。冻结前主入口探针调试作为单独证据保存。正式 CPU 检查与运行时判定以命名冻结清单和新的一轮原始结果为准。

## 编译器与准入

| Bundle 规格 §5 | 已定位实现 | 验收证据 |
|---|---|---|
| 1. 批准来源、封闭 ArtifactKind、引用字段类型 | `compiler._source_documents`／`check_bundle_source`／`artifacts.ARTIFACT_LAYOUTS` | `test_wp02_artifact_kinds` 覆盖 25 类。来源声明/原字节和编译器 slot 的绑定另作挑战，见下文。 |
| 2. 递归精确版本和摘要 | `compiler._resolve_members`，`catalogue.ArtifactCatalogue.resolve`，`closure._validate_typed_dependencies` | `test_source_bindings_fail_closed`，`test_content_dependency_uses_exact_type_id_version_and_digest` |
| 3. 每种制品结构和 schema | `catalogue.build_catalogue`，`artifacts.validate_artifact_root` | `test_artifact_kind_has_its_own_path_and_schema`，`test_raw_schema_uses_draft_validation`，`test_archive_inventory_root_and_detached_signature_contract` |
| 4. 声明闭包、选择器、ContentRef DAG、Profile 反向成员 | `closure.validate_closure`／`_require_acyclic`，`SchemaIndex` | `test_vocabulary_member_and_ordered_reverse_profile_relationships`，`test_unresolved_identity_selector_rejects_without_searching_other_versions`，`test_schema_id_ambiguity_and_cross_resource_cycle_reject` |
| 5. Profile/实体/Facet/Question/Tool/Rule 绑定、提示词、选问完备性 | `semantics.validate_bundle_semantics`，`semantic_inputs.build_definition_inputs` 和 WP-01 定义校验器 | `test_synthetic_parking_compile_emits_typed_closed_bundle`，`test_prompt_partials_cannot_be_an_unpinned_external_lookup`，`test_potential_mandatory_rule_cannot_read_an_optional_question` |
| 6. ToolPlan DAG 与输入/输出类型 | `validate_definition_relations`，`semantic_inputs.build_tool_mapping_uses`，`schema_views`，`producer_rules._validate_tool_uses` | `test_tool_dag_cycle_is_rejected`，`test_derived_view_passes_wp01_tool_definition_checks`， `test_exact_tool_input_rejects_a_sibling_at_semantic_boundary` |
| 7. Producer 派生和来源域 | `producer_rules.derive_producer_catalogue` 和 `validate_producer_rules` | `test_catalogue_sort_and_question_id_from_independent_jcs`，`test_admission_recomputes_a_structurally_valid_derived_declaration_id`，`test_system_authority_requires_exact_declaration_manifest_membership_and_host_agreement` |
| 8. Rule 的必须 Fact 有有效来源、Schema 兼容 | `producer_rules._validate_rule_uses`、`_resolve_input_producer` | `test_mandatory_rule_without_a_tool_fact_producer_is_rejected`。独立探针以受信签名且哈希一致的缺 Producer 变异包绕过编译入口。 |
| 9. Rule AST operator / operand types | `producer_rules._check_type`，WP-01C `rule_types` | `test_all_three_condition_domains_check_registered_operator_types` |
| 10. 模型部署、远程 primary、本机例外和回退评估 | `semantics._deployment_checks`，`capabilities.validate_model_deployments` | `test_model_registration_matches_remote_primary_or_explicit_review`，`test_fallback_requires_active_independent_review_and_exact_host_contract`，`test_model_policy_cannot_self_approve_local_switch` |
| 11. Scene / Region applicability | `semantic_inputs._region_uses`，`producer_rules._applicability`，`semantics._resources` | 独立场景验证 Region、精确 scene 词表、Region 几何及静态原件。运行期条件值和可信采集上下文由后续工作包提供。 |
| 12. Runtime 版本和可用能力 | `semantics._workflow_checks`／`_protocol_checks`，`capabilities.assess_capabilities` | `test_required_capability_unavailable`，`test_schema_runtime_range_and_snapshot_window_are_required`，`test_runtime_protocol_and_workflow_are_scoped_to_current_snapshot` |
| 13. 出包、JCS、文件/制品/根摘要 | `compiler._seal_package`，`archive`，`integrity` | `test_fixed_manifest_checksums_length_frames_and_root`，`test_different_zip_bytes_and_signature_list_keep_logical_root` |
| 14. 错误、提示和确定根的编译报告 | `compiler.CompileReport`／`compile_bundle` | `test_fixed_metadata_and_artifact_order_control_determinism`，`test_wp02_compiler_invalid`，独立探针记录各具名检查和 warnings。 |

§6 的准入自独立收到的原始 ZIP 开始，经 `read_bundle` 重做路径、资源预算、库存、字节 JCS、manifest 和完整性，再经独立 host trust snapshot 校验签名、授权、撤销与可选来源证明。其后重建制品、闭包、Schema 视图、Producer、Tool/Rule/部署/运行能力校验。技术准入与业务批准分别写入存储和 Release ledger。使用本地明确配置的认证输入、外部信任测试适配器和 SQLite 存储，生产上传认证、真实信任配置、API 审计和实际部署验证依后续范围。

| §6 准入项 | 测试定位 |
|---|---|
| ZIP 安全及预算 | `test_wp02_integrity_rejection` 路径/重复/链接/大小/膨胀反例 |
| 文件 SHA 和根 | `test_byte_tampering_fails_read_from_final_zip`；独立探针更改实际几何原件 |
| 签名、根和撤销 | `test_wp02_trust_rejections`；独立探针以空 trust 和无签同根包拒绝 |
| Manifest Schema | `test_wp02_compiler_packaging`、`test_c_release_schemas`、最终生成文件逐字节对照 |
| 制品 Schema 和精确引用 | `test_wp02_artifact_rejections`、`test_wp02_schema_views_invalid` |
| 传递依赖闭包 | `test_wp02_artifact_catalogue` 与 `test_wp02_artifact_rejections` |
| Rule AST | `test_wp02_producer_rules_invalid`、`test_c_rule_types_invalid` |
| Fact 生产/消费兼容 | `test_wp02_producer_rules_invalid`；独立探针合法签名但缺 Producer |
| Tool 能力注册 | `test_wp02_trust_capability_rejections`；独立正向 fixture 使用本机注入的 Tool/协议精确版本 |
| 模型能力路线 | `test_wp02_trust_capability_rejections`；独立正向 fixture 使用声明的 remote primary。无模型调用。 |
| Runtime 版本及能力 | `test_schema_runtime_range_and_snapshot_window_are_required` |
| 策略和安全约束 | `test_wp02_trust_rejections`、`test_wp02_releases_rejection`；独立来源证明、作用域鉴权和不可变字节反例 |
| 不可变回执、识别失败结构化响应 | `test_wp02_admission_receipt`、`test_wp02_admission_flow`；实际 SQLite 入库、重开及精确字节读取 |

## 规格 §12 的 11 条

| 条款 | 验收定位 |
|---|---|
| 1. 同逻辑内容摘要固定 | 两个不同的临时编译环境、逆序 source input；独立探针比较 ZIP 和 root |
| 2. 任一 payload 变更改变摘要 | `test_byte_tampering_fails_read_from_final_zip`、`test_recomputed_file_checksums_cannot_keep_the_original_root`，以及独立资源版本变更 |
| 3. 篡改载荷拒绝 | 实际签名的包中只改静态几何来源原件，主准入入口给完整性诊断 |
| 4. 不信任签名拒绝 | `test_detached_signature_rejection`、`test_same_bundle_revalidated_after_key_revocation`；独立空信任、缺来源证明、无签 |
| 5. 重复和穿越路径拒绝 | `test_invalid_zip_member_names_are_rejected` |
| 6. 未解析引用拒绝 | `test_source_bindings_fail_closed`、`test_content_dependency_uses_exact_type_id_version_and_digest` |
| 7. 缺 Fact Producer 的 Rule 拒绝 | 产品编译负例与独立在真实验签后重建的负例 |
| 8. ToolPlan 环拒绝 | `test_tool_dag_cycle_is_rejected` |
| 9. 必需能力不支持拒绝 | `test_required_capability_unavailable`、`test_required_capability_versions_features_and_evaluation` |
| 10. 回滚保留历史 Release | 独立 SQLite 生命周期探针重开、读取旧指针历史与两个原始 ZIP，`test_wp02_releases_contract` |
| 11. 切换后在途 Task 仍固定原版本 | 独立探针在 A、B 分别接收 Task，回滚及重开后比对两份历史 Task；重复原请求仍返回原 pin |

## 预冻结发现及状态

- 来源声明一度可独立于 `SourceArtifact.content` 更改。检查 `_source_documents` 的实际代码和业务 fixture 的 `SourceArtifactRef.digest` 建立方式，已通知编译写者对每条受批准的源引用核对精确输入字节。编译 slot 会改变输出引用，其来源授权绑定需要和普通 ContentRef 校验分辨。
- 产品的已有合成停车导入夹具用 Region 的 scene、Question/Tool 两种来源以及 mandatory Rule 串联。独立探针另外加入 Region 多边形、坐标系/单位词表、静态来源描述符及二进制原件。几何检验/模型推理的业务效果待后续工作包。
- 对生产的受信签名 profile、真实批准人/作用域、密钥与撤销策略、真实数据和模型评测保留 `UNVERIFIED`。本地隔离签名 profile 具有单独命名和环境边界。
