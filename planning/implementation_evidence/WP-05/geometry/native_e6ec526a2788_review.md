两个分割子步骤都按原图宽高写掩模，行列索引一致；但它们不可能从同一 RGB 原图产生盲道重叠见证，执行器也未把实际收发的字节与所引用的子步骤证据逐项绑定。STATIC_REVIEW_ONLY：结论限于所给 unified diff；本地沙箱未能读取工作树以独立核验冻结基线，TEST_NOT_RUN，RUNTIME_NOT_RUN。

Full review comments:

- [P1] 让盲道遮挡见证可由同一原图的两个受监督子步骤产生 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/_cpu.py:163-166
  车轮掩模只选中 `WHEEL_COLOR`，盲道掩模只选中不同的 `TACTILE_COLOR`。对同一张 RGB 原图，两者在任何像素上都不可能同时为 255，因此 `overlap` 恒为 0，`blocking_tactile_paving` 分支不可达。若车轮色覆盖了停车区内的一段盲道，而别处仍有盲道色，代码甚至会返回 `inside_parking_area`。应使用能从同图表达遮挡的合成编码，并以两个子进程实际输出的 PNG 验证；手工构造相交掩模不能证明这条路径成立。[AGENTS.md:14](AGENTS.md#L14)

- [P1] 在色彩分割前核验原图的合成来源 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/_cpu.py:200-202
  `load_image` 验证格式、大小、模式和帧数，但分割入口没有验证图像来自获准的合成来源。同尺寸的实拍 RGB 图也会被处理，其中恰好匹配调色板的像素会被当作车轮或盲道。应在派发前将实际图像字节的摘要绑定到可信合成来源；颜色命中本身不是来源证明。

- [P1] 要求两个掩模输入分别具有上游子步骤见证 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:688-689
  `ancestor_evidence` 默认为空，此处在读取 ToolPlan 绑定前就返回成功；只提供一个见证时，循环结束后也不检查另一个输入路径。因此几何调用即使缺少车轮或盲道的上游见证，仍能进入物理请求准备阶段。应从预绑定计划推导这一步必需的掩模输入路径，逐项核对，缺少任何一个就停止派发。[AGENTS.md:61](AGENTS.md#L61)

- [P1] 从权威记录核对当前选中的祖先尝试 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:712-714
  `previous` 和 `selected` 都取自调用方传入的同一个 `StepCompletion`。后续比较可证明其中的 Fact、Evidence 和选择记录彼此一致，却不能证明这就是当前计划实际选中的受监督尝试。例如，同一 Execution、Object 和 step 下另一尝试的自洽记录，不会因其并非当前选择而在这里被识别。应按当前计划实例从权威记录解析选中的尝试，再对照传入的证据。[AGENTS.md:131](AGENTS.md#L131)

- [P1] 将实际 stdin 中的掩模和坐标绑定到已核验输入 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1729-1733
  祖先校验读取的是逻辑 `request`，子进程读取的却是 `prepare` 返回的另一份字节；两者之间只检查类型和总长度。即便两个见证都正确，把同尺寸的车轮和盲道 PNG 在物理请求中对调，子进程仍会正常计算，而审计仍引用原来的输入槽。对整包求 SHA-256 只能标识发出的包，不能证明槽位正确。派发前应逐槽核对实际图像或掩模摘要，并核对该操作使用的尺寸和 bbox。[AGENTS.md:127](AGENTS.md#L127)

- [P1] 将持久化的掩模和 Fact 绑定到本次子进程 stdout — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1921-1923
  `materialize` 同时返回替换后的结果和证据，此后解码的是替换结果，没有将证据摘要与本次 stdout 中的 PNG 比对，也没有将几何 Fact 与 stdout 中的度量和关系比对。因此，若适配器误取上一次调用的有效掩模，结果与证据可以彼此一致，却不属于本次子步骤。应由执行器从本次 stdout 提取见证，或在接受物化结果前验证两者的对应关系。[AGENTS.md:61](AGENTS.md#L61)

- [P1] 按完整内容引用解析停车区定义和源资产 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/region.py:62-66
  `verify_parking_area` 把传入的 Region、Geometry 与引用按 ID 和版本配对，源字节也只与传入的 `source.sha256` 比较。若传入同 ID、同版本但内容不同且彼此匹配的 Geometry 和源描述符，函数可以返回新矩形，却在 `ParkingArea` 上标记旧的 `geometry_ref.digest`。应按完整引用从已准入 Catalogue 解析定义和描述符，再校验源字节；描述符的内容摘要与原始资产字节摘要不能相互替代。[AGENTS.md:101](AGENTS.md#L101)

- [P2] 不要对有损 JPEG 使用精确调色板判定 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/inspection/geometry/_cpu.py:56-57
  即使来源确为合成图，JPEG 编码也可能改变车轮边缘的 RGB 值。`segment` 使用逐字节相等判断；若内部仍有匹配像素、跨越停车线的边缘像素却不再匹配，结果不是空掩模，而是基于残缺轮廓得出的确定关系。对这套精确色值协议应只接受无损保存的调色板图像，除非另有经过验证的 JPEG 解码判定契约。[AGENTS.md:79](AGENTS.md#L79)

- [P2] 按声明的输出证据槽校验复用关系 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1955-1958
  `proof.input_path` 标识消费步骤的输入字段，`slot.name` 标识结果的证据槽。按输入路径的最后一段比较，意味着只要输出槽与输入字段命名不同，即使复用的是同一个已核验 EvidenceRef，也会被拒绝。应按明确的输入到输出证据槽映射核验，而不是把字段同名当作关联契约。[AGENTS.md:61](AGENTS.md#L61)

- [P2] 将物理请求准备失败记录为未派发的工具尝试 — /mnt/lc/LC/ares_xtws/4_agent/RSI_ares/inspect_pipeline/JIMU_Inspection_Runtime_WP01C_Plan/src/jimu_runtime/tools/executor.py:1735-1736
  `prepare` 只捕获 `ValueError`。若准备掩模字节时源资产不可读并抛出 `OSError`，异常会离开此执行入口，而不是像无效输入那样返回带审计信息的 `stopped` 结果。应区分无效输入与资产读取失败，并在两种情况下都如实记录子进程尚未派发。[AGENTS.md:61](AGENTS.md#L61)