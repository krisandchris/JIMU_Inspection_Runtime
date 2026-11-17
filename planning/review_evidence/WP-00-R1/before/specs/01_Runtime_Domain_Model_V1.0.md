# 01 — Runtime Domain Model V1.0

**Status:** Proposed normative contract baseline  
**Depends on:** README.md, AGENTS.md  
**Scope:** Native runtime domain, independent of the existing requirements platform.

## 1. Design rules

1. Definition-time configuration and execution-time evidence are separate bounded contexts.
2. Definitions are immutable once published; changes create a new version.
3. Runtime records pin exact definition references and digests.
4. Domain types do not depend on FastAPI, SQLAlchemy, Redis, model SDKs or storage vendors.
5. No runtime inference result mutates a definition.
6. `Entity`, `Profile`, `Object`, `Question`, `Fact`, `Tool`, `ToolExecution`, `Rule` and `Decision` are distinct concepts.

## 2. Bounded contexts

### 2.1 Definition context

| Aggregate | Entities | Responsibility |
|---|---|---|
| Ontology | Entity, Region | Business subject identity and scene/asset context |
| Capability | Profile, Facet, Question | What subjects to discover and what facts to observe |
| Tooling | Tool, ToolPlan | Registered executable capabilities and deterministic tool DAG |
| Decision | Rule, RuleSet | Typed rule predicates, applicability and conflict policy |
| Model | ModelBinding | Logical model capability to approved provider/deployment policy |
| Release | Bundle, Release, Activation | Immutable executable configuration and activation scope |

### 2.2 Execution context

| Aggregate | Entities | Responsibility |
|---|---|---|
| Intake | Task, MediaAsset, TaskContext | Accepted image request and trusted metadata |
| Execution | Execution, NodeRun, Attempt, Event | Durable workflow lifecycle |
| Perception | Object, GroundingRecord | Profile-specific localized instances |
| Facts | Fact, FactSchemaRef | Typed observations with producer and status |
| Tools | ToolExecution | Actual tool invocation and output provenance |
| Evidence | EvidenceAsset | Immutable image/mask/response/geometry references |
| Decision | CandidatePrediction, Decision, RuleEvaluation | Candidate generation and deterministic final result |
| Model audit | ModelInvocation | Actual provider/model/deployment invocation audit |
| Replay | ReplayRun | Artifact replay and inference replay |

## 3. Entity definitions

### 3.1 Entity

Business subject ontology. It must not contain a detected instance box or violation decision.

Required fields:
- `entity_id`
- `version`
- `name`
- `entity_type`
- `description`
- `attribute_schema_refs[]`
- `lifecycle`

Relationship: one Entity may support multiple Profiles.

### 3.2 Region

Represents inspection scope, asset/ledger identity and scene context. Region is not merely a polygon.

Required fields:
- `region_id`
- `version`
- `name`
- `region_type`
- `scene_type`
- `asset_refs[]`
- `context_schema_ref`
- optional `geometry_ref`

Keep `RegionDefinition` separate from per-task `TaskContext`. Device, camera and region IDs are distinct identities.

### 3.3 Profile

Reusable subject capability definition.

Required fields:
- `profile_id`, `version`
- exact `entity_ref`
- `discovery_capability_ref`
- `grounding_capability_ref`
- `facet_refs[]`
- `question_refs[]`
- exact `tool_plan_ref`
- `applicability`

A Profile is not a violation category. One Profile may yield zero, one or many Objects. The effective Question set is frozen into the Release and selected from the Profile's declared, validated Question references.

### 3.4 Facet

An observation dimension used to organize reusable Questions and Fact keys.

Required fields:
- `facet_id`, `version`
- `name`, `description`
- `fact_schema_refs[]`
- optional metadata and tags

Facet-to-Question relationship is many-to-many, but each Question must declare compatible Facet references. Reuse across Profiles is allowed only if the Question's input context and output schema are compatible with every referencing Profile.

### 3.5 Question

A versioned observation task, not a free-form prompt alone.

Required fields:
- `question_id`, `version`
- `objective`
- `prompt_template_ref`
- `applicable_profile_refs[]`
- `facet_refs[]`
- `context_allowlist[]`
- `output_fact_schema_refs[]`
- `evidence_policy`
- `unknown_policy`
- `validation_policy`

A Question never makes a final violation decision. The VLM output is untrusted until schema validation. Each Question result becomes an independently addressable Fact record.

### 3.6 Tool

An allowlisted executable capability.

Required fields:
- `tool_id`, `version`
- `tool_type`
- `provider_kind`
- `input_schema_ref`
- `output_schema_ref`
- `resource_requirements`
- `execution_policy`
- `capability_tags[]`

Bundle data may reference a Tool but may not supply arbitrary executable code, shell commands or dynamic imports.

### 3.7 ToolPlan

A Profile-bound directed acyclic graph of Tool steps.

Required fields:
- `tool_plan_id`, `version`
- `profile_ref`
- `steps[]`
- explicit dependency edges
- input/output mappings
- required/optional designation
- failure and skip policies

A ToolPlan is deterministic configuration. It cannot be invented by an LLM during execution. Conditional branches may depend only on declared context or validated Facts and must be statically type-checked. N05 does not wait for or branch on N06 violation predictions.

### 3.8 Rule and RuleSet

A Rule contains:
- `rule_id`, `version`
- violation category
- applicable Profile/scene/Region predicates
- required Fact references
- typed `condition_ast`
- missing/unknown/error policy
- evidence requirements
- result mapping

