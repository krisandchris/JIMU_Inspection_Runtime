# 01 — Runtime Domain Model V1.0

**Status:** Proposed normative contract baseline  
**Depends on:** README.md, AGENTS.md  
**Scope:** Native runtime domain, independent of the existing requirements platform.

**Review revision:** WP-00-R2.1.1, based on commit `2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`. Revised contracts below are decision
candidates: PROPOSED / PENDING_USER_APPROVAL, not implementation authorization.
R2.1 bytes are retained in `planning/review_evidence/WP-00-R2.1.1/before/`.
R1/R2/R2.1 audit history is unchanged. R3-01/R3-02 clarify applicability only;
F01–F05 and all D01–D15 approval gates remain in force.

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
| Configuration package | Bundle | Immutable definition closure; no activation state |

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
| Release bridge | Release, Activation | Runtime records pinning admitted configuration; approval and activation are separate |

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
- `discovery_binding_ref` (ContentRef[model_binding], capability=profile_discovery)
- `grounding_binding` (ModelGroundingBinding | ToolGroundingBinding; §10)
- `observation_binding_ref` (ContentRef[model_binding], capability=object_observation)
- `facet_refs[]`
- `question_refs[]`
- `question_selection_policy_ref` (ContentRef[policy], QuestionSelectionPolicy)
- `rule_set_refs[]` (ContentRef[rule_set])
- exact `tool_plan_ref`
- `applicability_policy_ref` (ContentRef[policy], ApplicabilityPolicy)

A Profile is not a violation category. One Profile may yield zero, one or many
Objects. Release freezes the declared Question catalogue and selection policy;
N02 records a per-Profile effective_question_refs set under that pinned policy.
N03 inherits it unchanged; N04 uses it in one joint request per Object (§11).

### 3.4 Facet

An observation dimension used to organize reusable Questions and Fact keys.

Required fields:
- `facet_id`, `version`
- `name`, `description`
- `fact_schema_refs[]`
- optional metadata and tags

Facet-to-Question relationship is many-to-many. fact_schema_refs is a nonempty
allowlist of exact FactSchemaDefinition refs (kind=fact_schema), not a producer
declaration. Question.primary_fact_schema_ref must belong to the intersection
of every linked Facet's allowlist. Multiple Facets may reuse the same exact Schema.
An empty intersection or merely similarly shaped/different-digest Schema rejects
the Question. Profile must declare all Facets of each referenced Question. Reuse
also checks context, units, semantic_key and producer scope (§12); facet membership
does not authorize creating extra Facts.

### 3.5 Question

A versioned observation task, not a free-form prompt alone.

Required fields:
- `question_id`, `version`
- `objective`
- `prompt_template_ref`
- no reverse Profile membership field (R2 D15 candidate; derived from Profile.question_refs)
- `facet_refs[]`
- `context_allowlist[]`
- `primary_fact_key`
- `primary_fact_schema_ref` (exact ContentRef; exactly one primary output)
- `evidence_policy_ref` (ContentRef[policy], EvidencePolicy)
- `unknown_policy_ref` (ContentRef[policy], UnknownPolicy)
- `validation_policy_ref` (ContentRef[policy], QuestionValidationPolicy)

A Question never makes a final violation decision. For each Object and authoritative
observation Attempt, one Question has exactly one primary Fact result. Compound
observations use a restricted Record value in that Fact, not multiple output slots.
Record fields do not have independent Fact status; an invalid required field makes
the entire primary Fact error. Separate independently uncertain observations into
separate Questions. Transport/whole-envelope parse failure creates no fabricated
Facts. Retry history can contain multiple immutable Facts; consumption selects an
explicit authoritative Attempt. VLM output remains untrusted until validation.

### 3.6 Tool

An allowlisted executable capability.

Required fields:
- `tool_id`, `version`
- `tool_type`
- `provider_kind`
- `input_schema_ref`
- `output_schema_ref`
- `resource_requirements_ref` (ContentRef[policy], ResourceRequirements)
- `execution_policy_ref` (ContentRef[policy], ToolExecutionPolicy)
- `evidence_policy_ref` (ContentRef[policy], EvidencePolicy)
- `fact_outputs[]` (ToolFactOutputDeclaration; §13; may be empty for evidence-only Tools)
- `capability_tags[]`

Bundle data may reference a Tool but may not supply arbitrary executable code, shell commands or dynamic imports.

### 3.7 ToolPlan

A Profile-bound directed acyclic graph of Tool steps.

Required fields:
- `tool_plan_id`, `version`
- no reverse Profile membership field (derived from Profile.tool_plan_ref)
- `steps[]`
- explicit dependency edges
- input/output mappings
- required/optional designation
- `failure_policy_ref` and `skip_policy_ref` on each step (ContentRef[policy], ToolFailurePolicy / ToolSkipPolicy)

A ToolPlan is deterministic configuration. It cannot be invented by an LLM during execution. Conditional branches may depend only on declared context or validated Facts and must be statically type-checked. N05 does not wait for or branch on N06 violation predictions.

### 3.8 Rule and RuleSet

A Rule contains:
- `rule_id`, `version`
- violation category
- `applicability_policy_ref` (ContentRef[policy], no Object Facts allowed)
- `required_fact_selectors[]` and `optional_fact_selectors[]` (FactSelector, §13)
- typed `condition_ast`
- `rule_policy_ref` (ContentRef[policy], RulePolicy)
- `evidence_policy_ref` (ContentRef[policy], EvidencePolicy)

A RuleSet groups Rules and defines applicability and conflict resolution. Rule order in storage is never an implicit priority.

RuleSet has rule_set_id/version, exact rule_refs, applicability_policy_ref,
conflict_policy_ref (ConflictPolicy) and optional
candidate_binding_ref (ContentRef[model_binding], candidate_prediction). Absence
of the candidate binding disables advisory prediction; mandatory scanning remains.

Rule AST is a typed, allowlisted data structure. It is never Python source and never evaluated with `eval`/`exec`.

### 3.8.1 F01 / R3-01 / R3-02 — applicability ownership and ordered gates

Profile → RuleSet → Rule is an ordered hierarchy of required gates, not a single
flattened AND expression. All three must return TRUE before a Rule condition may
be entered, subject to its separate Required Evidence Guard. No level overrides
an ancestor or grants membership; Profile's forward RuleSet/Rule refs remain the
authority. All levels pin the same Execution, Release/Bundle and ContextSnapshot.
Inputs are declared ContextSnapshot/Region/Profile metadata only: no Object Facts,
N02 Candidates/discovery results, N06 Candidates/Decisions or discovery confidence.
T/F/U abbreviate RuleTruth=true/false/unknown. not_evaluated is an execution status
with no truth field; it is not a fourth TruthValue or an alias for UNKNOWN.

| Contract stage | Owner and allowed work | Durable result |
|---|---|---|
| N01 | Context Builder only; build/validate immutable ContextSnapshot | ContextBuildOutput; no Profile eligibility results |
| Pre-N02 Profile Eligibility Gate | Workflow Engine internal deterministic precondition, after successful N01; resolve each configured Profile's pinned ApplicabilityPolicy | ProfileEligibilityEvaluation and Profile ApplicabilityTraces |
| N02 | Discover only within eligible_profiles; never evaluate Profile eligibility or author its exclusion records | ProfileHits / no_hits and discovery diagnostics over eligible Profiles only |
| N06 | Inherit the fixed Profile eligibility result; evaluate RuleSet then Rule applicability for reached branches | RuleSetApplicabilityEvaluation / RuleApplicabilityEvaluation and extended traces |

The Pre-N02 Gate is not a seventh business node, NodeId, model call or new
architecture layer. Its records are Workflow Engine execution evidence; they do
not fabricate a Gate NodeRun/Attempt or become N01 outputs. If N01 fails required
context validation, the Gate is not evaluated; existing lifecycle/coverage records
retain that blocker rather than invented eligibility results. Policy-required
metadata still unavailable in a valid Snapshot yields UNKNOWN at the Gate.

At each reached level: FALSE excludes that level's subtree with decisive evidence;
UNKNOWN creates an UnresolvedObligation and stops that chain; TRUE permits the
next level. Descendants behind FALSE/UNKNOWN are not_evaluated with an ancestor
reference, never independently false/unknown. Do not evaluate a later policy to
erase an ancestor's result. Strong Kleene remains the expression semantics *inside
one* Policy AST or Rule Condition AST; it does not combine these lifecycle gates.

| Profile | RuleSet | Rule | Effective gate / obligation |
|---|---|---|---|
| F | not_evaluated | not_evaluated | F; exclude Profile obligations, retain Profile exclusion evidence |
| U | not_evaluated | not_evaluated | U; Profile unresolved, descendants blocked by ancestor |
| T | F | not_evaluated | F; skip this RuleSet's Rules with ancestor exclusion, not other RuleSets |
| T | U | not_evaluated | U; this RuleSet's obligations unresolved/blocked |
| T | T | F | F; skip Rule with its specific exclusion evidence |
| T | T | U | U; this Rule unresolved/blocked |
| T | T | T | T; Rule condition may proceed subject to required Facts/Evidence/Producer Authority guard |

