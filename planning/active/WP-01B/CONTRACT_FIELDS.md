# WP-01B PLAN-4-A field appendix

This appendix is part of PLAN.md, not a separate implementation plan.
Only sections explicitly marked FROZEN_AFTER_CHALLENGE or FROZEN_LOCAL_IMPLEMENTATION
(and their named frozen subfields) after recorded Challenge may be delegated to code.
Sections11–13 are FROZEN_AFTER_CHALLENGE under the two explicit user approvals. Existing approved semantics prevail.
Initial execution baseline: c25bef668136acc651e7313ba608ec6fdefd39a5.
Only the approved shared condition AST data in §11 is authorized; no RuleDefinition,
TypeChecker, evaluator, alternate expression language or operator registry is authorized.

## 1. S1 — shared types and ontology/resources
Status: FROZEN_AFTER_CHALLENGE (Sol-D targeted review + Astra direct field review).

Reuse WP-01A ContractModel, ArtifactId, RecordId, Version, Digest, ContentRef,
SchemaRef, RuntimeRecordRef, WireEnum, Value/RecordValue/TypeDefinition.
Do not alter their accepted wire or numeric semantics. New persisted definition roots
have required schema_version Literal["1.0.0"], no default. Immutable tuples, no Any.

### 1.1 Shared primitive and identity contracts

- ShortText: strict str length1..128; Description: strict str length1..4096.
  No trimming/coercion; all-whitespace is rejected for names/descriptions.
- Namespace and SemanticKey: existing ArtifactId; exact values, no case folding.
- FieldPath: tuple[FieldName,...], max8, empty permitted as whole-value only in owners
  that explicitly allow it. FieldName remains A's exact nonempty length<=128 string.
- PositiveBudget strict int1..2^63-1; NonNegativeBudget strict int0..2^63-1.
  bool is not int. UTC datetime must be aware with zero UTC offset; no ambient clock.
- Lists accept JSON arrays and Python list/tuple only, copied to tuples. No sets/generators.
  Root external optional fields mean absent; explicit null rejected where not declared nullable.
  Generated JSON Schema must also reject null for these optional fields (including nested
  evaluation_ref); model-only null rejection is not enough. No null/numeric semantic conflation.
- B base helper (DefinitionContract) may extend ContractModel only to require root
  schema_version and reject explicitly supplied null in optional fields. Nested helpers do
  not acquire a fake independent schema_version. No default artifact version.
- reference_identity(ContentRef) returns (artifact_kind,artifact_id,version,digest);
  SchemaRef dialect is checked separately. Same identity refs are unique in a collection.
  Local shape validators do not imply external content digest/existence validation.
- Expected-kind constraints must apply both to Pydantic validation and generated JSON Schema.
  No second artifact-ref wire system. Narrow subclasses/annotations must preserve four fields.

Central B enums in domain/definition_enums.py (existing A enums unchanged):
FactScope=object/execution; ProducerKind=vlm_question/tool/system;
ModelCapability=profile_discovery/object_grounding/object_observation/candidate_prediction/vision_auxiliary;
CapabilityFamily=model_deployment/tool_implementation/system_fact_producer/context_derivation;
CapabilityFeature=rgb_image/typed_context/structured_output/original_xyxy/mask_output/geometry_output/ocr_text;
ContextSource=context_snapshot/region_metadata/profile_metadata.
These are the already-declared values, not new ArtifactKind/PolicyKind.
Later B enums are added by a serialized ownership handoff.

Nested shared models:
| Model | Required fields | Optional fields / constraints |
|---|---|---|
| ReviewReportRef | report_id:ArtifactId, version:Version, sha256:Digest, review_registry_id:ArtifactId | none; noURL |
| ExternalCapabilityRequirement | family:CapabilityFamily, capability_id:ArtifactId, contract_version:Version, allowed_implementation_versions:tuple[Version]1..1024, required_features:tuple[CapabilityFeature]0..7 | evaluation_ref:ReviewReportRef absent or valid; both tuples unique |
| VocabularyMemberRef | vocabulary_ref:ContentRef[vocabulary], member:ShortText | none; membership checked in explicit vocabulary catalogue |
| VersionConstraint | allowed_deployment_versions:tuple[Version]1..1024, allowed_model_versions:tuple[Version]1..1024 | no ranges/latest, each unique |
| ContextFieldSelector | source:ContextSource, schema_ref:SchemaRef[context_schema], field_path:FieldPath1..8 | declared input view, not provenance source; exact path, nodynamic access |
| AssetDimensions | width:PositiveBudget, height:PositiveBudget | no guessed aspect/coordinate conversion |

Shared models and identity helper live in definition_support.py. No imports from
definitions, validation, registry, services or later AST.

### 1.2 Persisted roots

| Model | Required fields besides schema_version | Optional fields |
|---|---|---|
| EntityDefinition | entity_id:ArtifactId, version:Version, name:ShortText, entity_type:VocabularyMemberRef, description:Description, attribute_schema_refs:tuple[ContentRef[attribute_schema]]0..1024, lifecycle:VocabularyMemberRef | none |
| RegionDefinition | region_id:ArtifactId, version:Version, name:ShortText, region_type:VocabularyMemberRef, scene_type:VocabularyMemberRef, asset_refs:tuple[ContentRef[static_asset]]0..1024, context_schema_ref:ContentRef[context_schema] | geometry_ref:ContentRef[geometry_resource] |
| VocabularyDefinition | vocabulary_id:ArtifactId, version:Version, semantic_domain:ArtifactId, members:tuple[ShortText]1..1024 | none |
| StaticAssetDescriptor | asset_id:ArtifactId, version:Version, data_path:strict str1..512, sha256:Digest, mime_type:ShortText, byte_size:NonNegativeBudget | dimensions:AssetDimensions |

All ref/member collections reject duplicates, including same kind/id/version with
different digest inside one identity list; different exact versions can coexist.
Vocabulary names remain caller-configured; no invented production member values.
Required semantic domains: entity_type->entity.type, lifecycle->entity.lifecycle,
region_type->region.type, scene_type->region.scene. These names govern catalogue use,
not business classifications/state machines. Injected catalogue checks must use exact ref,
verify ID/version/digest identity supplied with vocabulary, membership and domain.
Missing catalogues => not checked/incomplete; never local shape PASS => membership PASS.

data_path is normalized relative POSIX under resources/assets/data/; no absolute path,
empty/dot/dotdot segments, backslash, control character, URI/query/fragment. No filesystem
read or existence/symlink resolution in model validation; package verification belongs WP-02.
MIME is type/subtype with ASCII registered-token syntax; no parameters/header newlines.
Empty asset bytes are allowed and hash/size authenticity is not verified here.
Region geometry stays an exact ref; geometric body validation is not S1.
No detected Object/BBox/Decision, reverse memberships or lifecycle executor in these roots.

Files: domain/{definition_enums,definition_support}.py;
domain/definitions/{__init__,ontology,resources}.py.
Tests: tests/contract/test_b_ontology.py, tests/negative/test_b_ontology_invalid.py,
tests/unit/test_b_support.py. Test helpers may live tests/fixtures/wp01b/ only if needed.
Do not change existing A source/tests or shared tests/conftest.py.
S1 initial tests cover shape/roundtrip/strict refs/kind/dialect/UTC/immutability/limits,
root version, vocabulary uniqueness, safe paths, optional-null. Catalogue relation
validation may be delivered in D-owned integration phase, explicitly not claimed by S1.
Each exported root/helper schema passes Draft202012Validator.check_schema and strict
instances are checked in JSON/Pydantic; numeric F01 representation difference remains.

## 2. S2 — Schema descriptors and Mapping
Status: FROZEN_AFTER_CHALLENGE (Sol-B second targeted review; Astra direct reconciliation).
Pure shared types belong domain/schema_types.py, never validation imports into definitions.

## 3. Review metadata and provider contract
Status: FROZEN_AFTER_CHALLENGE for §3.1 review inputs. Remote Primary and explicit fallback semantics unchanged.

## 4. Facet metadata and Prompt support
Status: FROZEN_AFTER_CHALLENGE for §4.1 metadata/Prompt fields. No new Policy kinds, renderer or capability registry.

## 5. F1 — Fact Schema and deterministic value_schema projection
Status: FROZEN_AFTER_CHALLENGE (Sol-D targeted review; two root-bypass fixes below).
PLAN§3 G1-D02 generation algorithm applies.
No dependency on D01: only A TypeDefinition plus explicit shared types.
No parent FactSchema or Bundle digest computation.

Dependency: rfc8785>=0.1.4,<0.1.5, lock exactly0.1.4, public dumps only, no extras.
Official wheel SHA256=520d690b448ecf0703691c76e1a34a24ddcd4fc5bc41d589cb7c58ec651bcd48.
Sol-C inspected official wheel/metadata without executing it; purePython, no runtime
dependencies, Python>=3.8, Apache2, py.typed. Actual install/vector validation still required.
Sources: https://pypi.org/pypi/rfc8785/0.1.4/json and
https://github.com/trailofbits/rfc8785.py/tree/v0.1.4 .
Only generated JSON trees (recursive string/bool/int/float/null/list/dict union internally)
reach the canonicalizer; that internal serialization type is not a normative Any map.
Input contract stays A TypeDefinition; no arbitrary external JSONSchema import.
Reject integer bounds outside ±(2^53−1) with canonicalizer_integer_domain, without converting
to float to evade the library. This is the chosen library support domain, not all of RFC8785.
Precheck Unicode keys/strings and finite numeric content with paths; normalize library
CanonicalizationError/UnicodeEncodeError/OverflowError to typed validation error, not a crash.
Never change A accepted values or ValueContract to accommodate canonicalization.

FactSchemaDefinition root required fields:
schema_version=1.0.0; schema_id:ArtifactId; version:Version; semantic_key:ArtifactId;
scope:FactScope; type_definition:TypeDefinition; payload_schema_ref:ContentRef[value_schema];
value_schema_generation:ValueSchemaGenerationSpec; allowed_producer_kinds:unique nonempty
tuple[ProducerKind] max3; status_policy:FactStatusPolicy; units:UnitMetadata;
coordinate_system:CoordinateMetadata; evidence_policy_ref:ContentRef[policy];
confidence_metadata_policy:ConfidenceMetadataPolicy. No optional free metadata.
FactSchemaDefinition must normalize raw type input BEFORE A TypeDefinition inserts its
defaults, using the already validated generation spec. Raw string with omitted upper and
limit100 becomes max100; explicit4096 or already constructed A default4096 rejects at
limit100. This applies recursively to List/Record. Full root tests, not only helper tests.
Execution scope requires allowed_producer_kinds=(system,) (all execution Facts are system);
object scope allows the approved three kinds, subject to per-producer checks later.

FactStatusPolicy requires fixed literals: known_value=required; non_known_value=forbidden;
non_known_reason=required; error_diagnostics=required; not_applicable_basis=required;
not_evaluated_reason=required. No defaults. These restate five-status invariants, not a
new PolicyKind or sixth FactStatus; transport/envelope failure still creates no Fact.

UnitMetadata discriminated mode=not_applicable (no other fields) or declared(bindings).
UnitBinding has field_path:FieldPath, unit:VocabularyMemberRef. CoordinateMetadata has
same two modes; CoordinateBinding has field_path, coordinate_system:VocabularyMemberRef.
Declared bindings count1..64, no duplicate or ancestor/descendant overlap within one class;
Record-only field paths must exist; empty selects whole payload. Same path may have both
unit and coordinate annotations. Vocabulary semantic domains are unit / coordinate_system.
not_applicable is an explicit business configuration assertion, not an inferred absence.
F1 checks paths/local shape; finite vocabulary-content validation separately reports missing
catalogue. No conversion, ambient units or requirement that names are preinvented production enums.

ConfidenceMetadataPolicy discriminated mode=forbidden (no fields), optional or required.
The latter branches require allowed_fact_statuses:unique tuple[FactStatus]1..5,
interpretation=model_reported_not_calibrated_probability, numeric_format=finite_number.
Outside allowed statuses confidence is forbidden; required means present for listed statuses.
No null, implicit [0,1] scale, calibration or production threshold. Policy metadata does not
grant model output decision authority. It remains parent-only, excluded from child generation.
EvidencePolicy is reused by exact ref; shape only in F1, subtype/content checks require
the independent non-expression EvidencePolicy finite catalogue. Do not fabricate its content.

