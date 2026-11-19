源引用、能力协议和信任授权都存在内容改变而对应 pin 不变的情况；即使仅作为本地 fixture，这也影响证据的可核查性。STATIC_REVIEW_ONLY：结论限定于所贴 diff，未独立核验 Git 基线，未推断未附文件的行为；TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P2] 将引用槽的目标纳入源工件摘要 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:203-203
  只改一个 `$pin` 的目标、保留其位置和其他内容时，`_extract_slots()` 仍会在同一位置写入 `null`。这里仅散列 `item.content`，没有覆盖单独传给编译器的 `reference_slots`，因此两份引用不同的源会得到相同的 `SourceArtifactRef`。最终 Bundle digest 与源证据是两回事；应保留原始源文件摘要，或另行绑定包含槽位置、目标和 `schema_ref` 的规范化记录。见 [AGENTS.md:101](./AGENTS.md#L101)。

- [P2] 用实际能力快照的内容计算 pin — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:292-294
  保持 `index.json` 不变、只修改它引用的一个 schema，`contract()` 生成的 schema digest 就会改变，而这里的 `CapabilitySnapshot.pin` 完全不变。这样，同一 pin 可以指向两套不同的模型或工具协议。应从本次实际构造的快照内容（不含 pin 本身）计算摘要，而不是再次打开索引文件取哈希。见 [AGENTS.md:101](./AGENTS.md#L101)。

- [P2] 将信任策略和密钥授权一并绑定到配置 pin — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/local_trust.py:139-144
  保留同一份 `public_der` 而移除 `grant` 时，`keys` 会从一个授权变为空，但 `TrustConfiguration.pin` 不变；同样，策略 pin 只散列 `PROFILE`，不能区分来源授权或签名门槛的变化。因此，即使只审计本地 fixture，记录的 pin 也不能说明当时采用了哪套授权。应分别绑定实际策略内容，以及包含该策略、有效期和 grants 的配置内容。见 [AGENTS.md:101](./AGENTS.md#L101)。

- [P2] 在返回签名证明前核对 detached ref 的摘要 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/local_trust.py:93-95
  若签名字节保持不变、只有 `evidence.ref.sha256` 填错，`_message()` 和 OpenSSL 验签结果都不变，`verify()` 却会把错误的 ref 写入 `SignatureProof`。签发时不把签名自身的摘要放进待签消息是合理的；验签时仍应独立核对 `sha256_digest(evidence.payload) == evidence.ref.sha256`。这是该 verifier 自身的证据问题，不代表生产准入一定接受它。见 [AGENTS.md:101](./AGENTS.md#L101)。

- [P2] 将索引引用的源文件和资产限定在场景目录内 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:235-237
  如果索引误填绝对路径、越界的 `..` 或指向目录外的符号链接，`read_bytes()` 会读取场景外的文件，并把它当作本场景的源或资产；复制 fixture 到另一台机器后便可能失败或读到不同字节。`_source_artifact()` 的 `file` 读取也有同样问题。两处应共用路径解析检查，在读取前确认实际目标位于各自允许的根目录内。这是 self-contained 属性的问题，不依赖索引来自不可信用户。见 [AGENTS.md:147](./AGENTS.md#L147)。

- [P2] 在转换源 JSON 前拒绝重复成员名 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:170-170
  若源定义中意外出现两个同名字段，`json.loads()` 默认保留最后一个。对于含引用槽的 Rule、ToolPlan 或生成的输出 schema，后续规范化会抹去重复成员，编译器收到的已不是原始歧义文档；其他解析器则可能拒绝它或选择不同的值。应在首次解析源和索引时就检测重复键，而不是等规范化之后再校验。见 [AGENTS.md:147](./AGENTS.md#L147)。

- [P2] 消除能力协议对同名 schema 的歧义 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:251-253
  `schema_sources` 把三种 artifact kind 放进同一个只按 `artifact_id` 建键的字典。只要输入和输出 schema 复用一个 ID，后出现的条目就会覆盖前者，`contract()` 也会为两端取出同一条引用，使结果取决于索引顺序。应按完整身份解析；如果 fixture 索引只打算支持裸 ID，则至少在建表时明确拒绝重名。见 [AGENTS.md:101](./AGENTS.md#L101)。

- [P2] 区分按 Task 重开与读取当前激活配置 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/lifecycle.py:225-228
  这里要求当前 activation 仍是接单时的 activation，因此即使解析器回归为读取当前激活配置，示例也会得到相同结果并打印成功。若要以此证明 *historical pin reopen*，应在接单后让当前激活与 Task 的 pin 不同，再核对重开所得的 Release ID 和 Bundle digest 仍属于原 Task；否则应把证据限定为“当前激活未变化时可重开”。见 [AGENTS.md:87](./AGENTS.md#L87)。

- [P3] 固定临时签名 host 声明的测试信任域 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/local_trust.py:73-74
  `environment` 和 `profile_id` 是可由构造参数覆盖、也可事后修改的字段；`snapshot_at()` 直接用它们声明策略，而 `sign_bundle()` 始终按固定的测试 profile 签名。调用方因此可以让同一个临时 verifier 声明与签名不一致的 profile，或声明非 `isolated_test` 环境。既然该 host 只服务于这个 fixture，应将这两个声明固定，而不依赖下游拒绝不一致配置。

- [P3] 严格校验引用槽的 schema_ref 布尔类型 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/examples/nonmotor_vehicle_illegal_parking/compilable/scene.py:75-75
  Python 中 `1 == True`，所以 JSON 的 `"schema_ref": 1` 会通过这里的成员检查，却在后面的 `is True` 判断中被写成 `False`。这会把应报错的源标记静默转换成普通引用。应只接受真正的 JSON 布尔值，并用该已校验值构造 `ReferenceSlot`。