Consequently P=U with an offered RS=F is still Profile unresolved and RS must not
execute; P=F with an offered Rule=F records only the Profile exclusion; P=T,
RS=U with an offered Rule=F keeps RS unresolved and Rule not_evaluated. Such
offered child results are invalid execution records, not valid alternative traces.

**Evaluation records (Runtime, not Policy definitions):**

- ProfileEligibilityEvaluation: schema_version, evaluation_id, execution_id,
  release_id, bundle_digest, context_snapshot_ref, workflow_version, evaluated_at,
  configured_profile_refs, eligible_profiles, excluded_profiles,
  unresolved_profiles and applicability_traces (a list of RuntimeRecordRef to
  ApplicabilityTrace). All Profile refs are exact
  ContentRef[profile] from the pinned scope. The three result lists are disjoint,
  duplicate-free and exhaustive over configured_profile_refs, in configured order.
  Each configured Profile has exactly one referenced Profile trace: evaluated
  T/F/U maps respectively to eligible/excluded/unresolved. There is no aggregate
  truth value. An empty configured scope is explicitly recorded, not inferred
  from discovery. Independent persistence requires schema_version.
- RuleSetApplicabilityEvaluation / RuleApplicabilityEvaluation: schema_version,
  evaluation_id, execution_id, release_id, bundle_digest, context_snapshot_ref,
  profile_eligibility_evaluation_ref, profile_ref, rule_set_ref, applicability_trace_ref,
  recorded_at and object_ref only when associated with a real Object-scoped N06
  invocation; Rule evaluation also requires rule_ref and
  rule_set_applicability_evaluation_ref. Their result is the referenced target-level
  trace, not a second separately writable truth. An actually evaluated level
  requires its real N06 node_run_ref/attempt_ref. A not_evaluated descendant is an
  ancestor projection with decisive_ancestor_ref; it has no fabricated invocation
  refs or locally evaluated truth. Repeated N06 Attempts append distinct records;
  an explicit selected evaluation revision is pinned by Decision/Coverage, never
  last-write-wins. A projected record does not imply that N06 ran.

Ancestor projections apply only to a chain stopped by an evaluated applicability
ancestor. If no such ancestor exists and N06 is unreached because of no_hits,
grounding failure or another upstream blocker, Coverage retains that dependency
reason/ref without fabricating an applicability UNKNOWN or a child gate trace.

The Gate aggregate owns the Profile partition. N06 may reference but must not
rewrite it. RuleSet/Rule records wrap the target evaluation; ApplicabilityTrace
explains the ordered path. ExecutionCoverage aggregates obligations; it is neither
a Policy nor a second evaluator. These are RuntimeRecordRef targets, not new
ArtifactKinds or Bundle configuration resources.

ApplicabilityTrace is an immutable versioned Runtime record: applicability_trace_id,
execution_id, target_ref (ContentRef[profile/rule_set/rule]), profile_ref,
context_snapshot_ref/digest, levels[] and effective_truth (T/F/U), disposition
(included/excluded/unresolved), decisive_level and created_at. Each
ApplicabilityLevelTrace contains level (profile/rule_set/rule), target_ref,
policy_ref, evaluation_status (evaluated/not_evaluated), truth (T/F/U, required
only if evaluated), declared_input_refs, reason_code, reason_evidence_refs and
validation_issues. reason_code is included_by_policy/excluded_by_policy/
metadata_unknown/metadata_invalid/ancestor_excluded/ancestor_unresolved.
Every evaluated level identifies evaluation_owner=pre_n02_gate/n06 and its
source_evaluation_ref. Profile levels require the former; RuleSet/Rule levels the
latter. Reused ancestor prefixes reference the original records rather than imply
reevaluation. source_evaluation_ref is an ID-based RuntimeRecordRef, not a digest
of a record that hashes the trace; no cyclic content addressing is introduced.
FALSE requires nonempty exclusion reason and pinned-policy/declared-input evidence refs;
UNKNOWN requires an unresolved reason and missing/invalid metadata diagnostics.
not_evaluated entries forbid truth, local evaluation_owner/source_evaluation_ref
and evaluated-input evidence; they require decisive_ancestor_ref pointing to the
first decisive ancestor level in the same pinned chain. A Profile target has one
level, RuleSet two, Rule three; no fabricated child result in a Profile trace.
decisive_ancestor_ref is a RuntimeRecordRef to that ancestor's source evaluation;
the inherited level/target/Profile refs must resolve its exact result. It is not
an untyped path or a fabricated ID for an independently persisted child level.
For descendant targets, effective_truth is the decisive *ancestor* truth with
disposition excluded/unresolved; it is not a child truth or an AND reduction.

UnresolvedObligation is the typed unresolved branch of the existing obligation
ledger, not another obligation count: schema_version when independently stored,
obligation_id, execution_id, profile_ref, scope=profile/rule_set/rule, target_ref
with matching kind, optional object_ref only when an actual Object exists,
applicability_trace_ref, reason_code=metadata_unknown/metadata_invalid/
ancestor_unresolved, nonempty diagnostic_refs and decisive_ancestor_ref iff
ancestor-derived. RuleSet/Rule targets include their exact parent refs. The same
obligation_id is used by MandatoryRuleObligation/Coverage/Decision references;
one unresolved ancestor is not counted again as a new unresolved child policy.
Descendant ledger entries carry not_evaluated plus the ancestor blocker.

ExecutionCoverage keeps the Profile gate reference when evaluated and separate excluded,
unresolved, blocked and not_evaluated obligations with trace/decisive-ancestor refs,
even with no Objects/N06 invocation. Excluded branches are removed from execution
obligations only with proof; unresolved branches remain blockers. Mandatory scanning
accounts for the full configured hierarchy, by evaluated records or ancestor
projections, not by running child policies/conditions behind stopped gates.
Decision, when actually produced, preserves this ledger linkage and corresponding
skipped/blocked/not_evaluated Rule refs; do not invent per-Object Decisions to hold
Profile-level gaps. No unresolved required branch permits definite no_violation.

If eligible_profiles is empty, N02 is skipped with reason=no_eligible_profiles
and discovery outcome=not_evaluated, without a fabricated ModelInvocation/no_hits.
All configured Profiles proven excluded gives not_applicable scope; any unresolved
Profile gives none/uncertain. A nonempty eligible catalogue with a real discovery
no_hits remains none/uncertain under D09. Exclusion and no_hits are different facts.
Mixed eligible/unresolved scopes preserve successful work and unresolved coverage.

N05 retains its pre-bound ToolPlan and own declared Context/validated-Fact guards.
RuleSet/Rule applicability must not select, modify or cancel N05 steps. WP-01 only
defines these models, finite-reference invariants and AST static type checks; no
Gate Evaluator, Workflow Engine or Rule runtime is implemented in WP-01.

### 3.9 ModelBinding

Separates logical capability from actual provider/deployment.

Required fields:
- `model_binding_id`, `version`
- `capability`
- logical model ID
- `provider_policy_ref` (ContentRef[policy], ProviderPolicy)
- `input_schema_ref`, `output_schema_ref` (SchemaRef, allowed kinds in §9)
- `prompt_template_ref` for language/vision-language capabilities (purpose-compatible ContentRef[prompt_template]; optional only for vision_auxiliary without a prompt)
- `resource_requirements_ref` (ContentRef[policy], ResourceRequirements)
- `fallback_policy_ref` (ContentRef[policy], FallbackPolicy)
- allowed deployment/version constraints

Credentials are never part of a ModelBinding or Bundle.

### 3.9.1 F02 — Remote Primary and explicit local exceptions

ProviderPolicy has routing_mode=remote_primary/local_primary_exception,
primary_deployment_id, deployments[] and selection=primary_then_explicit_fallback.
All fields are explicit: primary_deployment_id identifies exactly one registered
DeploymentRequirement; other entries are fallback-only, not automatic eligible
alternatives. remote_primary requires that primary's provider_kind=remote.
An unavailable/timed-out primary produces a failure unless FallbackPolicy
explicitly permits that trigger and exact from/to transition; order alone never
authorizes switching models, providers or deployment versions. A retry of the same
approved deployment is not permission for a model-changing fallback.
If several transitions are explicitly permitted for the actual trigger, target
order follows deployments[] only within that authorized, compatible and evaluated
set; list order never grants permission or bypasses the checks.

local_primary_exception requires a local primary and a LocalPrimaryException:
exception_id/version, applicable_capabilities[], reason (explicit_local_capability/
privacy_restriction/unavailable_remote_capability), bounded rationale, approval_ref
(ReviewReportRef), valid_from and valid_until. It applies only to declared
ModelBinding capabilities and approved validity/scope; absence, expiry or mismatched
capability rejects the local-primary configuration. Approval is verified against
trusted Runtime review authority at admission; Bundle text is not self-approval.
valid_from/valid_until are aware UTC timestamps with valid_from < valid_until.
remote_primary forbids this exception field. This candidate shape permits review
of exceptions; no local-primary exception is approved by R2.1.