GlobalTypeLimits requires string_max_length<=4096, list_max_items<=256,
record_max_fields<=64, max_depth<=8, each strict positive integer.
ValueSchemaGenerationSpec requires projection_profile_version=1.0.0,
generator_id=jimu.value-schema, generator_version=1.0.0, schema_dialect=full2020-12URI,
canonicalization_id=JCS, global_limits. No default fields.
ValueSchemaGenerationInput has only projection:TypeDefinition and generation_spec;
projection is normalized, immutable A algebra, no second open type language. Its own
validators normalize/validate directly supplied trees with the spec; no unvalidated
"already normalized" assertion. Every digest/generation entry revalidates, including
instances made with model_copy/model_construct; order/limit tampering cannot bypass checks.
GeneratedValueSchema is internal immutable output: generation_input, canonical_input_bytes,
input_sha256, canonical_schema_bytes, child_digest, child_ref:ContentRef[value_schema].
JSON bytes are products, never unvalidated user-schema fields accepted as authoritative input.

Interfaces (pure): normalize_type(raw_or_A_type, limits)->TypeDefinition;
generate_value_schema(type_input, generation_spec)->GeneratedValueSchema;
validate_child_reference(definition)->GeneratedValueSchema (regenerate and exact compare).
Use bounded strict normalization preserving explicit-vs-omitted string/list limits. Recursive
unsupported/cyclic input fails before uncontrolled traversal. Local malformed fields raise
structured/pathed ValueError/ValidationError; no registry/Rule/Bundle service dependency.

Owner Sol-C: fact_schemas/{__init__,models,projection}.py and optional canonicalization.py;
tests/unit/test_b_projection.py, tests/contract/test_b_fact_schema.py,
tests/negative/test_b_fact_schema_invalid.py, tests/fixtures/wp01b/jcs_vectors.json;
pyproject.toml/uv.lock solely for bounded RFC8785 addition after S1 environment handoff.
No other dependencies upgraded. Evidence in execution_02/logs/f1/.
Independent vectors use RFC8785 §3 examples/AppendixB and UTF16 ordering expected bytes;
expected output cannot be generated by function under test. Cover duplicate JSON keys at
raw JSON entry if exposed (otherwise no raw JSON parser is claimed), surrogate keys/values,
safe boundary±(2^53−1),2^53/2^53+1/2^60/hugeint, negative zero/exponents/finite floats,
input hash vs child hash, parent exclusion, exact derived child identity, source-order
normalization, nested wrappers/closed Records, global-limit omission vs explicit values,
all seven value kinds, semantic validation and JSONSchema2020-12 roundtrip/rejections.
Existing import-boundary test's exact dependency allowlist must be updated by Sol-D to add
only the newly approved RFC8785 dependency; this is a changed contract, not dropping checks.

## 6. O1 / M1 and individual non-expression Policy models
Status: FROZEN_AFTER_CHALLENGE in §6.1–6.3. Definition shape, refs and local DAG are independent.
Complete PolicyArtifact and Applicability/ToolSkip data are now implemented under §11;
the original14 branch interfaces below remain compatible.
Individual existing non-expression Policy branches may be implemented once exact fields freeze;
do not export a subset as the normative full PolicyArtifact.

## 7. Fact / Attempt / Authority support
Status: FROZEN_AFTER_CHALLENGE for models in §7.1. Preserve all PLAN G1-D03 invariants.
No partial FactProducerDeclaration replacement: the full activation union is required by §12.
Local result/Fact/authority data checks must not claim full producer authority admission.

### 2.1 Frozen descriptor wire (FROZEN_AFTER_CHALLENGE)

All containers bounded (fields/outputs64; descriptors1024), strict/frozen, no null,
no recursive cycle. SchemaDescriptor union discriminator=descriptor_kind:
- raw_dto: schema_ref:SchemaRef[context_schema|attribute_schema|response_schema|
  tool_input_schema|object_schema|decision_schema], type_definition:TypeDefinition.
- typed_value: schema_ref:SchemaRef[value_schema|attribute_schema|tool_input_schema|response_schema],
  type_definition:TypeDefinition.
- tool_result: schema_ref:SchemaRef[tool_output_schema], payload_representation=typed_value,
  payload_type:TypeDefinition, evidence_fields:tuple[EvidenceFieldSpec]0..64,
  output_declarations:tuple[ToolOutputDescriptor]0..64.
- tool_input_record: schema_ref:SchemaRef[tool_input_schema], fields:tuple[ToolInputFieldSpec]1..64.
  ToolInputFieldSpec(name:FieldName,required:bool,representation=raw_dto|typed_value,
  type_definition:TypeDefinition). Root is closed raw object with unique field names;
  every direct field independently carries its representation. No recursive mixed
  descriptor or nested schema_ref. Field internals use one A algebra representation.
  Mixed target path starts with field name; empty/whole-input mapping is forbidden.
  A typed field preserves its complete Value wrapper; raw image/Object fields coexist
  without converting typed Facts into raw values. Required coverage includes all fields.

raw_dto interprets the finite A type tree as unwrapped JSON primitive/list/closed object.
typed_value interprets it exactly as A kind/value wrappers at every node. These are
explicit restricted generated-schema profiles, not a parser for arbitrary JSON Schema.
Raw metadata DTOs with unsupported nullable/union/pattern/custom constraints cannot
be admitted by omitting those constraints; return unsupported/incomplete. A caller
must inject an exact descriptor for the restricted generated view, not claim an
approximation represents an arbitrary runtime DTO. Actual schema bytes authenticity/
full resource closure is WP-02; B checks finite inputs and explicitly states this trust boundary.
TypeDefinition fields/limits/required describe all constraints supported by these views.

FieldSelector(schema_ref:SchemaRef, field_path:FieldPath) selects semantic Record fields.
ResolvedFieldType contains schema_ref, field_path, representation(raw_dto/typed_value),
type_definition, optional_ancestor_paths:tuple[FieldPath]0..9, wire_path:tuple[FieldName]0..16.
For raw: wire=semantic; typed: insert "value" before every Record segment, result is
still whole typed Value node. No automatic unwrap/encoding conversion during mapping.
Fact Registry describe_type uses this same result shape; empty path returns whole type.
Special Tool-result resolution explicitly selects payload; whole envelope is not a Value.
Because payload can be absent, its optional ancestry includes () followed by optional
Record paths; at most9 entries (root plus8 paths), ordered and unique. Ordinary required
raw/typed roots do not acquire this optional root marker.
Tool-result wire_path is payload-relative, never prepends the envelope's payload segment.
Mixed Tool input wire_path includes raw root field then typed wrappers inside that field;
overall semantic path remains<=8, wire path<=16. It reports the selected field's encoding.

EvidenceFieldSpec(name:FieldName, required:bool, multiple:bool) describes the explicit
named evidence section: value is one RuntimeRecordRef[EVIDENCE] or bounded tuple of them.
ToolOutputDescriptor(output_name:ArtifactId, value_path:FieldPath, required:bool) corresponds
exactly to Tool.fact_outputs names/paths/required; key/schema remain Tool declaration authority.
Evidence semantic paths select one named slot only (no list index); unknown slot rejects.
confidence_path resolves within typed payload to finite number. Tool value_path is payload-relative.

Tool-result fixed envelope contract (not ToolExecution lifecycle):
status=succeeded/partial/failed; diagnostics:tuple[ToolDiagnostic(code:ArtifactId,message:Description)];
evidence:declared named evidence slots; output_states:tuple[ToolOutputState]0..64.
Known state has output_name, fact_status=known, evidence_refs; reason forbidden.
Nonknown state has output_name, one existing nonknown FactStatus, reason(code/message),
evidence_refs. Names unique. Exact payload type comes from descriptor.
succeeded/partial require payload whenever any output state is known. If all named outputs
are nonknown, payload may be absent; if present it must conform to payload_type and all
nonknown value_paths must be absent. No outputs/evidence-only may omit payload.
failed forbids payload, requires diagnostics and empty output_states (no invented Facts).
Every mapped output in succeeded/partial has one state; known path exists and validates,
nonknown path absent. Evidence-only tool may declare no outputs.
Payload required fields describe transport structure, not Fact output obligations. For
nonempty named value_paths the transport path must have an optional ancestor/leaf so an
unknown/error item can coexist with known siblings; reject nonknown_payload_path_not_omittable.
Whole-payload output () can be nonknown only with payload absent; known sibling paths then
cannot coexist. Do not forbid this approved whole-value path or invent an empty payload.
Tool fact_outputs.required still requires an emitted Fact/state, even when transport path
is optional. Never equate Tool status=succeeded with FactStatus=known.
S2 defines descriptors/shape constraints only, no execution/adapter. The later result
validation checks actual payload/states. Optional absence is a separate authority
descriptor disposition, never a sixth FactStatus or silent removal of unknown/error Facts.

### 2.2 Frozen Mapping wire (FROZEN_AFTER_CHALLENGE)

InputBinding(source:InputSource, target:FieldSelector, required:bool,
on_missing:block|omit). All explicit, required denotes target obligation.
InputSource discriminator=source_kind:
- context: selector:ContextFieldSelector (exact context schema, nonempty path).
- object: selector:FieldSelector[object_schema], implicit current Object only.
- image: selector:FieldSelector[attribute_schema], implicit original Task media
  metadata/reference view only, not URL/filename/pixels. No new media ArtifactKind.
- fact: selector:FactSelector from the approved standalone selector contract.
- step_output: step_id:ArtifactId, selector:FieldSelector[tool_output_schema],
  path relative to that ancestor ToolResult.payload.

FactSelector and ProducerSelector are static identity data independent of activation AST:
scope, fact_key:ArtifactId, fact_schema_ref:ContentRef[fact_schema],
producer_selector, field_path:FieldPath. ProducerSelector discriminator=producer_kind:
vlm_question(question_ref,output_name=primary);
tool(tool_plan_ref,step_id,tool_ref,output_name);
system(requirement:ExternalCapabilityRequirement[system_fact_producer],output_name).
No runtime Fact/Attempt refs, derived catalogue backref or expressions.
These live in C-owned fact_selectors.py before B input-mapping consumers.

MappingTypeContext is nonpersistent injected input: profile_ref, tool_plan_ref,
current_step_id, context_registrations, object_schema_ref[object_schema],
image_schema_ref[attribute_schema], descriptors. ContextTypeRegistration discriminates
source context_snapshot(schema_ref only) / region_metadata(schema_ref,region_ref) /
profile_metadata(schema_ref,profile_ref), with exact source views and unique source/owner.
The full finite Tool/plan catalogue is supplied separately to validation, not imported
into low-level types. No definition binds arbitrary runtime Object IDs.

target.schema_ref must equal consuming Tool.input_schema_ref. Effective required means
every ancestor plus leaf required; whole input is required. InputBinding.required must
agree. Required target => on_missing=block; optional target may explicitly block or omit.
All required inputs covered, no duplicate/prefix-overlapping target writes.
Source optional ancestor allowed only under explicit missing policy, never value synthesis.
Unknown/error/nonknown Fact or absent authority is not ordinary absent field eligible for omit.
Upstream step must be a strict DAG ancestor and match exact Tool.output_schema_ref.
Grounding bindings before Object creation accept only declared context/image sources;
object/fact/step sources cannot be smuggled into N03 by a generic union.

Compatible mapping: same representation and same type kind; range/length inclusion,
enum subset, homogeneous list recursion, closed Record field inclusion and requiredness.
No implicit int->number or typed->raw coercion, conversion, expressions or N06 inputs.
Whole-Record containment is recursive: outer on_missing=block cannot repair optional
source members needed by a required target, or extra possible members in a closed target.
Required coverage is conditional on parent construction: an optional parent does not
create an unconditional top-level obligation, but once any child mapping constructs it,
all of that parent's required children must be covered. Optional ancestry is not a
permission to construct an invalid partial Record.
Fields reached through optional parents retain missing eligibility; a passed mapping
check does not prove runtime availability or whole Bundle conditional-producer completeness.
Unknown descriptor/unsupported schema/capability => typed incomplete/invalid, never PASS.
Pure types in schema_types.py/tool_mappings.py; validators in validation/schema_descriptors.py.

### 3.1 Frozen review inputs (FROZEN_AFTER_CHALLENGE)

ReviewedDeployment fields: provider_id, deployment_id, logical_model_id (ArtifactId),
provider_kind=remote/local, deployment_version:Version, model_version:Version.
VersionConstraint refers exact registered versions; unresolved/nonrepresentable external
versions do not get copied/guessed into these identities. No endpoint/secret/URL.

ReviewScope discriminated by scope_kind, all ref sets nonempty/unique/exact:
binding(binding_refs);
profile(binding_refs,profile_refs);
region(binding_refs,region_refs);
profile_region(binding_refs,profile_refs,region_refs).
Binding scope explicitly covers the exact listed binding versions; other branches
narrow it with matching Profile/Region identities. There is no empty-list wildcard.
Validation context must supply relevant owner identities; absent needed owner => incomplete.
This is report-coverage metadata, not a new approval authority or runtime activation scope.