A RuleSet groups Rules and defines applicability and conflict resolution. Rule order in storage is never an implicit priority.

Rule AST is a typed, allowlisted data structure. It is never Python source and never evaluated with `eval`/`exec`.

### 3.9 ModelBinding

Separates logical capability from actual provider/deployment.

Required fields:
- `model_binding_id`, `version`
- `capability`
- logical model ID
- ordered provider policy
- required input/output contract
- timeout and concurrency policy
- explicit fallback policy
- allowed deployment/version constraints

Credentials are never part of a ModelBinding or Bundle.

## 4. Runtime records

### 4.1 Task

One accepted inspection request. Required fields:
- `task_id`, `request_id`, idempotency scope/key
- immutable media reference and SHA-256
- `device_id`, `camera_id`, `region_id`
- capture/occur time plus clock provenance
- selected `release_id`
- acceptance timestamp and status

### 4.2 Execution

One actual run of a Task. A Task may have multiple Executions for retry/replay/version comparison. Every Execution pins:
- Release ID and Bundle digest
- runtime and workflow versions
- configured and actual model deployment identities
- Tool versions
- input digest
- context snapshot digest

### 4.3 Object

A localized instance within one Execution:
- `object_id`, `execution_id`
- `entity_ref`, `profile_ref`
- BBox in original-image pixel XYXY coordinates
- source image dimensions and coordinate-system ID
- grounding producer/model invocation
- visibility and localization status
- evidence references

Object IDs are unique within an Execution. They are not globally stable identities across images.

### 4.4 Fact

Fact is a typed value envelope plus a schema-governed payload. Required fields:
- `fact_id`, `execution_id`
- explicit scope: `object` or `execution`
- `object_id` when scope is object
- `fact_key`, schema reference and schema digest
- producer kind: `vlm_question`, `tool`, `system`
- producer ID and producer version
- status: `known`, `unknown`, `error`, `not_applicable`, `not_evaluated`
- typed value only when status permits it
- confidence as model-reported metadata, not calibrated truth
- evidence references
- creation time and trace IDs

Do not use an unrestricted `Any` as the persisted value contract. Use a discriminated union or resolve the declared Fact JSON Schema and validate the payload before persistence. `known` requires a valid value. Non-known states must follow the schema's null/value policy and include a reason where required.

### 4.5 ToolExecution

Records one actual Tool invocation:
- Tool ref and actual implementation version
- input/output references and digests
- Object/Execution scope
- Attempt and status
- timing, resource usage and error class
- generated Fact references and Evidence references

### 4.6 EvidenceAsset

Stores metadata and an immutable reference to the object store:
- evidence ID/type
- object/execution scope
- URI/key, SHA-256, MIME type, byte size
- dimensions/coordinate system where relevant
- producer identity/version
- access scope and retention class
- creation time

### 4.7 Decision

Stores final deterministic Rule evaluation:
- decision ID and scope
- candidate prediction reference (if used)
- applicable/evaluated/skipped/blocked Rule refs
- matched Rule refs
- result: `violation`, `no_violation`, `uncertain`, `not_applicable`
- Fact and Evidence references
- Rule Engine version
- explanation trace and conflict-resolution result

A candidate prediction is not a final Decision.

## 5. Relationship cardinalities

- Entity 1:N Profile.
- Profile M:N Facet.
- Profile M:N Question, resolved to a deterministic effective set at Release compile time.
- Profile 1:N ToolPlan versions over time; a given Profile version pins one exact ToolPlan.
- RuleSet 1:N Rule references.
- Rule consumes N Facts; Fact has one authoritative producer record, though derived Facts may reference multiple inputs.
- Task 1:N Execution.
- Execution 1:N NodeRun, Object, Fact, ToolExecution, ModelInvocation and Decision.
- Object 1:N object-scoped Fact, ToolExecution and Evidence.
- Decision N:1 Rule references and N:M Fact/Evidence references.

## 6. Status domains (separate enums)

Task:
`created`, `accepted`, `queued`, `running`, `completed`, `partial_failed`, `failed`, `cancelled`.

Execution:
`initialized`, `running`, `completed`, `partial_failed`, `failed`, `cancelled`.

NodeRun:
`pending`, `ready`, `running`, `succeeded`, `failed`, `blocked`, `skipped`, `cancelled`.

Attempt:
`created`, `running`, `succeeded`, `failed`, `timed_out`, `cancelled`, `superseded`.

Fact:
`known`, `unknown`, `error`, `not_applicable`, `not_evaluated`.

Decision:
`violation`, `no_violation`, `uncertain`, `not_applicable`.

Never reuse one enum across these domains.

## 7. Versioning and immutability

- Definition identity is `(artifact_id, version)`.
- Published artifacts are immutable.
- Exact references include a content digest.
- Bundle freezes the transitive dependency closure.
- Execution pins one Release and Bundle digest.
- Runtime implementation versions are separately recorded from configured artifact versions.
- Deprecation prevents new activation/use according to policy but does not erase history.

## 8. OPEN decisions

- Whether Region geometry is in this service or a spatial subsystem.
- Whether a Profile can select among multiple ToolPlans by declared context.
- Cross-Entity Question/Facet reuse constraints.
- Rule conflict semantics for simultaneous violations.
- Final Fact value union implementation and JSON Schema dialect enforcement details.