Every fallback transition requires exact IO contract compatibility and independent
quality evaluation with versioned ReviewReportRef covering source/target model,
capability and approved scope. A timeout cannot create an exception or evaluation.
ModelInvocation records configured_provider (ConfiguredProviderIdentity: binding_ref,
provider_policy_ref, primary_deployment_id, provider_id, provider_kind, logical_model_id), and
actual_provider (ActualProviderIdentity: provider_id, deployment_id, provider_kind,
model_id, model_version when resolved, version_resolution=reported/registry_verified/unavailable
and evidence/reason). A
ProviderRouteTrace records selected deployment, routing_mode, optional exception
identity, fallback_policy_ref, trigger and evaluated transition when used.
Its route_kind=primary/fallback determines fields: fallback requires from/to
deployment IDs, normalized trigger_error and evaluation_refs; primary forbids
fallback-trigger fields. DeploymentRequirement includes provider_id explicitly.
actual_provider is absent only before dispatch/no dispatch; unresolved actual
model_version is explicitly unavailable with reason, never copied from the
configured expected version. Attempt/provider request IDs preserve actual calls.

This is Model Gateway routing, not Tool Worker scheduling. SAM3/geometry/local
vision Tools run under their ToolDefinition, implementation allowlist and resource
budgets; they do not require a Remote Primary model call or LocalPrimaryException.
If a Tool explicitly invokes the Model Gateway through a ModelBinding, that nested
gateway call follows its own ProviderPolicy and is independently audited. Tool
weights/CPU/GPU placement alone do not imply a gateway routing decision.

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
- configured ModelBinding ContentRefs, deployment constraints and Tool versions at creation
- append-only ModelInvocation references recording actual provider/deployment/model versions after invocation
- Tool versions
- input digest
- context snapshot digest attached once N01 resolves it; unavailable before then, never invented

### 4.3 Object

A localized instance within one Execution:
- `object_id`, `execution_id`
- `entity_ref`, `profile_ref`
- BBox in original-image pixel XYXY coordinates
- source image dimensions and coordinate-system ID
- grounding producer/model invocation
- `question_selection_ref` and inherited `effective_question_refs[]`
- visibility and localization status
- evidence references

Object IDs are unique within an Execution. They are not globally stable identities across images.

V1 identity is Profile-specific: the same visible vehicle grounded under two
Profiles produces two Object records with distinct object_ids. Each record owns its
own Facts, Questions and Decisions. No IoU-based merging, cross-Profile Fact reuse,
global physical identity, or N02 instance alignment is implied. Object/Fact/Decision
references must agree on execution_id and profile_ref. Cross-Profile deduplication
is deferred to a separately versioned association contract; V1 counts are counts
of Profile observations, not unique physical vehicles (D10).

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

Use a discriminated typed value validated against the exact registered Fact Schema;
unrestricted Any and arbitrary imported JSON Schema are forbidden. The V1 type
algebra is bool, integer, finite number, bounded string, closed enum, bounded
homogeneous list, and closed Record. Records have declared named fields, required
flags and bounded nesting; undeclared fields and arbitrary recursive maps are
rejected. JSON Schema dialect candidate: 2020-12, generated restricted subset,
offline references only. `known` requires a valid value. All other statuses forbid
the value field (including null) and require a structured reason. Business enum
`unknown` must not duplicate FactStatus. Record field selection in AST uses a
declared field path and exact schema, not arbitrary JSONPath or code.

### 4.5 ToolExecution

Records one actual Tool invocation, with explicit schema_version,
tool_execution_id, execution_id, profile_ref, tool_ref, node_run_ref, attempt_ref,
input_refs, output_refs (immutable typed refs/digests), actual_implementation_version,
scope (execution/object), status/timing/resource/error provenance,
generated_fact_refs, evidence_refs and invocation_origin. object_ref is required
only for object scope. Runtime refs and actual implementation are not interchangeable
with definition ContentRefs. All parent execution/node/Attempt/Profile identities
must agree. Actual version is recorded when resolved; before dispatch it is absent
with implementation_version_resolution=not_dispatched. Other unresolved cases use
implementation_version_resolution=unavailable plus reason; resolved requires the
actual version. It is never an invented placeholder or configured expected version.

invocation_origin is a discriminated union:

| Origin branch | Required fields / parent identity | Output/Fact invariants |
|---|---|---|
| GroundingBindingOrigin, kind=grounding_binding | node_id=N03; grounding_binding (ToolGroundingBinding) must equal pinned Profile binding and common tool_ref; scope=execution, profile_ref fixed, object_ref forbidden (Objects do not exist yet) | normalized grounding outputs/evidence feed GroundingRecord; generated_fact_refs must be empty; no ToolPlan step/declaration identity |
| ToolPlanStepOrigin, kind=tool_plan_step | node_id=N05; tool_plan_ref, step_id, producer_declaration_ids[]; pinned Profile.tool_plan_ref and step.tool_ref agree with common refs; V1 scope=object with object_ref | named outputs map via matching FactProducerDeclaration; failure/evidence-only step may yield no Facts; cannot claim GroundingRecord as automatic Tool Fact |

The owning node_run_ref must have the branch's node_id, and attempt_ref must
belong to that actual invocation under the same node. Retries have new identities;
tool_execution_id cannot identify both origins. N03 produces GroundingRecord and
Object refs; N05 independently executes its bound step, even when tool_ref is the
same as N03. N05 producer/Fact source must reference a tool_plan_step invocation;
no implicit cache/reuse/producer promotion of N03 localization. WP-01 models this
union and reference invariants only, not Tool execution or scheduling (F05).

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

CandidatePrediction contains category identifiers, not selected Rule identities.
RuleEvaluation stores outcome (evaluated/skipped/blocked), predicate truth
(true/false/unknown only when the condition was evaluated), required-input/evidence
completeness and reason separately. not_evaluated Rule refs are retained separately
from actually evaluated predicates. It pins ProfileEligibilityEvaluation and the
selected RuleSet/Rule applicability records via ApplicabilityTrace references.
Its Required Evidence Guard has outcome=complete/blocked, required_fact_refs,
required_evidence_refs, authoritative_result_selection_refs and typed reasons
(required_fact_unavailable/evidence_unavailable/producer_authority_unresolved).
Each selected producer must match the declared producer/schema/Execution/Object
scope and pinned authoritative Attempt revision. Missing, ambiguous or invalid
authority blocks a definitive result; last-write-wins is forbidden. Guard outcome
is not RuleTruth and does not decide applicability. Even a complete guard cannot
convert a condition UNKNOWN into no_violation. These guard fields belong to
RuleEvaluation, not to ApplicabilityPolicy or UnresolvedObligation metadata reasons.
Matched Rule references are retained as a set; storage order has no priority.
See 03 §8 for three-valued logic and collect_all aggregation.

### 4.8 ExecutionCoverage (D09)

Coverage is distinct from Task/Execution completion and from Decision.result.
ExecutionCoverage records schema_version, execution_id, pinned applicability scope,
discovery outcome (hits/no_hits/failed/partial/not_evaluated), per-Profile grounding
outcome (objects/no_target/failed/partial/not_evaluated), Object refs/counts,
expected/evaluated/blocked/unresolved mandatory Rule refs and missing evidence.
It includes coverage_status (complete/partial/none/not_applicable), reasons and
execution-scoped summary_result using DecisionResult, with matched Rule/Object refs.
All externally stored per-Profile coverage roots also carry schema_version.
It pins profile_eligibility_evaluation_ref when the Gate was evaluated, selected applicability evaluation refs,
applicability_trace_refs and unresolved_obligations. The obligation ledger retains
excluded/not_evaluated refs and decisive_ancestor_ref as well as existing
evaluated/blocked/unresolved refs. A not_evaluated child has no TruthValue; its
blocker remains the ancestor record, not a newly evaluated UNKNOWN. Parent
obligations and derived child entries must reconcile without double counting.
These records exist independently of per-Object Decisions and N06 execution.
If N01 failed before the Gate, profile_eligibility_evaluation_ref is absent and
the existing upstream lifecycle blocker/ref is required; no fake gate/trace.

Coverage measures execution over the configured scope, not empirical discovery
recall or proof of physical absence. Empty Objects, JEV no_hits, grounding no_target
or failure cannot establish no_violation. Successful subset results are preserved;
any missing required coverage prevents a global no_violation. A valid matched
Rule still establishes violation while coverage remains partial. Detailed cases
and aggregation constraints are in 03 §8.4.

## 5. Relationship cardinalities