ReviewMetadata(report_ref:ReviewReportRef,verdict=approved/rejected,valid_from:UTC,
valid_until:UTC,assessments:tuple[ReviewAssessment]1..1024).
ReviewAssessment discriminator=assessment_kind:
- fallback_transition: source/target:ReviewedDeployment, capability:ModelCapability,
  scope:ReviewScope, trigger_errors:unique tuple[transport|timeout|rate_limit|unavailable]1..4,
  input_schema_ref/output_schema_ref:SchemaRef, contract_compatibility=exact,
  quality_evaluated=true. source/target distinct; all configured transitions and triggers
  must be covered for each permitted (transition,trigger) combination by matching exact
  model/deployment versions and IO, not separate unrelated set coverage.
- local_primary_exception: exception_id:ArtifactId, exception_version:Version,
  primary:ReviewedDeployment[local], applicable_capabilities:unique tuple[ModelCapability]1..5,
  scope:ReviewScope, reason=explicit_local_capability/privacy_restriction/unavailable_remote_capability.
  No fake from/to/trigger/quality fields for local exception.

valid_from<valid_until, validation_at explicit, from<=at<until. Both report window and
LocalPrimaryException window apply; exact approval/report identity/reason/capability/scope
match required. rejected never confers approval. Fixtures assert approved as trusted
injected data only; B does not prove report authenticity, evaluation quality or provider routing.
No defaults/quality thresholds/trust roots/approval actors invented.
DeploymentRequirement.logical_model_id equals its consuming ModelBinding; constraints
have one authority in pinned ProviderPolicy, no duplicate ModelBinding deployment arrays.
Types in validation/review_inputs.py may depend only on support/refs/enums, not Policy/Model.

### 4.1 Frozen metadata and Prompt fields (FROZEN_AFTER_CHALLENGE)

Facet metadata: optional tuple[FacetMetadataEntry]1..64; absent distinct from explicitnull.
Entry(namespace:ArtifactId,attribute_schema_ref:ContentRef[attribute_schema],value:RecordValue).
Namespaces unique; matching descriptor must be typed_value with RecordTypeDefinition and
exact attribute schema (already included in the typed_value kind list above).
Raw descriptor is incompatible; do not unwrap. Validate RecordValue against trusted type.
Facet.tags:unique tuple[VocabularyMemberRef]0..1024, semantic_domain=facet.tag.
Neither namespace nor tags can carry arbitrary unregistered schema/business members.

PromptTemplateDefinition explicit schema_version/prompt_template_id/version/purpose/template_text/variables/
response_schema_ref/rendering_policy=literal_substitution; optional partial_refs exact prompt refs.
purpose is the four approved model purposes (excludes vision_auxiliary).
template_text length1..65536, preserved without trimming. Variables are required0..1024,
unique by name; partial_refs optional0..1024, null forbidden and exact-ref unique.
ASCII_identifier is [A-Za-z_][A-Za-z0-9_]{0,127}. Named placeholders use {ASCII_identifier},
doubled braces escape. Generated Schema rejects invalid syntax as well as Pydantic.
Bare {}, unmatched brace and expressions reject. Repeated identical placeholder is allowed.
PromptVariable(name:ASCII identifier1..128, source=context/profile_metadata/question_metadata/
object_geometry/image_reference, field_path:FieldPath, schema_ref:SchemaRef,
required:strict bool,on_missing:block|omit). required true=>block, false=>omit;
Schema also exposes this pairing and source-to-Schema-kind restrictions.
Source schema kind context=context_schema, profile/question metadata=attribute_schema,
object_geometry=object_schema, image_reference=attribute_schema. Exact views explicitly supplied;
Question context allowlist constrains any context variable. Do not reuse broader Tool InputSource.
Partial graph finite/acyclic; common variables must be identical declarations; scope is
union of root+partial variables and actual named uses. Reject unregistered or unused
declarations at complete injected-graph validation, not local template-only parsing.
Root-only validation reports partial closure deferred when dependencies not supplied.
No template rendering, compiled joint-schema request generation or model invocation.

### 6.1 O1 definition fields (FROZEN_AFTER_CHALLENGE)

All persisted roots extend DefinitionContract and require explicit id/version fields.
All ContentRefs have expected-kind constraints in model and generated Schema.
ContentRef collections are immutable, max1024 and unique by kind/id/version
(conflicting digest duplicates reject). Other collections use their declared keys:
Prompt variable name, metadata namespace, tag (exact vocabulary ref,member), context
(source,exact schema,path). Multiple members of the same vocabulary are allowed. No reverse Profile membership or implicit default configuration.

FacetDefinition: facet_id, version, name:ShortText, description:Description,
fact_schema_refs:tuple[ContentRef[fact_schema]]1..1024; optional metadata (§4.1),
optional tags:tuple[VocabularyMemberRef]0..1024. Missing tags means no supplied tags,
not a production taxonomy inference; explicit null rejects.
QuestionDefinition: question_id, version, objective:Description,
prompt_template_ref:ContentRef[prompt_template], facet_refs1..1024,
context_allowlist:tuple[ContextFieldSelector]0..1024, primary_fact_key:ArtifactId,
primary_fact_schema_ref:ContentRef[fact_schema], evidence_policy_ref,
unknown_policy_ref, validation_policy_ref (all exact policy refs). No other output slots.
Context allowlist items unique by source/exact schema/path, paths are not flattened strings.

ModelGroundingBinding(kind=model,model_binding_ref[model_binding],input_bindings);
ToolGroundingBinding(kind=tool,tool_ref[tool],input_bindings).
input_bindings is optional bounded tuple0..64 of §2.2 InputBinding; absence means no
explicit mapping supplied, not permission to guess required inputs. If present only
context/image source branches allowed, including generated Schema branch constraints.
No Object/Fact/step/N06 inputs. Missing mappings are checked against known consuming
IO requirements at finite-catalogue validation; missing descriptors => incomplete.
Grounding target.schema_ref must equal the consuming ModelBinding.input_schema_ref
or Tool.input_schema_ref respectively; context includes declared Region/Profile views.
No producer/actual invocation/runtime Object fields in a GroundingBinding.

ProfileDefinition: profile_id, version, entity_ref[entity],
discovery_binding_ref[model_binding], grounding_binding, observation_binding_ref[model_binding],
facet_refs0..1024, question_refs0..1024, question_selection_policy_ref[policy],
rule_set_refs0..1024, tool_plan_ref[tool_plan], applicability_policy_ref[policy].
Empty Profile Question/Facet catalogues and zero RuleSets are representable; an actual
ProfileHit still requires the approved nonempty effective set. No negative-decision/
coverage claim follows from empty configuration. Empty ToolPlan is separate valid data.
Profile source ref is supplied by finite-catalogue caller; no own digest/backrefs.
Local shape does not claim ApplicabilityPolicy or complete QuestionSelectionPolicy
admission while D01 remains OPEN. Those checks explicitly deferred, not skipped-as-pass.

Finite ontology relations: exact Entity/Facet/Question/Schema/Prompt/Model/Tool/Plan
refs must resolve in supplied catalogue, with content identity supplied by caller.
Question primary Schema belongs to exact intersection of all its Facets; Profile
contains every Question Facet. Primary Schema scope=object and permits vlm_question;
Question primary key is a declared key, not silently substituted by Schema semantic_key.
Schema metadata/units retain exact identities, never shape-only equivalence.
Question Prompt purpose=object_observation; Profile discovery/observation/grounding
capabilities match exact models/tool tags and declared IO; partial closure is reported.
Prompt context variables must be in Question.context_allowlist by exact schema/path,
without expanding authority to all siblings under a parent path. The same rule applies
to inherited partial variables. ContextSource/owner ambiguity when multiple declared
views match a Prompt context variable is incomplete, never resolved by picking the first.

Files A: definitions/capabilities.py, definitions/prompts.py;
tests/unit/test_b_prompts.py, tests/contract/test_b_capabilities.py,
tests/negative/test_b_capabilities_invalid.py, fixtures/wp01b/o1/.
Pure local models/template syntax first; full injected relation checks belong
D-owned validation/definitions.py, not imported back into definition models.

## 8. R1 and finite diagnostics (FROZEN_AFTER_CHALLENGE)

Sol-D proposed exact fields and Astra reviewed them against §2/5 and the approved
finite-registry boundary. No full Producer/Authority acceptance is implied.

validation/issues.py (nonpersistent, imports only low-level primitives/support/refs):
ValidationIssue(code:ArtifactId,path:tuple[FieldName|nonnegative StrictInt]max32,
message:Description,related_refs:tuple[ContentRef]0..16 unique).
CheckResult(check_id:ArtifactId,status=passed|invalid|incomplete|deferred,
issues:tuple[ValidationIssue]0..64). passed iff no issues; other status requires issues.
ValidationReport(scope=registry_core|definition_relations|producer_admission|authority_admission,
checks:tuple[CheckResult]1..128 with unique check_id). Derived aggregate property only:
invalid > incomplete > deferred > passed. No caller-writable valid flag or silent truncation.
Report overflow is an explicit bounded-report failure, never incomplete data marked passed.
These are check outcomes, not new Fact/Decision/Node status values. No new ArtifactKind.

FactSchemaRegistryBuilder.register(exact_ref,definition)->Self:
revalidate inputs (including constructed/copied instances), kind=fact_schema and
ID/version equal definition; validate_child_reference regenerates actual child.
Caller parent digest is an asserted exact identity, not authenticated parent bytes.
Same kind/id/version with different digest rejects; same exact ref/different validated
definition rejects; identical verified ref/definition is an explicit idempotent no-op.
Failure does not mutate builder. Registry capacity max1024; overflow rejects atomically.
freeze()->FactSchemaRegistry makes a separate immutable snapshot and seals builder;
repeat freeze is harmless, any subsequent register rejects. New versions use new builder.
No shared mutable dict/global registry or exposure of writable internal storage.

resolve(exact_ref)->FactSchemaDefinition uses all four ref fields, no latest/range/selector
replacement; distinguish unknown identity and known identity with wrong digest.
validate_payload(exact_ref,raw_value)->A Value uses the registered TypeDefinition and
A validate_typed_value; caller payload cannot supply a replacement type/member set.
describe_type(exact_ref,field_path=())->ResolvedFieldType reuses §2 model and typed paths;
its schema_ref is the child payload_schema_ref converted to SchemaRef with the pinned
2020-12 dialect, not the parent FactSchema identity. Existing A SchemaRef is unchanged. Parent identity remains
in the query. Optional ancestors/leaf ordered root-to-leaf, no list/dynamic traversal.
Failures raise RegistryValidationError with finite ValidationReport; no None/false fallback.
Suggested check IDs registry.reference_shape/parent_identity_binding/definition_contract/
child_derivation/identity_conflict/payload_conformance/field_resolution; only report
checks actually performed. Producer/Authority checks in full-package report stay deferred.

F01 dual-stage validation is demonstrated using dev jsonschema then domain strict
validation; preserve JSON integer 1.0 vs Python strict integer and number 1 vs exactfloat
differences. Do not add jsonschema to Domain runtime dependencies or loosen A.
Tests: atomic registration, collision types, forged child identity, no parent-auth claim,
freeze/no mutation, all7 payload kinds, optional Record/path/closedness, exact child
SchemaRef, bypass instances, all4 diagnostic states and deferred full admission.

Sol-D files: validation/issues.py, fact_schemas/registry.py,
tests/unit/test_b_registry.py, tests/contract/test_b_registry_contract.py,
tests/negative/test_b_registry_invalid.py, fixtures/wp01b/r1/;
tests/contract/test_dependency_boundaries.py (existing file: minimal approved dependency
allowlist change and real module-import edge strengthening only; preserve all A tests).
External imports may add only rfc8785, confined to F1 canonicalization/projection.
Import graph resolves real local from-package submodules and package reexports;
synthetic tests cover hidden cycles, aliases, unresolved imports and valid acyclic graphs.
Do not mistake an imported symbol for a nonexistent submodule or invent self edges.
No Rule AST: Python ast for test-only import analysis is unrelated.

### 7.1 A1 exact support fields (FROZEN_AFTER_CHALLENGE — models first)

This is a technical completion of PLAN§3 G1-D03, not a replacement ProducerCatalogue.
Persistent roots retain flat execution_id:RecordId and scope=object/execution.
Object scope requires object_id:RecordId and profile_ref[profile]; execution scope forbids
both, including null. Fact/Selection roots do not duplicate object_ref; descriptors use
RuntimeRecordRef[object] whose record_id must match. Root schema_version=1.0.0 required.
Scope/producer/status restrictions must appear in generated Schema as well as Pydantic;
composed discriminated branches or generated conditional constraints are allowed, Any is not.

