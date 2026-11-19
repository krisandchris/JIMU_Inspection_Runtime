# 需求制品到 Runtime 的适配契约草案

交接单位是上游导出的、可证明已经批准的版本化制品。接收方从受控交接包读取字节，与上游数据库无连接。上游业务术语及审批轨迹保存在外部来源命名空间内。Runtime 自行审查输入、验证签名、准入、注册 Release 并记录业务批准与激活。

| 交接步骤 | 输入与检查 | 结果或失败依据 |
|---|---|---|
| 识别来源版本 | 来源系统、上游制品 ID、版本、原始字节 SHA-256、映射版本、来源审批凭据。凭据由宿主认可的信任源核验。 | 同系统同 ID/版本出现不同内容时拒绝，以独立新版本重新交接。匿名或尚无审批依据的制品保留诊断。 |
| 映射定义 | 对每一可解释的语义选择封闭 ArtifactKind，明确 Runtime artifact_id/version、内容和目标路径。上游业务 ID 仅用于来源追溯。一个来源若拆出多个制品，保留源行到各转换结果的清单。 | 构造 SourceArtifact 和 SourceAssetBytes。每个 SourceIdentity 对应唯一版本和编译输入摘要。遇到没有获准对应关系的枚举/单位/几何坐标、未声明的 Fact 或 Rule，返回带来源位置的错误。 |
| 显式绑定依赖 | 使用精确 pin 或 compiler.ReferenceSlot 指向被批准的目标。外部发布或构建身份只能证明来源，不能授予 Execution/Release 身份。 | 形成带 SourceProvenance、兼容性和能力约束的 BundleCompileInput。保留原始导出摘要、映射配置摘要，以及转换后的每项内容摘要。 |
| 编译、准入与回执 | 编译器检查完整资源闭包、类型、策略及能力。Runtime 信任方验证 Bundle 逻辑根、签名及准入权限。 | CompileReport 中保留失败位置。成功时可回传 BundleRef 与 AdmissionReceipt 的标识和内容摘要。ApprovalReceipt 和 Release 激活另有权限与范围。 |
| 重复交接及修订 | 以来源系统、批准导出身份和映射版本关联本次提交，保存输入清单摘要；相同输入复用技术回执。 | 输入变化须产生可追溯的新映射/制品版本。历史 Execution 继续读取其固定 Release。 |

## 可复核的合成映射

本地独立预检包 f4c27c5baa09fb958dd068b301b1d5880e364f6919ed48b67cbae1a84d79504a 的场景导出器使用来源系统 `synthetic.nonmotor.parking.source`。逐项重算 76 个 SourceArtifact 和 1 个 SourceAssetBytes 的 SHA-256，全部与 77 行 SourceProvenance 对应。以下两个定义共享业务名称，kind、路径、原始来源和摘要各自独立。

| Runtime identity | 归档位置 | 转换后编译输入 SHA-256 | 显式引用槽 |
|---|---|---|---:|
| entity / nonmotor_vehicle / 1.0.0 | ontology/entities.json | 56e38e58667d2291d999d58a3d67fa0ebb3fc3d74109f53df7f59ba4e7fe8c23 | 2 |
| profile / nonmotor_vehicle / 1.0.0 | capabilities/profiles.json | d5f8cbf871b9f0ba7a48215770e2ec1ad5855b1020e354554d4ebd37aa87b090 | 14 |

样例加载器根据实际原始文件、路径和声明的引用槽派生来源 ID。几何资源按原始字节交接。转换后的 Definition、编译时最终绑定的 Definition 和完整 Bundle 各有自己的摘要。可从 [场景加载器](../../../examples/nonmotor_vehicle_illegal_parking/compilable/scene.py) 和 [编译输入](../../../src/jimu_runtime/bundle/compiler.py) 复核。

真实上游导出格式、签发身份、字段字典和双方兼容套件由正式对接时确认。此草案覆盖本地合成制品交接与历史追溯；上线前需要用双方批准的导出样本重做映射验收。