- Entity 1:N Profile.
- Profile M:N Facet.
- Profile M:N Question, resolved to a deterministic effective set at Release compile time.
- Profile 1:N ToolPlan versions over time; a given Profile version pins one exact ToolPlan.
- RuleSet 1:N Rule references.
- Rule consumes N Facts; Fact has one authoritative producer record, though derived Facts may reference multiple inputs.
- Task 1:N Execution.
- Execution 1:N NodeRun; Execution 0:N Object, Fact, ToolExecution, ModelInvocation and Decision, plus one execution coverage summary (empty collections are valid data, not negative evidence).
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
- ContentRef includes kind, artifact_id, exact version and SHA-256 digest. SchemaRef is a constrained ContentRef. Legacy ArtifactRef means ContentRef, never an optional-digest reference.
- IdentitySelector includes kind, artifact_id and exact version, without digest/range/latest. R2 removes reverse Profile membership from Question/ToolPlan. Selectors remain only for explicitly declared applicability constraints over pinned Profile/Region metadata; they cannot execute content or establish membership. Bundle inventory resolves their targets exactly once.
- Profile owns ContentRefs to Entity, Facet, Question, ToolPlan and RuleSets. Reverse Question/ToolPlan membership is compiler-derived and not serialized in those artifacts (D15). Facets do not embed Question back-references; a published artifact does not embed its own digest or Bundle root digest.
- The forward ContentRef graph must be acyclic; selectors are not hashing edges. Complete semantic closure includes selector targets too: manifest inventory pins their content externally. Missing, ambiguous or kind-mismatched targets are hard errors. No unpinned content is executed and no selector is resolved against a global latest catalogue.
- Bundle freezes the transitive dependency closure.
- Execution pins one Release and Bundle digest.
- Runtime implementation versions are separately recorded from configured artifact versions.
- Deprecation prevents new activation/use according to policy but does not erase history.

## 8. Decision candidates awaiting approval

- Whether Region geometry is in this service or a spatial subsystem.
- D08 candidate: one exact ToolPlan per Profile version; declared conditions only over typed context/validated Facts, never N06 output.
- D08 candidate: Question/Facet reuse only with compatible exact output Schema, context allowlist and producer scopes for every referencing Profile.
- D02/D06 candidate: one primary Fact per Question, restricted Record values, non-known values absent, offline 2020-12 subset.
- D03/D04 candidate: strong Kleene three-valued predicates, required-input/evidence safety guard, mandatory full scan and collect_all matches (03 §8).
- D05/D09/D10 candidates: ContentRef/IdentitySelector split, explicit ExecutionCoverage, Profile-specific Object identity.

All candidates remain PROPOSED / PENDING_USER_APPROVAL; detailed D01–D15 are
tracked in planning/ARCHITECTURE_DECISIONS.md. Region geometry ownership remains
OPEN. Required structured policies cannot be replaced by arbitrary config maps.
Every externally persisted root has explicit schema_version and extra=forbid;
nested value records inherit the root's version unless stored independently.

## 9. ArtifactKind and reference catalogue (D11/D15)

ArtifactKind is one closed enum with exactly the following candidate values.
Every ContentRef requires one of these kinds, exact ID/version and digest. Bundle
inventory rejects duplicate kind/ID/version identities with different bytes.
New kinds require schema/capability version review; no arbitrary kind strings.