FactRecord common fields: fact_id:RecordId, execution_id/scope fields, fact_key:ArtifactId,
fact_schema_ref[fact_schema], producer_catalogue_ref[fact_producer_catalog],
producer_declaration_id:ArtifactId, producer_kind, producer_id:ArtifactId,
producer_version:Version, attempt_id:RecordId, source, evidence_refs[evidence]0..1024 unique,
created_at:UTCDateTime, trace_id:RecordId; optional confidence:A finite strict number.
No separate schema_digest or duplicate actual-version fallback; exact refs hold digest.
Confidence has no invented min/max/0..1 or calibration; parent FactSchema's frozen
confidence_metadata_policy governs presence/status, not a new ReportedConfidence schema.

status discriminator has five branches: known requires Value, forbids reason; unknown
requires FactReason, forbids value; error requires FactErrorReason, forbids value;
not_applicable requires FactNotApplicableReason, forbids value; not_evaluated requires
FactReason, forbids value. All forbidden fields include null. Whole Record has one status.
FactReason(reason_code:ArtifactId,message:Description,diagnostic_refs[evidence]0..1024 unique).
FactErrorReason adds diagnostics:tuple[FactDiagnostic]1..64;
FactDiagnostic(code:ArtifactId,path:IssuePath,message:Description) describes
transport/item/tool errors without pretending all errors are Pydantic validation errors.
IssuePath is the existing §8 validation.issues alias (FieldName/nonnegative StrictInt, max32).
FactNotApplicableReason adds basis_refs:tuple[ContentRef[policy]|RuntimeRecordRef[
evidence|context_snapshot]]1..64 unique. Basis types do not execute applicability.

Fact source is a closed union with required source_kind discriminator, exactly
vlm_question/tool/system, agreeing with root producer_kind in model and Schema
(no independent authority). Source branch fields:
vlm_question: question_ref,observation_batch_ref,model_invocation_ref;
tool: tool_execution_ref,tool_plan_ref,step_id,tool_ref,output_name;
system: requirement[system_fact_producer],system_output_ref[execution_event],output_name.
Question output=primary; Question/Tool only object; system may object/execution.
Producer ID/version equals definition ref or system capability identity/contract version.
Source refs/Attempt/result bind actual provenance via descriptors, not expected provider data.

Nonpersistent immutable descriptors (all refs expected runtime kind):
ObjectDescriptor(object_ref,execution_id,profile_ref).
NodeRunDescriptor(node_run_ref,execution_id,node_id=N01..N06,status:NodeRunStatus).
AttemptDescriptor(attempt_ref,execution_id,owner,attempt_number:PositiveBudget,status:AttemptStatus).
owner union discriminator owner_kind=node(node_run_ref) /
system(requirement[system_fact_producer]); no invented N07.
QuestionSelectionDescriptor(question_selection_ref,execution_id,profile_ref,
eligibility_qualification=validated|unresolved|rejected,effective_question_refs1..1024 unique).
Qualified selection anchor is caller-provided evidence; no Gate/ModelInvocation execution claimed.

Result common: result_ref,execution_id/scope fields,attempt_ref,
arrival_qualification=on_time|late|unresolved. No default/clock-derived assertion.
ObservationResultDescriptor: object scope, result_ref[observation_batch],
question_selection_ref,effective_question_refs1..1024,model_invocation_ref,
envelope_validation/association_validation=trusted|untrusted|unresolved,
question_results:tuple[QuestionPrimaryCandidate(question_ref,fact_ref)]0..1024, unique
by question identity AND by fact_ref independently. effective refs reject repeated
kind/id/version even if digest differs. Cross-directory existence checks are separate.
ToolStepResultDescriptor: object scope,result_ref[tool_execution],
invocation_origin=tool_plan_step,tool_plan_ref,step_id,tool_ref,
actual_implementation_version:Version,envelope_validation/association_validation,
mapped_outputs:tuple[MappedOutputResult]0..64 unique output_name.
SystemOutputResultDescriptor: result_ref[execution_event],requirement,output_name,
actual_implementation_version:Version,output:MappedOutputResult (same name).
Actual implementation versions must be explicitly supplied and allowed; never copied
from expected requirement to invent invocation evidence.
MappedOutputResult discriminator disposition:
emitted(output_name,fact_status:FactStatus,fact_ref), or
optional_absence(output_name,reason:FactReason) forbidding Fact/status/value.
This is completeness input, not another ToolOutputState transport wire/sixth FactStatus.
Tool payload/state validation, when supplied, reuses §2 ToolResult descriptor and paths;
descriptor-only association does not imply raw Tool response validation.

AuthoritativeResultSelection common: selection_id:RecordId,execution_id/scope fields,
selected_attempt_id:RecordId,selected_result_ref,selected_fact_refs[fact]0..1024 unique,
reason=validated_success|explicit_retry_resolution|manual_review,
authority_revision:PositiveBudget,optional expected_previous_selection_ref
[RuntimeRecordRef kind=authoritative_result_selection],
actor_ref:ActorRef,created_at:UTCDateTime.
ActorRef(actor_kind=service|human,actor_id:RecordId,identity_registry_id:RecordId).
selection_group discriminator: observation_batch(object scope,batch result);
tool_step(object scope,tool_plan_ref,step_id,ToolExecution result);
system_output(requirement,output_name,execution_event result,matching scope).
Group identities, whole-result selected set, succeeded/on_time qualification for ALL
selection reasons, exact external effective Questions, optional/emitted distinctions,
revision/fork rules and finite-history limits remain exactly PLAN§3 G1-D03.

FactCandidateSubmission(fact:FactRecord,raw_bytes:bytes) is a nonpersistent validation
input. Raw JSON parser preserves and checks bytes before dictionaries (nested duplicate
keys, bad UTF8/scalars, nonfinite numbers rejected); strict parsed content must equal fact.
Identical bytes same candidate is duplicate; otherwise conflict, never overwrite.
No complete ProducerDeclaration can be synthesized from these inputs. Any finite output
identity projection used by local checks must identify its source catalogue/declaration,
scope/profile/key/schema/selector/cardinality; it omits activation and is explicitly NOT
a substitute for approved ProducerCatalogue, not a persisted artifact or admission input.
The models-first task does not implement a public identity projection; complete
Producer validation remains deferred rather than accepting an invented activation-free
producer input. Local shape/association/history checks can pass independently, while full producer/authority
local admission report retains a specific unperformed full-admission check; §12–13 supply
the separate complete finite interfaces. No authoritative consumer API may return
an unconditional validated-authority claim using only those partial identity inputs.

Implementation split: models first (runtime/facts.py, runtime/authority.py,
validation/authority_inputs.py), then explicitly local checks. Full Producer/Authority
admission endpoints stay X2/X3 blocked. No state machine, database, selection executor,
Gate evaluator, fake confirmed absence or latest-write preference is added.

### 6.2 M1 Model / Tool / ToolPlan (FROZEN_AFTER_CHALLENGE)

ModelBinding root: model_binding_id/version/capability:ModelCapability/logical_model_id:ArtifactId,
provider_policy_ref,resource_requirements_ref,fallback_policy_ref (exact policy),
input_schema_ref/output_schema_ref:SchemaRef, prompt_template_ref[prompt_template].
Prompt required for all four language/VLM capabilities; optional absent only for
vision_auxiliary. If a vision_auxiliary prompt exists, never require a nonexistent
vision_auxiliary Prompt purpose; compatibility needs declared consuming purpose/context,
missing context reports incomplete. Exact deployment/version constraints live solely in ProviderPolicy.
No embedded endpoint/secret/provider config/duplicated deployment array or actual result.

ToolDefinition root: tool_id/version; tool_type=grounding/segmentation/geometry/ocr/evidence_transform;
provider_kind=builtin/local_model/remote_service; input_schema_ref[tool_input_schema],
output_schema_ref[tool_output_schema]; resource_requirements_ref/execution_policy_ref/
evidence_policy_ref; implementation_requirement with family=tool_implementation;
capability_tags unique0..5 of object_grounding/segmentation/geometry/ocr/evidence_transform;
fact_outputs unique by output_name AND fact_key,0..64; optional model_binding_ref
(when declared must resolve vision_auxiliary, no implicit call). Worker placement is
not Model Provider routing. Capability tags are declarations, not model-quality proof.

ToolFactOutputDeclaration(output_name:ArtifactId,fact_key:ArtifactId,
fact_schema_ref[fact_schema],scope=object,required:strict bool,value_path:FieldPath,
optional evidence_paths:tuple[FieldPath1..1]0..64 unique,optional confidence_path:FieldPath).
Only object-scoped Tool Facts in this V1; evidence-only Tools have zero outputs.
confidence_path may be empty iff whole payload is finite Number; evidence paths address
one named evidence slot (not an index). Empty value_path remains legal whole payload.
ToolFactOutputMapping(output_name,target_fact_key) both ArtifactId; target must equal
declared key at finite-catalogue validation, never alias/rename. Shapes alone do not
prove payload compatibility, producer admission or actual ToolResult validity.

ToolPlanDefinition root tool_plan_id/version/steps:tuple[ToolStep]0..1024.
ToolStep(step_id:ArtifactId,tool_ref,depends_on:tuple[ArtifactId]0..1024 unique,
input_bindings:tuple[InputBinding]0..64,fact_output_mappings0..64 unique output/key,
requirement=required/optional,failure_policy_ref,skip_policy_ref).
depends_on is the single explicit edge authority, no duplicate root edge array.
Local validator rejects duplicate step IDs, dangling/self/repeated dependencies and
any cycle; empty/single/fork/join DAG valid. Step-output InputBinding must refer to
a strict ancestor, not merely an earlier array item. Preserve configured ordering;
no execution schedule/service. Finite-catalogue checks verify actual upstream Tool
schema, selected/mapped output closure, Policy subtype and budget tightening.
A step may select a subset of Tool outputs; an unused Tool output is not forced into
Fact production merely because its declaration says required. Required/cardinality
constraints apply to mapped outputs. No added obligation to select every Tool output.
validate_tool_plan requires caller-supplied keyword-only plan_ref[tool_plan];
ID/version bind the definition, full four-tuple binds every mapping context. All contexts
in one call share one exact Profile; reuse across Profiles is validated in separate calls.
This is asserted identity consistency, not computed parent-content authentication.
FailurePolicy.requirement must agree with Step requirement. Full ToolSkip content
admission requires the separate full Policy/Producer/use-site checks; this local ToolPlan
model/static DAG does not itself prove those higher checks.