| ArtifactKind value | Class / authoritative contract | Bundle location |
|---|---|---|
| entity | Domain Definition: EntityDefinition | ontology/entities.json |
| region | Domain Definition: RegionDefinition | ontology/regions.json |
| profile | Domain Definition: ProfileDefinition | capabilities/profiles.json |
| facet | Domain Definition: FacetDefinition | capabilities/facets.json |
| question | Domain Definition: QuestionDefinition | capabilities/questions.json |
| tool | Domain Definition: ToolDefinition (not executable implementation) | tools/registry.json |
| tool_plan | Domain Definition: ToolPlanDefinition | tools/plans.json |
| rule | Domain Definition: RuleDefinition | rules/registry.json |
| rule_set | Domain Definition: RuleSetDefinition | rules/sets.json |
| model_binding | Domain Definition: ModelBinding | models/bindings.json |
| fact_schema | Domain Definition: FactSchemaDefinition, including type_definition | schemas/facts/definitions/*.json |
| prompt_template | Static config: PromptTemplateDefinition | prompts/templates/*.json |
| response_schema | Generated static response contract | prompts/response_contracts/*.schema.json |
| attribute_schema | Generated static attribute contract | schemas/attributes/*.schema.json |
| context_schema | Generated static context contract | schemas/context/*.schema.json |
| tool_input_schema | Generated normalized Tool input contract | schemas/tools/inputs/*.schema.json |
| tool_output_schema | Generated normalized Tool result contract | schemas/tools/outputs/*.schema.json |
| object_schema | Generated Object contract | schemas/objects/*.schema.json |
| decision_schema | Generated Decision contract | schemas/decisions/*.schema.json |
| value_schema | Generated payload contract from a bounded TypeDefinition | schemas/values/*.schema.json |
| policy | Static typed PolicyArtifact, §14 | policies/*.json |
| vocabulary | Static VocabularyDefinition | vocabularies/*.json |
| geometry_resource | Static GeometryResourceDefinition | resources/geometry/*.json |
| static_asset | StaticAssetDescriptor + checksummed non-executable bytes | resources/assets/* |
| fact_producer_catalog | Derived static ProducerCatalogue, §13 | facts/producers.json |

There is no separate discovery_capability or grounding_capability ArtifactKind in
the recommended candidate. Capability labels on ModelBinding/ToolDefinition are
registered enums, not ContentRef targets. BundleManifest/BundleRef, Release,
Execution, Object, Fact, Evidence and ModelInvocation are separate package/runtime
contracts, not ArtifactKinds. Their refs use BundleRef or RuntimeRecordRef/EvidenceRef,
not ContentRef. A schema JSON file is an artifact; an instance conforming to that
schema is a distinct definition/runtime object.

All definition/config relationships above use ContentRef (with expected kind
checked per field). SchemaRef accepts only fact_schema, value_schema,
response_schema, attribute_schema, context_schema, tool_input_schema,
tool_output_schema, object_schema or decision_schema. fact_schema refers to the
registry definition; its payload_schema_ref must target value_schema, generated
from its type_definition without embedding the parent definition's digest.
Generated schema `$id` is versioned identity, resolved offline, never its own hash.

IdentitySelector is permitted only inside explicit ApplicabilityPolicy identity
constraints (kind=profile/region); it is not a substitute for an unresolved
ContentRef. Profile membership in Question/ToolPlan is forward-authoritative;
compiler-derived reverse indexes are rebuildable reports, not new artifacts.
Profile version changes do not require re-versioning an unchanged Question/ToolPlan.

External Runtime capabilities are separately typed ExternalCapabilityRequirement
records: family=model_deployment/tool_implementation/system_fact_producer/context_derivation,
capability_id, contract_version, allowed_implementation_versions (bounded nonempty
exact versions), required_features (closed feature enum: rgb_image/typed_context/structured_output/original_xyxy/mask_output/geometry_output/ocr_text), optional evaluation_ref
(immutable reviewed-report reference). These are capability requirements, not
executable artifact refs. Model deployment endpoints, secret handles, signing keys,
loaded weights and registered Tool code come from trusted host configuration.
Bundle contains their requirements, never credentials, arbitrary URLs or code.
Task media, Device/Camera registry snapshots and actual invocation records are
versioned/provenance-bearing Runtime inputs. No missing config is fetched upstream.

ReviewReportRef is a closed external audit reference: report_id, exact version,
sha256 and trusted review_registry_id, not an arbitrary URL/ContentRef with an
undefined ArtifactKind. Admission resolves approved reports from trusted Runtime
configuration; reference metadata is pinned in the Bundle. Evaluation report
bytes are provenance outside the configuration closure, not a missing template.

Static resource models are closed: VocabularyDefinition has ID/version,
semantic_domain and unique bounded members; GeometryResourceDefinition has
ID/version, coordinate_system, units, bounded typed geometry and provenance;
StaticAssetDescriptor has ID/version, normalized package-relative data_path,
sha256, MIME type, byte_size and optional dimensions. Region.asset_refs targets
static_asset (including bundled ledger snapshots); fresh registry metadata is an
N01 Runtime input with provenance, not an unpinned static asset reference.
Static asset inventory pins the descriptor JSON; its declared data_path/SHA-256
covers associated raw bytes under resources/assets/data/, also in checksums.
Those bytes are declared resource members, not extra artifact identities or orphan
payloads. JSON/schema resources and definition files remain canonical artifacts.

## 10. Capability bindings, PromptTemplate and grounding provenance (D11)

ModelBinding.capability candidate enum: profile_discovery, object_grounding,
object_observation, candidate_prediction, vision_auxiliary. Its declared IO schemas
and modality features must match the consuming node, not just a matching label.
Required Features include rgb_image, typed_context, structured_output, original_xyxy
where applicable; unsupported required features reject admission later in WP-02.

- Profile.discovery_binding_ref directly targets ModelBinding(profile_discovery).
  N02 accepts a pinned Profile catalogue/context and produces registered hits plus
  Question selection proposals only. No new capability registry artifact is needed.
- Profile.grounding_binding is discriminated: ModelGroundingBinding has kind=model
  and model_binding_ref (object_grounding); ToolGroundingBinding has kind=tool and
  tool_ref (ToolDefinition whose capability_tags includes object_grounding). Both
  require original_xyxy compatible output and may reference typed input mappings.
  ToolDefinition declares implementation_requirement (ExternalCapabilityRequirement)
  and optional model_binding_ref (vision_auxiliary), if its tool implementation
  explicitly invokes a model. That binding is not silently substituted.
- Profile.observation_binding_ref is ModelBinding(object_observation), so N04 has
  an explicit source of ModelBinding/response contract. RuleSet.candidate_binding_ref
  is optional ModelBinding(candidate_prediction). Advisory absence/failure cannot
  change mandatory Rules. No binding object performs scheduling in WP-01.

PromptTemplateDefinition has schema_version, prompt_template_id/version,
purpose (profile_discovery/object_grounding/object_observation/candidate_prediction),
template_text (bounded literal with registered named placeholders only),
variables[] (PromptVariable: name, source, field_path, schema_ref, required, on_missing=block/omit),
response_schema_ref (ContentRef[response_schema]), optional partial_refs
(ContentRef[prompt_template], acyclic) and rendering_policy=literal_substitution.
Variable source enum: context/profile_metadata/question_metadata/object_geometry/
image_reference. Each path resolves in the declared Schema and relevant allowlist;
Question templates cannot access another Question's unapproved context.
required=true implies on_missing=block; required=false requires explicit
on_missing=omit (remove that placeholder, do not synthesize a value). Disallowed
missing required variables block rendering, optional missing variables are omitted
by explicit placeholder contract, never guessed. No executable expressions,
include-from-URL, dynamic imports or secret variables. V1 renderer supports named
placeholder substitution only, no loops/conditionals. Renderers are later code.
Question.prompt_template_ref targets purpose=object_observation, with output
contract compatible with its one primary Fact. JointBatch response_schema composes
selected Question contracts indexed by exact Question identities, not a model's
free-form association. Generated schemas/prompt resources are pinned in Bundle.
For N04, ModelBinding.output_schema_ref pins the common batch envelope, and
Question templates pin per-Question component contracts. The exact selected-set
response schema is derived by the versioned compiler from these authoritative
contracts, not fetched or hand-written. ModelInvocation records a typed
InvocationContractSnapshot: configured_output_schema_ref, effective_question_refs,
compiler_version, response_schema_sha256 and response_schema_evidence_ref. The
derived schema bytes are part of model_request evidence, not a new unregistered
Bundle ArtifactKind. This records the exact request constraint without packaging
every optional-subset combination. WP-01 defines the snapshot; runtime prompt/schema
composition is later work, not a model call in the domain.

ToolDefinition.tool_type is grounding/segmentation/geometry/ocr/evidence_transform;
provider_kind is builtin/local_model/remote_service. These refer to trusted
implementations, not Bundle-supplied executable providers.
Tool capability_tags is a closed enum set: object_grounding/segmentation/geometry/ocr/evidence_transform. A tag declares compatibility, not executable code.

GroundingRecord has schema_version, grounding_record_id, execution_id, profile_ref,
node_run_id/attempt_id, grounding_binding snapshot, question_selection_ref,
effective_question_refs, source_media_ref, coordinate_system, transforms[],
outcome (objects/no_target/failed/partial), Object refs, evidence_refs and producer.
Producer is a discriminated union, not a mandatory model_invocation_id:

| Branch | Required provenance | Optional provenance / invariant |
|---|---|---|
| ModelGroundingProvenance, kind=model | model_binding_ref, model_invocation_ref | invocation records actual deployment/model; tool_execution_ref prohibited |
| ToolGroundingProvenance, kind=tool | tool_ref, tool_execution_ref, actual_implementation_version | nested model_invocation_refs allowed only when Tool declared model use; not a fake direct model producer |

Failure/no_target records retain the applicable branch. Tool-based N03 localization
is not an N05 Fact producer by implication: N05 executes the pre-bound ToolPlan
independently, with separate step identities and audit even if the same Tool is used.

## 11. Effective Question Set and QuestionSelectionPolicy (D12)

Profile.question_refs is the authoritative ordered catalogue. Its referenced
QuestionSelectionPolicy contains mandatory_question_refs and optional_question_refs:
disjoint exact refs, union equal to Profile.question_refs. Mandatory/optional is a
Profile-specific role, not a mutable property of a reusable Question.

| Mode | Benefit | Failure risk / constraint |
|---|---|---|
| A: all_declared | Simplest deterministic Stage1 Profile→Question-set output; no selection omission | Larger N04 prompt/schema; still one call per Object, no measured cost claim |
| B: mandatory_plus_optional | Stage1 generates a smaller configured set; may reduce N04 input/schema complexity | Optional omission can reduce coverage/quality; all required downstream producers must be mandatory; selection needs audit/fallback |

Recommended candidate: contract supports both; initial accepted Profile must
explicitly choose all_declared (conservative starting recommendation) or
mandatory_plus_optional. No mode default. B allows optional_source=all/context_rules/
model_advisory under explicit policy. Model may only propose optional registered
Questions; system always includes mandatory Questions. Selecting a nonregistered
Question, missing trustworthy association or invalid selection invokes configured
fallback=include_all_optional or block_profile; recommendation include_all_optional.
Model omission of a mandatory Question is recorded as a rejected proposal and
repaired by deterministic mandatory union, never accepted as its removal.

QuestionSelectionRecord is immutable per N02 Attempt/Profile: schema_version,
selection_id, execution_id, profile_ref, policy_ref, configured_question_refs,
mandatory_question_refs, proposed_optional_question_refs, selected_optional_question_refs,
effective_question_refs, excluded_question_reasons[], method
(all_declared/context_rules/model_advisory/fallback), context_snapshot_ref/digest,
node_run_id/attempt_id, discovery_model_invocation_ref, conditionally required
model_invocation_ref (see F03 below), structured validation_issues,
fallback_applied and created_at. Reasons are bounded model-reported metadata,
not proof of truth or hidden reasoning. Context rules have exact policy refs;
model proposals reference the actual N02 invocation (shared by explicit policy),
not unverifiable confidence alone. Ordered effective set follows Profile catalogue
order, has no duplicates and is nonempty for a hit Profile. ProfileHit stores
question_selection_ref + effective_question_refs, and any repeated set must match.

N03 copies the selection ref and exact set to per-Profile result/Object; no
reselection by localization or visibility. N04 validates the set against the
selection record and issues one joint normal-path request per Object. A retry is
whole-batch under the same frozen selection (new Attempt/Invocation), not per-Question
follow-up or silent optional expansion. Selection retries in N02 create new records;
downstream work pins one authoritative N02 selection, never the newest by timestamp.

Static completeness: Questions producing required Facts for any potentially
applicable mandatory Rule or required Tool step must be mandatory under every
selection mode. Producer dependencies are transitive; optional Question omission
cannot suppress mandatory Rule applicability or required Tool execution. Omitted
optional Questions produce no fabricated not_applicable Facts; selection trace
explains non-evaluation. Optional-dependent advisory Rules may be blocked/unknown
without converting their omitted Facts to false. Unknown context eligibility
cannot demote a mandatory Question.

### 11.1 F03 — selection evidence branches and explicit extra calls

QuestionSelectionRecord is discriminated by method; common fields above remain.
discovery_model_invocation_ref identifies the actual N02 discovery invocation
supporting the trustworthy ProfileHit, not a fictitious selection call. A selection
record cannot invent a hit after complete discovery transport/association failure.
That failure remains an N02 Attempt diagnostic without a fabricated ProfileHit.

| method | Selection-call fields | Required evidence / constraints |
|---|---|---|
| all_declared | model_invocation_ref and selection_invocation_mode forbidden | configured complete set + pinned policy/context; fallback_applied=false; discovery still has its own real audit |
| context_rules | model_invocation_ref and selection_invocation_mode forbidden | declared context-rule refs and deterministic reasons; fallback_applied=false; no selection model call |
| model_advisory | model_invocation_ref and selection_invocation_mode required | actual ModelInvocation with optional_question_proposal purpose, same execution/N02/Profile proposal association, actual Attempt/provider audit; fallback_applied=false |
| fallback | model_invocation_ref and selection_invocation_mode forbidden; fallback_evidence required, fallback_applied=true | triggered_method, trigger_attempt_ref, fallback_reason, diagnostic_refs and dispatched (bool); trigger_invocation_ref required iff dispatched, otherwise forbidden; never label a fallback result as an accepted model proposal |

FallbackSelectionEvidence.triggered_method is context_rules/model_advisory;
fallback_reason is context_unknown/selection_review_unavailable/invalid_proposal/
selection_transport_failure/additional_budget_unavailable. The actual failed
operation/Attempt or failed selection-validation subresult is preserved. A partial
selection validation failure does not rewrite a successful transport Attempt as
failed. If a shared N02 response has trustworthy ProfileHit association but invalid
Question proposals, fallback may include all optional Questions under the pinned
policy. An entire invalid discovery envelope cannot be repaired into a hit.
Fallback retains configured call policy/mode, the failed proposal/diagnostics and
actual invocation ref when one exists; it cannot reference the successful discovery
call as the trigger of an undispatched failed context/extra-call operation.

QuestionSelectionPolicy.selection_call_policy is required only when
optional_source=model_advisory; it is otherwise forbidden. SelectionCallPolicy is
the union of SharedN02SelectionCallPolicy and AdditionalSelectionCallPolicy:
- mode=shared_n02: no additional-call fields. Recommend this mode; both Profile
  discovery and optional proposals come from the same N02 ModelInvocation.
  model_invocation_ref must equal discovery_model_invocation_ref and that invocation
  records both purposes (profile_discovery, optional_question_proposal).
- mode=explicit_additional: requires proposal_binding_ref (ContentRef[model_binding],
  profile_discovery-compatible proposal contract), additional_resource_requirements_ref
  (ContentRef[policy], ResourceRequirements) and positive bounded
  max_additional_invocations_per_execution (including retries). It requires explicit
  configuration approval, not an implicit repair call. Each actual additional call
  has a distinct invocation ID, purpose=optional_question_proposal, its own Attempt,
  provider/version/request evidence and a link to the supporting discovery invocation.
  A different mode/ref/budget or unregistered additional call is invalid.

The named union branches are AllDeclaredSelectionRecord, ContextRuleSelectionRecord,
ModelAdvisorySelectionRecord and FallbackSelectionRecord. ModelAdvisorySelectionRecord
fields (the method=model_advisory QuestionSelectionRecord
branch) record selection_invocation_mode=shared_n02/explicit_additional matching
the pinned policy. No automatic extra call is permitted for all_declared,
context_rules or fallback. Exhausted/absent additional budget takes the configured
conservative fallback/block path; it does not silently spend another request.
N03/N04 inherit the same effective set; one Object still has one normal joint N04
request. WP-01 defines this union and reference validation, not an N02 call engine.

ModelInvocation.purposes is a nonempty closed set of profile_discovery/
optional_question_proposal/object_grounding/object_observation/candidate_prediction/
vision_auxiliary, constrained by its ModelBinding capability. An explicit additional
proposal invocation requires source_discovery_invocation_ref (distinct real ID);
a shared invocation has both purposes and no self-referencing source field.
ProfileHit.invocation_ref agrees with discovery_model_invocation_ref; proposal and
fallback references are validated against actual recorded execution/node/Attempt
associations, not merely syntactically valid IDs. Finite fixtures do not prove a
real provider call occurred.

## 12. Facet–Schema and reuse checks (D13)

Keep Facet.fact_schema_refs: it gives a reusable semantic allowlist, not duplicate
Question output configuration. FactSchemaDefinition requires semantic_key, scope,
units/coordinate_system when relevant, type_definition, payload_schema_ref and
allowed_producer_kinds. Facet links do not create Facts or change these properties.

Static rules: (1) Question has at least one Facet; (2) its primary_fact_schema_ref
is an exact member of each Facet's declared allowlist (intersection rule);
(3) shared Schema may belong to many Facets with the same semantic_key/units/scope;
(4) every Profile referencing the Question includes its Facets and satisfies its
context_allowlist/input schemas; (5) no Profile changes the Question's fact_key,
Schema, meaning, unit or producer scope; semantic differences require a new
Question/FactSchema version or identity; (6) producer and FactSelector resolve the
same exact Schema, including Record field paths. Shape equality is insufficient.
WP-01 checks finite injected definitions; full Bundle graph checking is WP-02.

### 12.1 F04 — independent TypeDefinition projection to value_schema

FactSchemaDefinition adds required value_schema_generation
(ValueSchemaGenerationSpec): projection_profile_version, generator_id,
generator_version, schema_dialect=2020-12, canonicalization_id=JCS and global_limits
(GlobalTypeLimits: string_max_length, list_max_items, record_max_fields, max_depth,
all explicit positive bounded integers within approved contract limits). It is a
closed data record, not executable code.
Only the registered projection profile is accepted. No environment-global mutable
limits, parent identity/digest or installed-library-default options are inputs.

TypeDefinitionProjection is the following normalized, finite type tree:
- Keep only kind and payload constraints: bool; integer/finite-number bounds;
  string min_length/max_length; enum unique string members; list item type and
  min_items/max_items; Record fields (name, required, recursively projected type).
  Payload `kind` tags are part of this projection and agree with typed Value models.
  Numeric bounds are inclusive minimum/maximum when supplied; they must preserve
  exact meaning under canonical serialization or reject, never round silently.
- Fill omitted structural size lower bounds (string/list only) with 0 and omitted upper size bounds with
  the explicit global_limits; do not invent business enum members or values.
  Record field required flags and item/field types must be explicit, not inferred.
  Duplicate fields/members, nonfinite numbers, inconsistent/over-limit constraints,
  recursive types, unsupported keys or arbitrary schema/code refs reject input.
- Sort Record fields by UTF-8 byte order of exact field names and enum members
  by UTF-8 byte order of their exact strings (no locale-dependent sorting);
  nested bounds normalize identically. Record fields are closed (extra forbidden).
  No arbitrary Unicode/text rewriting or unit conversion is performed. Semantic
  enum identity remains a Registry/Fact-envelope check, not a parent-digest literal
  inside the generated payload Schema.
- Exclude parent schema_version/schema_id/version, semantic_key, producer/scope/policy metadata,
  payload_schema_ref, timestamps, parent ContentRef/digest and Bundle digest.
  They are not payload type constraints. If a value-level unit/dimension is needed,
  it must be an explicitly declared payload Record field, not an ambient input.

ValueSchemaGenerationInput contains only normalized TypeDefinitionProjection plus
ValueSchemaGenerationSpec. For fixed generation spec, semantically identical
normalized trees must emit identical Schema content and canonical UTF-8 bytes,
regardless of Record/member source order or parent definition metadata. Generator
version/profile/limits are explicit inputs; changing them requires compatibility
analysis and may change the output. No current implementation is asserted.

Generation mapping is data, not a hand-maintained parallel Schema: each type emits
a closed object with required kind/value, kind.const fixed to the registered Value
tag; value is boolean/integer/number/bounded string/closed enum/array/closed Record
as appropriate. Record properties recursively contain the projected typed Value
schemas, required names follow explicit flags, and all extra fields are forbidden.
Finite JSON parsing and constraints remain mandatory; cross-Fact identity and
status/scope/producer rules stay outside this payload Schema. Nested types are
inlined deterministically; no parent `$ref` is emitted.

The projection contract fixes `$schema` to 2020-12. If `$id` is emitted, it is
`urn:jimu:value-schema:<projection_profile_version>:<input_sha256>` where input_sha256
is over canonical ValueSchemaGenerationInput, not the generated Schema's own hash.
The profile fixes this emission mode (V1 emits that `$id`); no random titles,
timestamps, path-dependent definitions or parent name/version/digest are injected.

Required dependency order:
1. Normalize the type tree and form independent generation input.
2. Generate canonical value_schema bytes; compute its independent SHA-256 under
   the artifact digest contract, and assign its versioned inventory identity from
   the generation input (not the parent).
3. Set FactSchemaDefinition.payload_schema_ref to that exact ContentRef[value_schema].
4. Only then canonicalize/hash the complete FactSchemaDefinition. Neither child
   bytes nor its generation input contains the parent's resulting digest.

Multiple parent definitions with the same normalized type and generation spec can
share one value_schema; semantic meaning/scope/producer compatibility is still
checked by exact FactSchema refs, never replaced by shape equality. Bundle compiler
and admission must reject a supplied payload_schema_ref whose bytes do not match
this derivation. WP-01 defines projection/generation models, generated payload
Schema content and repeatability/negative CPU contract tests after authorization;
it does not implement canonical Bundle packaging/root hashing/signatures or graph
admission. Those complete artifact/Bundle digest operations remain WP-02.

## 13. Fact producer, output mapping and authority contracts

### 13.1 FactProducerDeclaration

Derived ProducerCatalogue (schema_version, catalogue_id/version, declarations[])
is a manifest-rooted artifact, never referenced back from Profile/Question/Rule.
Each declaration has declaration_id (unique in catalogue), profile_ref for
object-scoped production, scope (object/execution), producer_kind
(vlm_question/tool/system), producer_id, producer_version, producer (typed union
below), output_name, fact_key, fact_schema_ref, cardinality, and activation_condition
(always or typed Tool condition). Optional description is bounded; no arbitrary map.
Producer ID/version is definition identity, never an Attempt ID or the actual
provider implementation version. Catalogue generation is WP-02; its model and
finite in-memory checks are WP-01.

| Producer branch | Identity fields and cardinality |
|---|---|
| QuestionProducer | question_ref (ContentRef[question]); producer_id/version must equal that ref; output_name=primary; cardinality=exactly_one per eligible Object + accepted N04 Attempt; Schema/key match Question |
| ToolStepProducer | tool_plan_ref, step_id, tool_ref; producer_id/version equal ToolDefinition ref, step identity includes pinned ToolPlan; output_name matches declared Tool output; cardinality=zero_or_one or exactly_one per step Attempt/output, with reason for optional absence |
| SystemProducer | requirement (ExternalCapabilityRequirement family=system_fact_producer); producer_id/version equal registered capability identity/contract version; cardinality explicitly zero_or_one/exactly_one; emitted key/schema are fixed declarations |

A Tool step may declare multiple outputs with distinct output_names/Fact keys.
V1 Profile-bound N05 ToolPlans only bind object-scoped Fact outputs. Execution-
scoped Facts use registered system producers in this candidate, avoiding per-Object
fanout racing over one execution Fact key. ToolExecution keeps its execution scope
for grounding/evidence audit; broader execution-scoped Tool Fact production needs
an explicit separately approved scheduling contract, not an implicit last writer.
ToolDefinition.fact_outputs[] contains ToolFactOutputDeclaration: output_name,
fact_key, fact_schema_ref, scope, required, value_path (bounded tuple of declared
Record field names), optional evidence_paths[] and confidence_path. ToolStep has
fact_output_mappings[] (ToolFactOutputMapping: output_name, target_fact_key)
selecting declared outputs and explicit target Fact key;
V1 target key must equal the Tool declaration (no runtime alias). Output paths
are statically resolved against Tool.output_schema_ref. Tools return a normalized
ToolResult envelope (status=succeeded/failed/partial, typed payload only when
available, structured diagnostics, evidence refs, output_states[]); adapters cannot expose arbitrary
vendor response shapes as Facts. Each mapped payload must pass its own Fact Schema.
value_path is relative to ToolResult.payload (statically unwrap succeeded/partial
payload schema), not arbitrary response metadata; evidence_paths address the
explicit evidence section. ToolOutputState entries have output_name, fact_status (the existing five-state
FactStatus), reason for non-known and evidence_refs. A mapped output must have one
unique state; known requires its validated value_path, non-known forbids a value
at that path. Explicit unknown is retained as unknown; missing state/path is a
validation error, not inferred unknown/false. Whole transport failure preserves
ToolExecution failure without inventing successful named outputs.
Missing/invalid required output yields an error result/reason; valid sibling outputs
may survive only with trustworthy envelope/association. Overall Tool failure never
creates known false/empty facts. Unknown/failed output uses ToolFailurePolicy, not
truthiness. Raw Tool JSON is evidence or normalized IO, never an unconstrained Fact.

### 13.2 Uniqueness and authoritative result selection

- fact_id identifies an immutable candidate. V1 compile-time collision domain is
  (profile_ref, object scope, fact_key), or (execution scope, fact_key) across all
  Profiles. Two declarations in one collision domain reject even if their Schema
  matches; producers cannot race to own the same key. Different Profiles may share
  an object-scoped key but never share Object/Fact identity (D10).
- Candidate uniqueness is (execution_id, scope, object_id when object-scoped,
  producer identity including step/output when applicable, fact_key, attempt_id).
  Duplicate delivery of identical candidate bytes is deduplicated; different bytes
  for the same key/Attempt reject and preserve a conflict event, never overwrite.
- Fact stores producer_declaration_id + pinned producer catalogue ref, source
  question_ref or ToolExecution ref, producer_id/version, attempt_id and actual
  Invocation/implementation provenance separately. All refs/schema/key must agree.
- AuthoritativeResultSelection is append-only: schema_version, selection_id,
  execution_id, scope/Object ref, selection_group (observation_batch or tool_step
  or system_output), group identity, selected_attempt_id, selected_result_ref,
  selected_fact_refs, reason (validated_success/explicit_retry_resolution/manual_review),
  authority_revision, expected_previous_selection_ref (optional for first), actor_ref,
  created_at. New revisions explicitly supersede selections, never old Facts.
- Observation authority selects an entire trustworthy Question batch for one
  Object, not best-value cherry picking across Attempts. Tool authority selects
  a single step Attempt with its mapped outputs. N06 pins selection revisions in
  its input digest; a late result cannot change an existing Decision. A refreshed
  score/run must be explicit. Missing/ambiguous authority blocks consumers; the
  latest write, finish time or highest confidence is never the authority rule.
- Persistence CAS/fencing/idempotency implementation belongs to WP-03, not WP-01.

ActorRef is a closed trusted identity reference: actor_kind=service/human,
actor_id and identity_registry_id. Model output cannot select authority as an actor.
No credential or executable lookup URL is included.

### 13.3 FactSelector and AST input domains

FactSelector contains scope, fact_key, fact_schema_ref, producer_selector and
field_path (empty = whole primary/mapped value). ProducerSelector is typed:
QuestionProducerSelector has question_ref + output_name=primary;
ToolProducerSelector has tool_plan_ref + step_id + tool_ref + output_name;
SystemProducerSelector has registered requirement identity. Object scope implicitly
uses the current Object/Profile; cross-Object/cross-Profile joins are not V1.
The selector does not embed runtime fact_id/attempt_id or reference the derived
producer catalogue, avoiding a Profile→Rule→catalogue→Profile digest cycle.
At compile/load, selectors match exactly one declaration; at execution they resolve
through the explicitly pinned AuthoritativeResultSelection. RecordFieldRefExpr
uses this same field_path, not a second independent path convention.

AST checking receives an explicit input_domain:
rule_applicability permits declared context/Region/Profile metadata only;
rule_condition permits validated FactSelectors (including declared Record paths)
and declared context; tool_condition permits declared context + validated Facts
available before that step. Each root is Boolean lifted to T/F/U inside its AST.
Ordered applicability gates are separate from that expression logic; the Required
Evidence Guard independently protects definitive Rule results with validated
Facts/Evidence/Producer Authority, not a generic AST or metadata gate. No input
domain includes N06 Candidate/Decision;
applicability cannot depend on Object Facts. Conditional producer availability is
checked conservatively for mandatory consumers: unsupported/unproved completeness
rejects the Bundle, rather than hiding obligations with the same condition.

### 13.4 Approved WP-01B local Tool Fact selector amendment

A ToolPlan-attached ToolSkipPolicy must not embed a same-Plan digest back-reference
through ToolProducerSelector: ToolPlan -> Policy -> same ToolPlan would create a
content-digest cycle. The user explicitly approved the following local selector
amendment during GOAL-002 (WP-01B execution_03); existing exact selectors are retained.

LocalToolFactSelector contains explicit scope=object, step_id, tool_ref (exact tool
ContentRef), output_name, fact_key, fact_schema_ref (exact Fact Schema ContentRef),
and the existing bounded field_path. LocalToolFactRefExpr has kind=local_tool_fact_ref
and this selector. Only tool_condition may contain this node. It carries no
ToolPlan/Profile/Object/Execution/Attempt reference or digest placeholder. It is not
an IdentitySelector or an independently verified content dependency.

Each use binds the enclosing pinned ToolPlan and actual Profile/current Object.
The selected step must be a strict ancestor, with exact Tool, mapped output, key,
Schema and field path. Self/future/sibling/cross-Plan/unmapped references reject.
Policy reuse requires independent binding validation at every use. The resolved
exact Plan identity may enter a temporary selector for Producer matching, but must
never be serialized back into Policy bytes. Question/System conditional Producers
without a ToolPlan step context cannot use this local node. Existing exact
ToolProducerSelector remains usable in acyclic contexts, including Rule conditions
and derived manifest-rooted catalogues. No cross-Object Fact reuse is introduced.

This amendment changes condition data only. WP-01B checks shapes, references and
finite associations; operator TypeChecker/evaluator and Bundle compilation remain
out of scope. No new ArtifactKind/PolicyKind or digest exclusion rule is introduced.

## 14. Typed Policy Catalog (R2-04)

PolicyArtifact has schema_version, policy_id/version and policy (discriminated by
policy_kind). Only the following registered shapes are allowed. All field names
below are normative candidate names. Referencing field names end in `_policy_ref`
or `_requirements_ref` and target ContentRef[policy] of the expected subtype;
Schema/Context selectors and ASTs are typed definitions from this spec.

policy_kind values are exactly applicability, question_selection, unknown,
question_validation, evidence, tool_execution, tool_failure, tool_skip,
resource_requirements, provider, fallback, rule, conflict, coverage,
context_provenance and execution, respectively for the rows below.
DeploymentRequirement.actual_version_constraints is a closed VersionConstraint:
allowed_deployment_versions[] and allowed_model_versions[], bounded nonempty
exact versions; logical_model_id must agree with ModelBinding. Evaluation refs
use ReviewReportRef. ContextFieldSelector is source + declared field_path + exact
context Schema ref. Applicability identity constraints are OR within one kind and
AND across profile/region kinds, then AND with condition_ast; empty constraints
do not remove context requirements. No implicit selector wildcard/version range.

Evidence type enum: original_image/crop/bbox_visualization/mask/tool_output/
geometry/model_request/model_response. retention_class is a VocabularyMemberRef
(ContentRef[vocabulary] + member), not an arbitrary policy object.
ContextFieldRequirement includes derivation_requirement
(ExternalCapabilityRequirement family=context_derivation) and input_selectors[]
iff declared_derivation is an allowed source; otherwise these fields are absent.
Derived inputs must be declared/trusted, versioned and acyclic. Selecting sources
or running a derivation is later Runtime behavior, not WP-01 implementation.

**Common rules:** no value/behavior defaults; all required fields must be explicit.
Branch-only fields are forbidden outside their stated branch.
Optional fields may be absent only in the listed branch, with that absence meaning
documented here. Every numeric budget is bounded and explicitly configured, not a
measured-performance assertion. Budget scalars are explicit positive 64-bit
integers (1..2^63-1), clock_tolerance_ms is 0..2^63-1; tighter global list/depth
bounds remain in the plan. No dict[str, Any], opaque object, executable hook
or implicit default strategy. Unsupported variants fail closed. Domain contracts
describe effects; WP-01 does not implement execution.

When multiple declared budgets apply, the effective limit is their minimum;
ToolExecutionPolicy may only tighten its ResourceRequirements. Conflicting or
unsatisfiable limits reject configuration, not silently widen limits. Required
evidence/Questions/producer obligations combine by union, never by a looser policy.

| Policy / scope / purpose | Required fields and enum values | Optional fields / default rule | Validation and execution effect |
|---|---|---|---|
| ApplicabilityPolicy; Profile/Rule/RuleSet | input_domain=rule_applicability; context_allowlist[]; identity_constraints[] (IdentitySelector[profile/region]); condition_ast; on_unknown=block | description only; no default. Empty identity_constraints means unrestricted identity within declared scope, not absent policy | Metadata only, no Facts/N02 Candidates/N06 Decisions. Profile owned by Pre-N02 Workflow Gate; RuleSet/Rule by N06. Ordered levels, not flattened AND; inside one AST use Strong Kleene. FALSE proves subtree exclusion; UNKNOWN unresolved, descendants not_evaluated without truth (3.8.1) |
| QuestionSelectionPolicy; Profile/N02 | mode=all_declared/mandatory_plus_optional; mandatory_question_refs[]; optional_question_refs[]; optional_source=all/context_rules/model_advisory; fallback=include_all_optional/block_profile; ordering=profile_order | context_selection_rules[] required iff context_rules (question_ref + ApplicabilityPolicy ref); proposal_schema_ref (response_schema), selection_review_ref (ReviewReportRef) and selection_call_policy (F03) required iff model_advisory; no default mode | Disjoint exhaustive partition; all_declared requires optional_source=all; required producer Questions mandatory. Invalid/uncertain optional selection or unavailable/inapplicable independent selection review applies fallback, never drops mandatory |
| UnknownPolicy; Question Fact | insufficient_visibility=unknown; transport_failure=no_fact; envelope_failure=no_fact; item_validation_failure=error; non_known_value=forbidden; reason_required=true | none; no defaults | Preserve distinctions and original diagnostics, no implicit false/null known value |
| QuestionValidationPolicy; Question/N04 | coercion=forbid; extra_fields=forbid; association=exact_question_ref; missing_item=error; invalid_record=whole_fact_error; sibling_retention=trusted_envelope_only | max_string_chars/max_items may tighten global bounds; no implicit widening | Validate each primary Fact independently only if association trustworthy; cannot invent per-Question follow-up calls |
| EvidencePolicy; Question/Tool/Rule | required_evidence_types[]; allowed_evidence_types[]; on_missing=block_result; original_media_required (bool); retention_class; retention_duration_seconds; access_scope=project/task | transform_required_for_derived=true required when derived types allowed; no default class/required types | Required subset allowed; hash/MIME/scope/transform provenance required; unavailable mandatory evidence yields uncertain, never negative evidence. Retention duration/inspection-specific evidence set OPEN until business config approved |
| ToolExecutionPolicy; Tool invocation | timeout_ms; max_attempts; max_concurrency; input_byte_limit; retryable_error_classes[] (transport/timeout/rate_limit/resource_exhausted); whole_step_retry=true | retry_backoff_ms[] required iff max_attempts>1; no default budgets | Positive bounded budgets; per-step retries preserve old attempts; no retry on validation unless explicitly allowed in future schema |
| ToolFailurePolicy; ToolPlan step | requirement=required/optional; required_descendants=block; fact_failure=error; optional_continuation=allow_if_consumer_policy/block | none; no defaults | Required evidence absence remains explicit; optional continuation cannot produce definitive mandatory result with missing required inputs |
| ToolSkipPolicy; ToolPlan step | condition_ast (tool_condition) or explicit literal true; on_false=skip_with_reason; on_unknown=block; on_missing_input=block | none; no defaults | Context + declared validated upstream Facts only; no Candidate/Decision; skipping required producer cannot erase mandatory coverage |
| ResourceRequirements; model/tool/execution | resource_class=cpu/remote/gpu; max_concurrency; timeout_ms; input_byte_limit; output_byte_limit | gpu_memory_limit_bytes + worker_pool_id required iff gpu; remote_quota_id required iff remote; no guessed capacity | Strict positive limits; deployment admission must validate values, not assume RTX4090 capacity; CPU core does not load workers |
| ProviderPolicy; ModelBinding | routing_mode=remote_primary/local_primary_exception; primary_deployment_id; deployments[] (nonempty DeploymentRequirement: provider_id, deployment_id, provider_kind=remote/local, actual_version_constraints, required_features[]); selection=primary_then_explicit_fallback | local_primary_exception (LocalPrimaryException) required iff local mode, otherwise forbidden; no default route/URL | Remote primary required in remote mode; all other entries fallback-only. Local primary needs reviewed capability-scoped exception. Timeout/order never authorizes switching; exact compatible evaluated transition required (F02) |
| FallbackPolicy; ModelBinding | mode=disabled/explicit_evaluated; trigger_errors[] (transport/timeout/rate_limit/unavailable); allowed_transitions[] (from/to deployment IDs); contract_compatibility=exact | evaluation_refs[] required iff explicit_evaluated; disabled requires empty triggers/transitions; no default fallback | All endpoints declared in ProviderPolicy; each transition independently reviewed/evaluated; no unreviewed model substitution or Fact coercion |
| RulePolicy; Rule | mandatory (bool); missing=unknown; unknown=unknown; error=unknown; not_applicable=unknown; not_evaluated=unknown; required_guard=block_unavailable; scan=all_mandatory; true_result=violation; false_result=no_violation; unknown_result=uncertain | none; no default mandatory flag | Applicability gates, AST truth and Required Evidence Guard are separate contracts. Definitive Rule result requires complete Facts/Evidence/Producer Authority; UNKNOWN condition is uncertain, never no_violation |
| ConflictPolicy; RuleSet | mode=collect_all; early_exit=false; category_dedup=set; preserve_rule_refs=true | none; no default | Keep all valid matches/provenance. Exclusive/priority winner modes unsupported V1 |
| CoveragePolicy; Release/Execution | no_hits=uncertain; no_target=uncertain; partial_without_match=uncertain; partial_with_match=violation; complete_negative=all_mandatory_false_with_required_evidence; absence_strategy=conservative | none; reviewed_protocol_ref forbidden in V1; alternate strategy requires a new approved version | Maintains D09; no Object/recall confidence alone permits auto-clear. B/C alternatives remain D14 pending evaluation/business approval |
| ContextProvenancePolicy; ExecutionPolicy/N01 | fields[] (ContextFieldRequirement: field_path, schema_ref=context_schema, required, time_sensitive, allowed_sources[]); conflicting_sources=block; missing_required=block; internal_timezone=UTC; naive_time=reject | clock_tolerance_ms required for time-sensitive fields, otherwise absent; no default tolerance | Source enum=trusted_task_metadata/device_registry/camera_registry/region_definition/declared_derivation; derived fields require versioned derivation ID/inputs from allowlist; never invent context |
| ExecutionPolicy; pinned workflow contract | workflow_version; node_order=[N01,N02,N03,N04,N05,N06]; coverage_policy_ref; context_provenance_policy_ref; resource_requirements_ref; artifact_schema_refs[] | none; no defaults | Fixed deterministic order; no arbitrary next-node Agent; schema refs typed per §9; no executable workflow source |

Resource/input limits, evidence retention duration/classes, deployment IDs,
evaluation acceptance thresholds and clock tolerances are **business/deployment
OPEN values**, not arbitrary object fields or invented defaults. They must be
supplied explicitly before a Bundle using them is accepted. WP-01 may model and
test their types with synthetic labelled fixtures; fixture numbers are not
approved production configuration. Declared derivation IDs refer to registered
system capabilities; Bundle cannot provide executable derivation code.