B owns definitions/models.py, definitions/tools.py and validation/tool_plans.py;
C ownership of tool_outputs.py is explicitly transferred to B for this task only
(C's active A1 does not write it). A/B/C consume the single resulting public types.
B may serially add these existing closed enum domains to definition_enums.py;
no other agent edits that file. Tests unit/test_b_tool_plans.py,
contract/test_b_models_tools.py,negative/test_b_models_tools_invalid.py,fixtures/wp01b/m1/.

### 6.3 P1 individual non-expression Policies (FROZEN_AFTER_CHALLENGE)

Use all exact field names/enumerations/conditional-presence requirements in Domain§14,
§3.9.1 and §11.1. No fields there may remain Any/object or undeclared Schema placeholder.
ApplicabilityPolicy and ToolSkipPolicy full data are added by X1/§11; no literal-true-only
substitute is permitted. The14 individual contracts below remain their original complete
shapes, and QuestionSelection context-rule refs now resolve actual Applicability artifacts.
No subset may be exported as complete PolicyArtifact. Specialized named artifacts
(e.g. EvidencePolicyArtifact) contain schema_version/policy_id/version/policy with its
exact concrete policy type. Complete PolicyArtifact discriminated union is defined in §11.3.
PolicyKind remains exactly16 approved kinds, not16 invented generic placeholders.

All ordinary collections max1024, named output/field sets64; enum sets bounded by their
domain size and unique; required refs exact. PositiveBudget everywhere specified,
clock_tolerance_ms NonNegativeBudget. No silent budgets/retention/calibration defaults.
QuestionValidationPolicy optional max_string_chars/max_items positive <=4096/256;
missing means use global maximum, not null. EvidencePolicy required subset allowed;
original_media_required true requires original_image in allowed set; retention_class
VocabularyMemberRef uses semantic_domain=evidence.retention_class for finite catalogue.
Spatial-derived evidence types are crop/bbox_visualization/mask/geometry: allowing any
requires transform_required_for_derived=true. In other configurations the field may be
absent or explicit true, never false; it is a guard on actual derived assets, not a claim
that tool_output/model_request/model_response are necessarily spatial images. Actual
asset source/transform provenance is still required where relevant; no raw JSON inference.
ToolExecutionPolicy retry_backoff_ms present iff max_attempts>1, each positive budget,
length=max_attempts-1, max1024; max_attempts consequently<=1025 when supplied schedule
is required. retry_backoff_ms[i] configures the wait after failed Attempt i+1 before
the next attempt. No repeat-last/truncation; >1025 attempts rejects the explicit schedule
support domain (not a performance/production limit). One attempt forbids retry schedule;
execution policy never auto-retries.

QuestionSelectionPolicy: mandatory/optional exact refs disjoint and jointly equal
Profile.question_refs in finite validation; all_declared optional_source=all;
mandatory_plus_optional may all/context_rules/model_advisory. context_rules has one
ContextSelectionRule(question_ref,applicability_policy_ref) per optional Question, no
rule for mandatory/unregistered Question. No entries in other source branches.
model_advisory requires proposal_schema_ref[response_schema],selection_review_ref,
SelectionCallPolicy from Domain§11.1 (shared_n02 with no extra fields, or
explicit_additional with proposal_binding_ref/additional_resource_requirements_ref/
positive max_additional_invocations_per_execution). Others forbid these three fields.
Fallback/order fixed approved choices; no actual selection/invocation behavior.

DeploymentRequirement: provider_id/deployment_id/logical_model_id:ArtifactId,
provider_kind=remote/local,actual_version_constraints:VersionConstraint,
required_features:unique CapabilityFeature set. Only deployment_id is unique within
ProviderPolicy; provider_id/logical_model_id may repeat. Every logical_model_id must
match the consuming ModelBinding in finite-catalogue validation.
LocalPrimaryException exact Domain§3.9.1 fields with UTC windows and reviewed refs;
ProviderPolicy remote/local-primary branch requirements and FallbackPolicy transitions
cross-checked against injected §3.1 trusted reports at explicit validation_at, not now().
Disabled fallback has empty triggers/transitions and no evaluation refs; explicit
evaluated requires nonempty trigger/transition/report sets. No implicit timeout switch.

ContextFieldRequirement exact field_path/context SchemaRef,required/time_sensitive bool,
allowed_sources nonempty unique among5 approved values. Conditional derivation requirement
family=context_derivation plus input_selectors present iff declared_derivation allowed;
clock_tolerance_ms present iff time_sensitive. Define source-owner/schema/path matching
and acyclic derivation on finite injected field declarations, never execute derivations.
Output identity is (exact context SchemaRef,path); duplicate or prefix-overlap output
declarations reject. context_snapshot input matching a declared derived output creates
an edge; a declared trusted non-derived field is a root. Region/Profile source inputs
are external roots validated against supplied owner/schema/path views, not forced into
this Policy output list. Missing source view/descriptor is incomplete; equal type shape
is not proof of trusted source. Cyclic derived edges reject deterministically.
ExecutionPolicy fixed six ordered NodeIDs plus exact resource/coverage/provenance policy
refs and unique SchemaRefs; metadata only, no Workflow execution.

B owns policies.py, validation/policies.py and named policy tests/fixtures/logs/p1.
Provider/report coverage is a pure finite compatibility check, not proof of genuine
report approval or actual quality. Missing expected policy content => incomplete/deferred;
supporting raw ref shape never becomes complete policy/Bundle acceptance.


Review metadata trust boundary clarification: §3.1 ReviewMetadata is an injected
trusted-resolver result, not the serialized report body covered by ReviewReportRef.sha256.
Exact Binding/Profile/Region content refs in ReviewScope are resolver associations made
after those definitions are pinned. Never require original report hash input to contain
the digest of a Binding/Profile that itself reaches this report through Policy refs.
The report independently covers deployment versions/capability/business scope; trusted
resolution supplies matching exact owner associations. B checks supplied associations,
not report authenticity/resolver execution or a new IdentitySelector usage. Thus no
Binding→Policy→report hash→Binding digest cycle is reintroduced.

### 7.2 A2 candidate bytes / finite history (FROZEN_AFTER_CHALLENGE)

This adds pure checks already required by PLAN§3; no persistence, executor, authoritative
consumer view, partial ProducerCatalogue or unconditional admission result.
CandidateParseLimits explicit strict positive integers: max_bytes<=16MiB,
max_json_depth<=32,max_object_members<=1024,max_array_items<=1024,max_number_chars<=4096.
These bound this parsing call, not A Value semantics or production capacity/defaults.
CandidateSnapshotLimits explicit max_entries<=1024,max_batch_entries<=1024,
max_total_raw_bytes<=64MiB (combined prior+submitted raw bytes for this call, counting
repeated deliveries too; budget rejection is not a candidate conflict). No behavior defaults; budgets must be supplied per call.

validate_candidate_submission(submission,*,limits) returns CandidateParseOutcome:
discriminator outcome=locally_valid(submission,report) / invalid(report).
Strict UTF8, no BOM/replacement/Unicode normalization; lexical depth guard before JSON
parser recursion. Preserve object pairs and numeric lexemes until nested duplicate keys,
scalar Unicode, cardinality, token budgets and nonfinite values are checked. Duplicate
escaped-equivalent names reject before dict construction. Do not coerce int/bool/number.
Then TypeAdapter(FactRecord).validate_json(original bytes) plus revalidation of typed
Fact must agree, preserving strict A1 JSON UTC and F01 number behavior. Catch malformed
JSON/Unicode/numeric/budget exceptions as typed finite issues; no swallowed errors.
Every stored original byte remains unchanged; model equality does not prove delivery equality.
Diagnostic path uses IssuePath32; report ancestor/parser-root location explicitly when
invalid input is outside supported path domain, never claim a truncated full location.

CandidateSnapshot immutable entries:tuple[FactCandidateSubmission]0..1024;
append_candidates(prior,submissions,*,parse_limits,snapshot_limits)->CandidateAppendOutcome.
Outcome discriminator outcome: appended(snapshot,report,appended_count,duplicate_count),
duplicate(snapshot,report,duplicate_count), rejected(report,prior,conflicts,conflict_completeness).
Counts strict nonnegative<=1024; appended_count>=1. Empty batch => duplicate/count0;
mixture of new+duplicate => appended iff every submitted candidate is valid.
Rejected.prior discriminator prior_qualification: validated(snapshot) or unavailable
(forbids snapshot). Only independently revalidated prior may be returned. Invalid/
unparseable/over-limit prior yields unavailable, never rewraps it as validated history.
ConflictRecord(conflict_kind=same_candidate_different_bytes|fact_id_rebinding,
old_location:{origin=prior|batch,index:strict int0..1023},new_index:strict int0..1023,
old_submission,new_submission). Both submissions must already satisfy parse limits.
No raw echo for invalid/over-budget input. conflicts max64; conflict_completeness=complete|overflow.
If more than64 conflicts/diagnostic budget occur, rejected report explicitly records
overflow and incomplete enumeration, never pretends the retained prefix is exhaustive.
Revalidate every supplied prior/input, no already_verified assertion. All-or-nothing:
any invalid/conflict/budget failure preserves the caller's original history; output returns
unchanged validated prior only when its qualification is validated. No partial batch admission.
Internal indexes are built only after detecting identity collisions; explicit same key,
same fact_id and identical bytes is duplicate. Same key/different bytes or same fact_id
different immutable candidate rejects; candidate ID is single-valued in this finite set.
candidate key=(execution_id,scope,object_id iff object,producer_identity,fact_key,attempt_id).
Question identity uses exact question ref+primary; Tool exact plan/step/tool/output;
System family/capability_id/contract_version/output_name. System mutable requirement
details do not create a new identity to evade conflict. No latest/confidence selection.

LocalAuthorityHistoryInput contains candidates:CandidateSnapshot,
prior_selections/proposed_selections:tuple[AuthoritativeResultSelection],
objects:tuple[ObjectDescriptor],node_runs:tuple[NodeRunDescriptor],
attempts:tuple[AttemptDescriptor],question_selections:tuple[QuestionSelectionDescriptor],
observation_results:tuple[ObservationResultDescriptor],tool_results:tuple[ToolStepResultDescriptor],
system_results:tuple[SystemOutputResultDescriptor],
questions:tuple[AuthorityQuestionInput],tools:tuple[ToolRegistration],
plans:tuple[AuthorityToolPlanInput]. Every directory explicit0..1024. These typed
Question/Plan inputs have fields ref (expected question/tool_plan ContentRef), definition
(actual QuestionDefinition/ToolPlanDefinition), with ID/version
consistency, not activation-free Producer surrogates. They may be empty, yielding incomplete
when needed. R1 FactSchemaRegistry is a separate required function argument, not a fake
Pydantic arbitrary object or embedded mutable service.
validate_local_authority_history(inputs,*,fact_schemas,parse_limits,snapshot_limits)
returns ValidationReport only. All A2 reports use existing scope=authority_admission
and explicitly include full_authority_admission=deferred; local outcome is based on the
performed syntax/history checks, never on pretending full report aggregate is passed.
Empty inputs also retain this local API deferred check. Complete finite Producer/Authority
association is a separate §12–13 composition; actual runtime authority remains unproven.
This local API exposes no selected-consumer view and no overall passed status.

Before indexing each directory, compare scoped ref identity/content; conflicting body
rejects, identical repeated content may deduplicate explicitly. Runtime descriptor keys
include execution_id+kind+record_id; never resolve a same ID from another execution.
Candidate fact_id uniqueness is additionally enforced across the submitted finite snapshot.
Check all supplied history, not only latest/proposed item. Revision1/no previous then
contiguous same-group revision+exact previous; reject forks, gaps, self/cross-group links,
selection ID rebinding. Missing previous identity/definition/schema is incomplete;
known identity with wrong digest, contradictory revision or provable gap/fork is invalid.
Unselected failed Attempts remain valid historical data. Directory and selection-history array order is irrelevant. The effective_question_refs
sequence is different: Domain §11 requires Profile catalogue order and exact inherited
sequence equality, so reordered effective Questions must reject. Re-selecting earlier successful
Attempt is legal; no finish-time/attempt-number/confidence authority inference.

All reasons require selected Attempt succeeded and result on_time; unresolved arrival
or selection qualification is incomplete, rejected/late/failed result invalid. Node owner
must link same Execution and N04/N05 as appropriate; system owner matches registered
requirement, not a fake Node. NodeRun aggregate status does not rewrite historical Attempt
identity or invent lifecycle transitions. Object/Profile/source/result/Attempt links match.
N04 uses external validated QuestionSelectionDescriptor, exact effective set and exactly
one primary Fact per effective Question; actual Question definition checks key/schema.
N05 actual pinned ToolPlan mappings determine the output set, actual Tool required flag
determines emitted requirement; all required mapped outputs present, optional emitted or
absence exclusive. Unmapped Tool outputs are not forced into Facts. Match declaration
key/schema/scope and Tool implementation allowlist, but do not assert Producer activation.
Profile Question/ToolPlan membership, Release pin and selection-policy completeness
remain deferred without their full authoritative catalogues. Producer catalogue/ref/declaration
existence/activation/authorization remains D01 deferred. System emitted output-name→key/Schema/
cardinality binding AND optional-absence zero_or_one permission stay deferred until complete Producer declaration;
System actual version must match its explicit requirement. All emitted nonknown Facts
remain in selected set. Selected Facts equal entire selected Result candidates, no splicing.
R1 checks exact Schema, value when known, scope/allowed_producer_kinds and confidence policy
presence/status; absent registry/schema is incomplete/invalid as appropriate. Actual evidence,
Invocation, Actor trust and outside history completeness remain explicitly unverified/deferred.

C files: runtime/candidates.py, validation/authority.py;
tests/unit/test_b_candidate_history.py,tests/contract/test_b_authority_history.py,
tests/negative/test_b_history_invalid.py,fixtures/wp01b/a2/,logs/a2/.
May extend owned authority_inputs.py with the named finite input classes, preserving A1 wire.
No new status/ArtifactKind; reports use existing diagnostics. Candidate append is in-memory
atomic, not DB CAS/fencing or durable conflict event. Full X2/X3 still blocked by D01.

UTC technical correction: the unreleased B shared UTCDateTime wire accepts RFC3339
T/t date-time with Z/z or +00:00. Python accepts only aware zero-offset datetime. Numeric
text, space separators, naive/nonUTC strings reject; nested JSON follows the same rule.
RFC3339 §4.3 -00:00 means a known UTC instant with unknown local offset; this internal
wire does not silently discard that provenance by accepting it as explicit UTC preference.
Pydantic/Python datetime support does not include leap-second :60, and regex alone does
not prove calendar validity. No A foundation contract changed. Source: [RFC3339 §4.3/5.6](https://www.rfc-editor.org/rfc/rfc3339.html#section-4.3).

## 9. V1 finite Definition relations (FROZEN_LOCAL_IMPLEMENTATION)

Pure finite validation only; not a new Definition Registry, Compiler or admission service.
D-owned validation/definition_inputs.py contains nonpersistent exact ref+definition
registrations for Entity/Region/Facet/Question/Profile/Prompt/ModelBinding/Tool/ToolPlan/
Vocabulary/StaticAsset. Each union branch has registration_kind (existing kind string),
ref of that expected ContentRef kind, definition of the existing actual model; ref ID/version
must equal definition ID/version. Reuse/subclass B's ToolRegistration, PromptRegistration,
ModelBindingRegistration, rather than copying their consistency rules. No own digest.
DefinitionRegistration union has discriminator=registration_kind; branch labels are not
new ArtifactKinds. Identity truth is the injected caller's assertion, not authenticated bytes.

DefinitionValidationInput required fields:
definitions:tuple[DefinitionRegistration]0..1024; descriptors:tuple[SchemaDescriptor]0..1024;
fact_schema_refs:tuple[ContentRef[fact_schema]]0..1024;
policies:tuple[P1 PolicyRegistration]0..1024; use_contexts:tuple[ProfileUseContext]0..1024;
reviews:tuple[ReviewMetadata]0..1024. Optional validation_at:UTCDateTime, absent not null;
P1 Model Policy validation requires this explicit instant even with remote/disabled fallback.
If absent, mark those compatibility checks incomplete, continue independent shape/ref/Prompt
checks; never now(), fixture time, or a duplicate no-review routing implementation.
ProfileUseContext required profile_ref, context_registrations0..1024,
question_metadata_views0..1024, mapping_contexts:tuple[MappingTypeContext]0..1024,
model_prompt_uses0..1024. Optional region_ref[region], profile_metadata_schema_ref[attribute_schema],
object_geometry_schema_ref[object_schema], image_reference_schema_ref[attribute_schema].
QuestionMetadataView(question_ref, schema_ref[attribute_schema]) unique by question identity.
ModelPromptUse(binding_ref[model_binding],consuming_purpose:existing4 PromptPurpose),
unique binding identity; this is explicit consuming context, not a fifth Prompt purpose.
Use-context identity is exact Profile plus optional exact Region; duplicate uses reject.
Profile context registration owner must match current Profile. Declared Region metadata
views retain explicit owners; ambiguous matching source/schema/path is incomplete, no
first-match selection. Question metadata resolves only the current Question's view.
No fake ToolPlan step_id for N03. Separate mapping contexts refer real ToolPlan steps.

Challenge closure (2026-11-17): Question-owned Prompts have an explicit current Question.
A ModelBinding-level joint Prompt has no singular Question owner in this finite input;
question_metadata there is incomplete/unsupported owner, never the first matching Question.
ModelPromptUse is unchanged; no invented per-Question binding execution semantics.
Discovery/object_grounding Prompt is before Object creation: an object_geometry variable
is incomplete for lack of current Object owner, even with a shape view. Object observation
may use its declared current Object view. Shape compatibility never grants stage access.

Nested MappingTypeContext catalogues are restricted views of the top-level catalogue.
Preflight all views before indexes or subvalidators: same kind/id/version with differing
digest, or same exact ref with differing descriptor body, is invalid. A nested descriptor
absent from top-level is incomplete, never silently promoted into the authoritative set.
Nested Context registrations must agree with the current ProfileUseContext declarations
on source, exact owner and Schema. Every mapping context must pin this exact Profile,
its exact configured ToolPlan and an actual step (including otherwise unused contexts).
A used Region metadata source must match the current use's exact region_ref; absent
current Region is incomplete, contradictory owner invalid. This validator intentionally
checks one current Profile/Region use, without banning explicitly declared other-region
relationships from future contracts. Used object/image source Schema must agree with
its supplied outer view; missing outer view is incomplete. Unused required view fields
in MappingTypeContext are not evidence of access or runtime source authorization.

validate_definition_relations(inputs, fact_schemas:FactSchemaRegistry)->ValidationReport
with scope=definition_relations. Registry is separate typed argument, no arbitrary-types
Pydantic field. Explicit fact_schema_refs plus reachable referenced Schema definitions
are checked; this finite inventory does not prove all Registry entries/Bundle closure.

Revalidate all input models before building indexes. Reject duplicate kind/id/version
registrations and same-version conflicting digest; exact four-part lookup everywhere.
Known identity/wrong digest invalid, missing target incomplete. Same checks apply to
descriptor/Policy/report/context view identities; no dict construction that loses conflict.
Checks failing prerequisites remain incomplete/deferred, never unearned passed.

Actual independent checks:
- Entity attribute/Region Context/asset refs, classification vocabularies and domains;
  geometry body/asset bytes authenticity explicitly deferred.
- Every used vocabulary ref, member and semantic_domain: entity.type/entity.lifecycle/
  region.type/region.scene/facet.tag/unit/coordinate_system/evidence.retention_class.
- FactSchema metadata paths use F1 and vocab checks; evidence_policy_ref resolves the
  actual EvidencePolicy subtype; type equality is not business meaning approval.
- Facet schemas resolve exact R1 refs; metadata exact typed_value Record descriptor and
  A validate_typed_value; raw descriptor or undeclared/invalid fields reject.
- Question primary Schema is exact intersection of all Facets, object scope, permits
  vlm_question; Prompt purpose=object_observation. Profile includes every Question Facet.
- Profile exact Entity/Question/Facet/Model/Tool/Plan refs and known capability labels;
  discovery/observation/model grounding capability, Tool grounding object_grounding tag,
  Tool-declared nested ModelBinding=vision_auxiliary. Host capability/protocol/quality
  proof is not inferred from these labels; unresolved semantic IO compatibility deferred.
- Prompt partial graph finite/acyclic: exact refs, same-name declarations identical,
  union declared names equals union actual placeholder uses. Incomplete graph cannot
  pass full closure. Inherited variables use correct source/schema/path/owner; Question
  context variables must match exact allowlist, no parent-path authority widening.
- Grounding bindings only context/image and exact consuming input Schema; use B's
  validate_target_coverage plus resolve/containment functions for Model/Tool input.
  Tool-specific wrapper stays restricted. Absence of explicit bindings is checked as
  empty configuration against actual requirements, not silently guessed source values.
- Reuse B finite Tool/Plan/Policy functions (with explicit plan_ref/binding_ref/UTC/use
  owners); preserve all their invalid/incomplete/deferred checks. No duplicate routing,
  budget, DAG, report-quality or Context derivation algorithms.
- QuestionSelection partition may be verified; referenced context-rule expression is
  still D01 deferred. Remaining Policy subtype checks use actual14 artifacts, no fake
  Applicability/ToolSkip roots or empty definitions.

Reports group checks by category (catalogue/ontology/vocabulary/fact_schema_metadata/
facets/questions/profiles/prompt_closure/prompt_context/capabilities/grounding_io/policies/
tool_plans), within128 check IDs and64 issues each. Overflow explicitly fails closed
with a diagnostic that enumeration is incomplete; no silent truncation or all-PASS.
Always preserve deferred applicability/ToolSkip content, RuleSet body, complete Producer/
Authority admission, mandatory producer completeness, full Bundle and external authenticity.
Empty input cannot be mistaken for complete configuration acceptance. No Runtime calls.

Tests: positive finite fixture with explicit deferred boundaries; index/identity/taint;
Vocabulary wrong member/domain/digest; exact Facet intersection/foreign scope/producer;
metadata Record constraints; partial cycles/dangling/conflicting/unused declarations;
inherited allowlist/owner ambiguity; Model/Tool Grounding missing/wrong target mappings;
P1 reports not dropped; report capacity; no forged blocked definitions to obtain green.
Challenge regressions: multi-Question model Prompt cannot guess question_metadata owner;
unused nested conflicting descriptors/foreign contexts reject; missing outer view incomplete;
missing validation_at remains incomplete both with and without reviews.
D files: validation/{definition_inputs,definitions}.py;
tests/unit/test_b_definition_relations.py,tests/contract/test_b_definition_catalogue.py,
tests/negative/test_b_definition_relations_invalid.py,fixtures/wp01b/v1/,logs/v1/.

## 10. E1 implemented-contract Schema export (FROZEN_LOCAL_IMPLEMENTATION)

An engineering export of Pydantic contracts already implemented in A/B, not a Bundle
Compiler or future business Schema generator. `scripts/generate_schemas.py` uses explicit
source imports and an explicit finite public-root/named-union inventory; no file-directed
imports/eval, manually authored divergent schemas or remote resolution. Nested helpers
are covered through generated $defs; inventory lists every persisted definition/Fact/
Authority root and every current contract union, including A value/reference types,
B descriptors, input sources, Policy subsets, Authority inputs/outcomes/reports. Pure service
classes are not schemas. Audit coverage test catches newly implemented public persisted
roots omitted from the inventory. Nonpersistent helpers are labeled as such; Candidate
bytes/history helpers do not claim JSON byte transport or semantic roundtrip from Schema.

Each output is the actual model/TypeAdapter JSON Schema with top-level `$schema` fixed
https://json-schema.org/draft/2020-12/schema, deterministic sorted JSON+newline. No timestamps,
absolute worktree paths, fake parent digest or private deployment material in outputs.
Index records source symbol, role, filename, SHA256 of emitted bytes and explicit limitations;
index digest need not include itself. Initial E1 inventory was PARTIAL_D01_BLOCKED
(execution_02 historical snapshot). E2 must include all implemented condition/Policy/Producer/
integration input contracts, remove obsolete D01 missing-groups, and identify status as
COMPLETE_WP01B_CONTRACTS only when actual inventory is complete. Keep later TypeChecker/
evaluation/Bundle/real runtime authority limitations explicit. NonExpressionPolicyArtifact
must retain its actual name and cannot masquerade as full PolicyArtifact. A existing schemas
are regenerated without changing A models/semantics. Fact value child projection uses F1,
not this pretty-printed engineering export/digest.

CLI default writes only owned `schemas/generated/` files and deterministic index; `--check`
performs no writes, compares exact expected contents, rejects missing/stale/unknown generated
files and returns nonzero on mismatch. No automatic deleting unknown files, no global cwd
assumptions (derive repository root from script path). Tests may provide a temporary output
path via internal function/explicit CLI option; reject root escapes for the default output.
All schemas validated offline with Draft202012Validator.check_schema; local refs must resolve
within each document, no remote-ref retrieval. Shape-only schema validation cannot substitute
Pydantic strict numeric/UTC/taint checks or cross-definition checks; document differences.

Tests: repeated output identical; missing/stale/additional output --check nonzero without
mutation; partial index correctly names all deferred groups; real representative valid and
invalid fixtures across A/B; discriminators/extra=null constraints; inventory coverage and
no external $ref. No checked-in fabricated empty schemas for blocked modules. Targeted tests
and final once-only full A+B verification after all current writers finish. Source inventory
may wait for V1 stable public symbols; no code writes into another writer's source files.

E1 owned files: scripts/generate_schemas.py, schemas/generated/**,
tests/contract/test_b_schema_inventory.py, tests/unit/test_b_schema_export.py,
execution_02/logs/e1/. All persisted-source changes require separate ownership transfer.

E1 independent Sol Challenge closure: fix generation to validation mode/by_alias=True
(union_format=any_of where supported by the locked Pydantic API); explicit filenames must
be unique safe basenames, with source symbol/module collision checks. Independent inventory
coverage test must compare declared public roots/unions from another explicit expectation
or AST/public export scan, not enumerate the export mapping and call that proof of coverage.
Set sys.dont_write_bytecode before source imports; --check creates no caches, dirs, temps
or index and tests compare before/after trees. Default output is repo schemas/generated;
explicit test output may be an arbitrary caller-specified directory, but no directory/file
symlinks, traversal filename, resolved child escape or implicit deletion are allowed.
Reject unknown entries without following links. Validate all documents in memory before
writing. Independently resolve each local JSON Pointer $ref, not just metaschema validation;
remote $ref forbidden, standard $schema URI is a dialect marker. Scripts import Domain
one-way. Record per-category numeric/UTC/bytes/taint/relation limitations. Targeted tests
must distinguish structural rejection from semantic rejection, not demand false equivalence.


## 11. X1 — approved shared condition data and complete Policy catalog

Status: FROZEN_AFTER_CHALLENGE; §11 implementation independently reviewed, §12–13 implementing.
§12/X2 and §13/X3 targeted Challenges accepted with §12.1/13.1 clarifications;
X4 binding-helper interface frozen in §13.2, PLAN-4-A. The user explicitly
approved G1-D01 option A and the current-ToolPlan local selector amendment. These
sections supersede earlier D01-blocked descriptions; execution_02 remains historical.
No RuleDefinition/RuleSetDefinition, operator registry, semantic TypeChecker, evaluator,
constant folding, gate engine or workflow is authorized. No new ArtifactKind/PolicyKind.

### 11.1 Shared AST data (`domain/conditions.py`)

Reuse WP-01A ValueContract and existing ContextFieldSelector/FactSelector. Central
ConditionInputDomain enum: rule_applicability/rule_condition/tool_condition;
ComparisonOperator: eq/ne/lt/le/gt/ge; ProducerCardinality: exactly_one/zero_or_one.
B owns the additive enum changes in definition_enums.py. No changes to A enum values.

Recursive ConditionExpr is a strict frozen discriminated union on kind:

| Model / kind | Required fields |
|---|---|
| LiteralExpr / literal | constant: ValueContract |
| FactRefExpr / fact_ref | selector: FactSelector, field_path must be empty |
| RecordFieldRefExpr / record_field_ref | selector: FactSelector, field_path must be nonempty |
| ContextRefExpr / context_ref | selector: existing ContextFieldSelector |
| LocalToolFactRefExpr / local_tool_fact_ref | selector: LocalToolFactSelector (§11.2) |
| ComparisonExpr / comparison | operator: ComparisonOperator; left/right: ConditionExpr |
| InExpr / in | item/collection: ConditionExpr |
| AllExpr / all; AnyExpr / any | arguments: nonempty tuple[ConditionExpr], max64 |
| NotExpr / not | argument: ConditionExpr |

Every required field explicit; extra fields forbidden. Literal is the approved
scalar/list constant language: ValueContract validates its own type/value, enum
membership and homogeneous bounded list; Record literals, including nested Record
literal members, are not part of the approved scalar/list literal language. References
may still select Record fields/values; their later operator compatibility is not decided here.
Literal list used as `in.collection` must be nonempty and <=64 values. Do not restrict
collection to literals: referenced list types/cardinality require later TypeChecker/runtime.
Root depth1, max depth32, max expression nodes512. Count every expression occurrence,
not only distinct object identities; cyclic Python inputs fail before unbounded recursion.
A shared subtree may appear twice; it counts twice and is not itself a cycle.
Typed Value containers keep their independent A depth/size limits. Schema validation
is not alone a proof of recursive budgets; targeted Pydantic negative tests are required.

Boolean root structure: a non-bool Literal root is rejected. Comparison/in/logical
nodes declare Boolean-producing syntax; bare reference roots remain supported.
Reference return types, operand signatures, enum nominal identity/units and predicates
such as eq(integer,string) are not proven by successful data parsing. No operator
TypeChecker is implemented to make this layer appear semantically complete. Children
remain full recursive expressions; semantic correctness belongs to C. Never evaluate
T/F/U, infer activation, simplify ASTs, or turn not_evaluated into TruthValue.

Owner fixes input_domain rather than duplicating a mutable label inside every node.
rule_applicability forbids all Fact/local-tool references at any depth, allowing only
Context plus literals/operators. rule_condition permits existing exact Fact/Context
refs but no LocalToolFactRef. tool_condition permits all three reference forms; binding
constraints below still apply. No Candidate/Decision/Object-image/runtime-ID node exists.

Public helpers: `validate_condition_data(raw, *, input_domain) -> ConditionExpr` for
strict shape/budget/root/domain checks; `condition_references(expr) -> ConditionReferences`
returns immutable tuples of context_selectors/fact_selectors/local_tool_selectors, each
bounded512, in preorder preserving repeated occurrences, without resolving content or
evaluating expressions. Cycle detection uses current ancestors, not a global visited set. This is shared traversal,
not a second expression parser in each consumer. Preserve injected extras when revalidating
model_copy/model_construct input; do not sanitize via a lossy model_dump first.

### 11.2 Approved local Tool Fact selector; digest-cycle repair

`LocalToolFactSelector` fields: scope=object (explicit), step_id:ArtifactId,
tool_ref:ContentRef[tool], output_name:ArtifactId, fact_key:ArtifactId,
fact_schema_ref:ContentRef[fact_schema], field_path:existing FieldPath (empty allowed).
It has no tool_plan_ref, Profile/Object/Execution/Attempt ID, digest placeholder,
IdentitySelector, free path or cross-Plan variant. Its exact Tool/FactSchema refs retain digests.
This is a use-site-relative selector inside an already pinned ToolPlan context, not a
verified content dependency or replacement for ContentRef. It is allowed only inside
tool_condition. Do not persist its resolved enclosing Plan ref back into Policy bytes.

At each ToolPlan step use, bind the enclosing exact ToolPlan ref and actual Profile.
The selected step must exist and be a strict transitive ancestor of the consuming
step; selected tool/output/key/schema must exactly match actual mapped declarations.
Self/future/sibling/missing step, unselected output, other Plan/Profile/Object and
wrong pin are rejected. A reused Policy is checked independently at every Plan/step use.
Object identity remains the current Object; no cross-Object join. Current exact Plan
identity may be used in a temporary FactSelector for finite Producer matching, never
written back into the Policy, so the Artifact forward-reference graph remains acyclic.

Existing exact ToolProducerSelector stays unchanged for contexts where its Plan ref
is a forward content dependency (e.g. future Rule condition or manifest-rooted catalogue).
Inside a ToolPlan-attached guard, same-Plan exact Tool selectors would form a digest
cycle and cross-Plan tool inputs are outside V1; use the local form for ancestor Tool Facts.
A Question/System conditional Producer has no ToolPlan step use-site for this selector:
its data branch remains supported, but a local-tool node there is rejected as unbound.
Context/exact Fact conditions remain data without claiming scheduling availability.

### 11.3 Complete Policy data

ApplicabilityPolicy: policy_kind=applicability; input_domain=rule_applicability;
context_allowlist: bounded unique tuple[ContextFieldSelector] (0..1024);
identity_constraints: unique tuple[IdentitySelector] (0..1024, only A profile/region);
condition_ast: ConditionExpr; on_unknown=block; optional bounded description (absent,
not null). Every ContextRef in AST must be an exact allowlist member. No default policy.
Identity constraints are condition data (OR within kind, AND between kinds and AST),
not membership assertions to be evaluated against the consuming Profile by this model.

ToolSkipPolicy: policy_kind=tool_skip; condition_ast: tool_condition AST OR explicit
strict Python/JSON true; on_false=skip_with_reason; on_unknown=block;
on_missing_input=block. Reject bare false/0/1/null; a typed bool false literal is valid
AST data. No default true. Structural literal-true recognition is allowed only for
matching a named `always` declaration; never fold a compound expression.

Add ApplicabilityPolicyArtifact and ToolSkipPolicyArtifact. Complete PolicyArtifact
must select exactly one of the16 existing PolicyKind branches while preserving the
four-field artifact wire schema_version/policy_id/version/policy. A tagged discriminator
which reads nested policy.policy_kind may preserve all16 specialized artifact models;
no input filtering, arbitrary dict or plain uncontrolled union. Preserve the existing
14 specialized classes and explicitly partial NonExpressionPolicyArtifact name for
compatibility; registration/resolve must use the complete type. Fields retain their
wire forms. Any Pydantic discriminator adaptation must preserve strict literal enum
validation and expose branch-specific generated Schemas; test nested resource/provider/
fallback discriminators and tainted model instances, not only raw JSON happy paths.

### 11.4 X1 files, tests and handoff

Sol-B owns conditions.py, policies.py, definition_enums.py and
validation/policies.py (complete registration/resolve; exact skip subtype alongside
existing failure checks), tests/*/test_b_conditions*.py and test_b_complete_policies*.py,
fixtures/wp01b/x1/, execution_03/logs/x1/. No other writer touches these files concurrently.
Add complete16 policy fixtures; all AST node/operand nesting round-trips; explicit
false vs0, enum/list conformance, extra/null/unknown operator/node, missing/excess arity,
32/33 depth,512/513 occurrences,64/65 logical/in literals, cycle/shared-subtree,
Applicability hidden Fact/local-node rejection, root scalar rejection, ref-root support,
local selector wire restrictions and old14 regression. Demonstrate that shape-valid
operand mismatch is NOT certified as type-correct; no evaluator/TypeChecker imports.
Run targeted pytest, Ruff/format on assigned maintained files, mypy source. Preserve
raw first failures and before/after hashes. Hand off exact public symbols to C/A.

## 12. X2 — complete Producer definitions and finite catalogue checks

Sol-C owns domain/producers.py, validation/producer_inputs.py, validation/producers.py,
X2 tests/fixtures and logs; shared ref extraction X0 below is independently assigned to C.
Only B changes definition_enums.py. Schema export is deferred until all writers release.

ProducerCatalogue(DefinitionContract): explicit schema_version=1.0.0, catalogue_id,
version, declarations: immutable0..1024 FactProducerDeclaration; unique declaration_id.
No self/root digest, no Profile back-reference to catalogue; catalogue generation is WP-02.
FactProducerDeclaration(OptionalContract): declaration_id, scope, producer_kind,
producer_id, producer_version, producer:discriminated union, output_name, fact_key,
fact_schema_ref, cardinality, activation_condition; object iff profile_ref present;
execution forbids profile_ref including null; optional description absent-or-bounded.
All IDs/versions/refs use existing A types. No runtime identity in a declaration.

Producer union has required producer_kind and no duplicate output_name:
QuestionProducer(vlm_question, question_ref); ToolStepProducer(tool,tool_plan_ref,
step_id,tool_ref); SystemProducer(system,requirement:SystemProducerRequirement).
Root kind equals nested kind. Question is object/exactly_one/primary with ID/version
from Question; Tool is object with ID/version from Tool; System supports object or
execution and ID/version from requirement capability_id/contract_version.

ActivationCondition: AlwaysActivation(mode=always), or
ToolConditionActivation(mode=tool_condition,condition_ast:ConditionExpr), discriminator
mode and fixed tool_condition domain. No defaults, arbitrary dictionary or opaque refs.
Preserve this complete union for all producer kinds. N04 effective-set obligations
cannot be removed by any condition. Non-Tool producers cannot bind local-tool nodes.
Tool declaration activation must structurally match the pinned actual ToolSkipPolicy;
Always corresponds only to its explicit strict true or direct bool-true Literal.
A compound expression is never folded, even if apparently tautological. Other conditional
branches preserve exact data and report unimplemented dynamic activation separately.

Move ProducerCatalogueContentRef's sole class definition from runtime/facts.py to
fact_selectors.py (kind=fact_producer_catalog), re-export/import it from old module to
retain API/wire compatibility. C owns these two edits as independent X0 before X1 handoff; low-level
Producer data must not import runtime or validators. Preserve all existing Fact wire.

ProducerCatalogueRegistration: ref:ProducerCatalogueContentRef + definition;
require exact ID/version and revalidation, no assertion of parent digest authenticity.
ProducerValidationContext: bounded profiles:ProfileRegistration[] (the configured
Profile set for this catalogue), questions:AuthorityQuestionInput[], plans:
AuthorityToolPlanInput[], tools:ToolRegistration[], policies:complete PolicyRegistration[],
system_capabilities:SystemCapabilityRegistration[]. Reuse these registrations, not new
parallel Definition representations. Indexed identities must be conflict checked before
lookup. Missing exact refs are incomplete, known wrong digest/content invalid.

SystemCapabilityRegistration is nonpersistent injected host data: registry_id,
family=system_fact_producer, capability_id, contract_version,
available_implementation_versions:nonempty unique bounded Versions,
supported_features:unique bounded CapabilityFeatures. A requirement cannot self-prove
registration. Identity is family/id/contract_version; conflicting host entries cannot
be selected by order. Requirement version allowlist/features must be supported; actual
implementation must satisfy both declared requirement and supplied host entry. Do not
claim these synthetic caller-supplied descriptors establish actual host or report trust.

Pure interfaces: validate_producer_catalogue(registration,context,*,fact_schemas)->
ValidationReport(producer_admission); validate_fact_producer(fact,registration,context,
*,fact_schemas)->ValidationReport. Validate entire finite context/catalogue first, then
bindings; no dict overwrite on conflicting identities, no unconditional passed checks
when prerequisites were skipped. Finite selector-to-declaration matching uses full scope,
Profile, exact producer definition/step/output/key/schema, field path via Registry.

Every configured exact Profile+Question has exactly one primary declaration (including
optional Questions in the definition catalogue); same Question may be reused by different
Profiles. Every mapped output of every step in each Profile's configured exact ToolPlan
has one declaration; no extra mapping/alias/unmapped outputs. Tool required matches
exactly_one; optional matches zero_or_one. Evidence-only/no-mapping steps create no Facts.
Schema scope/allowed_producer_kinds must agree; reuse actual Tool outputs and R1 schema.
Object collision key=(exact Profile,object,fact_key); execution collision key=(execution,
fact_key) across Profiles. Any two declarations collide regardless of schema equality,
same producer or apparently exclusive conditions. These differ from runtime candidate keys.

Fact checks: catalogue/declaration identity, source Question/Tool step/System identity,
producer kind/ID/version/output/key/schema/scope/Profile, known Value, metadata policy;
Fact source actual implementation version is separate from configured producer version.
No authority granted by Fact-only validation; raw bytes and history remain separate.
Full expression operand typing, conditional availability/mandatory completeness, real
activation and Bundle authenticity remain named deferred checks, not D01 blockers.

Tests: all3 producers/2activation branches, bothSystem scopes, absent vsnull, nested
kind mismatch, duplicate declaration/collision, Question reuse1:1, Tool multi-mapped1:N,
missing/extra primary/output, wrongscope/key/schema/required/cardinality, wrong exact pin,
unknown/conflicting host, version/features mismatch, stable catalogue, complex condition
retained and optional output not inferred absent. No simple/always-only substitute.

## 13. X3/X4 — full finite contract integration without runtime claims

X3 Sol-D composes R1 registry and X2 producer checks with existing A2 history validation;
never make low-level registry.py import Producer/Policy/Authority validators. Public entry
may live in validation/producer_authority.py. Keep existing local parse/history APIs and
their local-only reports; no consumer-ready authority API is added by deleting deferred.

ExecutionDefinitionPin is nonpersistent injected association data: execution_id:RecordId,
release_id:RecordId, bundle_digest:Digest, producer_catalogue_ref, configured_profile_refs
(unique exact Profile refs). It is not a Release/Execution root or proof of admission.
ProducerAuthorityValidationInput: history:LocalAuthorityHistoryInput;
producer_catalogues:bounded ProducerCatalogueContext[] (registration+context);
execution_pins:bounded ExecutionDefinitionPin[]. Reuse the Question/Tool/Plan entries
inside history and context; where both are supplied they must have identical exact pins
and content. Reject index-before-validation and contradictory sources, including unused
entries. Each pin's configured Profile set must match its Catalogue context.

validate_producer_authority_contracts(inputs,*,fact_schemas,parse_limits,snapshot_limits)
returns ValidationReport. Actual checks include catalogue identity, execution pin,
declaration/source binding, Profile membership, Schema/scope/key/producer permission,
N04 effective Questions consistent with exact Profile+QuestionSelectionPolicy (ordered
legal subset, mandatory included, all_declared includes all), system declaration/cardinality,
complete emitted output sets, and existing Attempt/trust/late/revision/history/raw-byte
checks. Conditional declarations never filter expected outputs: N04 whole effective batch,
N05 whole actual mapped step output set, System exact declared output. exactly_one cannot
be absent; zero_or_one has explicit emitted OR reasoned absence; nonknown is still a Fact.

Reports distinguish completed finite conformance from conditional activation truth,
semantic TypeChecker, actual Release admission, Actor/Invocation/evidence authenticity
and external history completeness. These later responsibilities remain explicitly deferred;
no synthetic truth assertion or test can promote them to passed. The existing local APIs
may remain aggregate deferred; integrated checks must actually establish Producer/Scope/
key/system-output obligations previously missing, rather than rename incomplete as passed.
Full WP-01B code acceptance concerns this complete authorized CPU contract component,
not WP-01C type/evaluation or WP-02/Runtime admission. No broad downstream consumption claim.

X4 Sol-A owns validation/conditions.py, definition_inputs.py/definitions.py and X4 tests,
fixtures/logs after X1 interface handoff. Shared AST traversal comes from conditions.py.
Replace blocked_policy_ref handling with exact full subtype resolution for Profile
applicability, QuestionSelection context rules and step ToolSkip. Check Context allowlist/
exact source/schema/owner/path; Fact refs vs actual Question/System/Tool declarations;
local-tool binding vs actual enclosing Plan/ancestor/mapping/output and R1 paths. This
validator must not import Producer validators/catalogue, to keep a DAG; C/D add unique
Producer-catalogue matching at their own layer. IdentitySelector restrictions remain
condition data, never statically decide applicability by matching current Profile.
Known wrong pin invalid; missing descriptor/definition incomplete. No predicate short circuit.

Integration test matrix adds complete Catalogue→Execution→Fact linkage, wrong catalogue
same declaration ID, conflicting context directories, crossProfile/key/Attempt, explicit
system absence cardinality, Source implementation constraints, missing/invalid authority,
optional selectedQuestion omitted, missing/extra outputs even with conditional AST,
conditional truth deferred, immutable prior snapshots and explicit revision choice.
Local selector tests include two-step/long-chain positive, self/future/sibling/other Plan,
unmapped output, same schema differentdigest, Policy reused at differing Plan uses,
no Plan digest in serialized local selector, and preserved original exact-selector wire.
Generated Schema index and full A+B test regression update only after all source writers
release. Final acceptance requires all authorized modules/tests and independent review;
no extrapolation from previous1455 count. No new dependencies are currently required.


### 12.1 Accepted X2 Challenge clarifications (frozen)

ProducerCatalogueContext(registration:ProducerCatalogueRegistration,
context:ProducerValidationContext) is a nonpersistent strict/frozen model in C-owned
validation/producer_inputs.py. No new schema_version/ArtifactKind is added to this helper.

System output-slot uniqueness is independent of fact_key collision:
(scope,exactProfile iff object,family,capability_id,contract_version,output_name)
resolves at most one declaration. Reject two such declarations even with different
keys/Schemas, because the existing System authority group/absence must resolve uniquely.

Tool activation matching is exactly:
- skip raw strict true: Always accepted; ToolConditionActivation rejected (no AST to match).
- skip direct bool-true Literal: Always accepted; ToolConditionActivation accepted iff identical AST.
- skip any other AST (including bool false): only identical ToolConditionActivation accepted.
No simplification, tautology recognition, truth evaluation or order rewriting.

X2 Fact-only validation has no actual implementation version. It checks configured
identity and leaves invocation/version provenance explicitly deferred. X3 resolves the
actual version from Tool/System ResultDescriptor; it must satisfy declaration/source
constraints and actual injected host availability, never copy an allowlist value into
an absent actual version. System requirement identity does not include its constraints;
compare permitted-version and required-feature sets plus exact optional evaluation_ref
separately between declaration and source/result/Attempt/selection requirements. Reordering
these unique allowed sets is not a new capability identity or a change of allowed values;
no mutation/canonicalization of original candidate bytes is performed.

### 13.1 Accepted X3 Challenge clarifications (frozen)

ProducerAuthorityValidationInput fields use bounded tuples (max1024). Catalogue contexts
are unique by ref kind/id/version; ExecutionDefinitionPin is unique by execution_id.
A duplicate/conflicting pin cannot win by order. Validate duplicates and wrong digests
before comparing configured Profile sets; compare the sets, not incidental storage order.

Validate every supplied Catalogue context before consuming selected items, including
unused entries. Cross-source Question/Tool/Plan registries compare reference_identity
and actual definition content, never wrapper class equality. Equal exact content can
be merged into a new immutable validation view supplied to A2; do not modify inputs.
Same ID/version different digest, same exact ref different definition, or contradictory
source declarations are invalid. Missing in both context and history is incomplete;
one complete source may supply an otherwise missing duplicate definition. No source
has precedence. Unknown external trust remains unproven despite data equality.

Execution pins cover every candidate Fact and every supplied result/selection Execution,
including System optional_absence and evidence-only Tool results with zero Facts.
Missing pins/catalogues are incomplete; known contradictory pins are invalid. All
candidate Facts require declaration/Schema/source/scope/Execution/Attempt association.
Only selected Results require succeeded/on_time/trusted eligibility: unselected failed/
late attempts remain valid historical records, never silently removed or rewritten.

Old local report check IDs are not globally ignored. Integrated X3 replaces only the
specific portions it actually performs: full_authority_admission -> catalogue/source/
Scope/output checks plus explicit dynamic-activation deferred; profile_release_admission
-> Profile/pin/Question-partition checks plus real Release-admission deferred;
system_output_declaration -> actual declaration/cardinality/absence checks;
external_provenance remains deferred. Keep every failed/incomplete local check and
explicit diagnostic-overflow invalidity; do not collapse them into a nicer status.
N04 expected set is the effective subset, never all optional catalogue Questions;
N05 uses actual mappings and System uses its uniquely declared output. Conditions do
not filter these sets. Complete finite tests may pass while named future runtime
responsibilities remain deferred; no consumer-ready verified-authority view is exposed.


### 13.2 Accepted X4 binding-helper interface (frozen)

A owns validation/condition_inputs.py and validation/conditions.py. The former defines
ConditionReferenceContext as strict discriminated use_role branches (all collection
fields required, bounded1024, no defaults):

Common: context_registrations:ContextTypeRegistration[], descriptors:SchemaDescriptor[],
optional region_ref (absent, not null). Anchors:
- applicability: profile_ref, policy_ref; optional question_ref only for a selection
  context-rule use. No catalogue/declaration/Plan/step fields.
- tool_step_guard: profile_ref, policy_ref, tool_plan_ref, step_id. No Question/catalogue
  or producer fields.
- producer_activation: producer_catalogue_ref, declaration_id, producer_kind,
  producer_scope; object iff profile_ref present, execution forbids profile_ref;
  Tool iff tool_plan_ref+step_id present; non-Tool forbids both. No policy/question refs.

producer_catalogue_ref reuses the sole lower-level class moved by C to fact_selectors.
No condition-input model imports producers.py, producer_inputs.py or Producer validators.
ConditionReferenceResult is report:ValidationReport plus immutable bounded512
selectors_for_matching:FactSelector[]. Failed/incomplete binding yields no usable
projection, never a partial set mistaken for complete. Explicit runtime/type/producer
checks may remain named deferred even when reference bindings succeed.

validate_condition_references(condition:ConditionExpr|Literal[True],context,*,
input_domain,profiles:ProfileRegistration[],questions:AuthorityQuestionInput[],
tools:ToolRegistration[],plans:AuthorityToolPlanInput[],fact_schemas:FactSchemaRegistry,
context_allowlist:tuple[ContextFieldSelector,...]|None)->ConditionReferenceResult.
No implicit empty/context defaults. Applicability receives actual allowlist; Tool/Producer
receives None and still validates explicit registered sources/paths. Validate all supplied
identity conflicts before selective lookup. Scope/Profile/Plan/step/Region anchor mismatch
is invalid, missing necessary owner/view/descriptor incomplete. Scope is execution for an
execution producer activation; all other use-sites are object/Profile-bound. Object scope
can read declared execution System Facts; execution scope cannot read an Object Fact.

The helper uses shared domain.conditions traversal; it proves finite selector identity,
source-owner/schema/path and local Plan/strict ancestry/output relationships, not operand
signature/Boolean return type. Same-Plan exact Tool references are invalid within attached
Tool guards/Tool activation (must use approved local form). This is not generalized to
manifest-rooted Question/System activation, where existing forward exact refs remain data;
scheduling availability/activation remains unproven. System output→declaration key/schema
binding is completed by C, not inferred from a host registration. Temporary local selectors
use the actual enclosing exact Plan ref only in the result, never mutate source data.

Caller first verifies context anchors correspond to the actual policy/declaration and
condition: V1 resolves exact Policy subtype/context-rule association, C verifies actual
declaration and ToolSkip activation match. The helper cannot self-prove condition origin
from a context.policy_ref or declaration_id. This avoids copying Policy/Catalogue authorities
into its signature. B validation/policies.py must NOT import this higher helper (cycle).

C ProducerValidationContext adds required condition_contexts:ConditionReferenceContext[]
(max1024), with each supplied entry producer_activation anchored to the exact catalogue+
declaration+scope/Profile+Plan/step; optional Region distinguishes separate uses. Duplicate
use keys or known contradictory anchors reject before lookup; validate every supplied use,
including unused ones. Conditional selectors requiring owner/type views with no matching
use return incomplete. A conditional tree with no references may have explicit empty
contexts without pretending dynamic activation was evaluated. Always requires no condition
environment. Other entries cannot silently supply producer context by similar IDs.

A keeps applicability IdentitySelector checks about permitted kind/exact version and
identity completeness separate from comparing against the current Profile/Region: it
must not compute eligibility TRUE/FALSE or reject a policy merely because a condition
would be false. Bundle-level unprovided identity targets are reported unverified rather
than made into fabricated content dependencies.
