# 03 — Six Node Execution Contract V1.0

**Status:** Proposed normative execution contract  
**Input:** One RGB image per Task  
**Orchestration:** Deterministic state machine

## 1. Global invariants

- Workflow engine owns control flow; LLMs do not choose arbitrary next nodes.
- Every NodeRun has immutable input references/digest, config reference, Attempt history and output references.
- Large payloads move by immutable object references, not in-process mutable globals.
- Every output is schema-validated before downstream use.
- Node, Task, Execution, Fact and Decision states are distinct.
- N02 does not produce instances; N03 does not associate instances with N02.
- N04 performs one normal-path joint request per Object.
- N05 executes Profile-bound ToolPlan regardless of N06 candidate prediction.
- N06 candidate prediction cannot silently suppress mandatory Rule applicability checks.

## 2. Generic Node envelope

Input:
- task_id, execution_id, release_id, bundle_digest
- node_run_id, attempt_id, trace_id
- upstream artifact references and digests
- exact node config reference

Output:
- node_id, node_run_id, attempt_id
- status
- output artifact references and digests
- error code/class/retryability
- started_at, finished_at, duration
- trace_id

Use strict typed contracts. A generic envelope does not replace node-specific schemas.

## 3. N01 — Context Builder

Inputs:
- accepted Task and immutable media metadata;
- Device and Camera registry records;
- Region definition and context schema;
- capture time and clock provenance;
- pinned Bundle.

Outputs:
- immutable ContextSnapshot;
- resolved device/camera/region identities;
- scene context values with provenance;
- missing/invalid required context report.

Rules:
- device_id, camera_id and region_id are separate.
- Never invent missing context.
- Distinguish user-supplied, registry-resolved and derived fields.
- Validate every value against the Bundle context schema.
- A required context failure blocks downstream execution.

## 4. N02 — Omni-JEV Profile Discovery

Inputs:
- original RGB image reference;
- N01 ContextSnapshot;
- applicable Profile catalog from pinned Bundle;
- ModelBinding.

Output:
- ProfileHit records with registered Profile ref, configured Question refs, model-reported score/confidence, and invocation reference;
- excluded/filtered Profile reasons;
- discovery invocation metadata.

Forbidden outputs:
- BBox;
- Object ID;
- instance-level identity or alignment;
- violation category/decision;
- arbitrary unregistered Question.

Questions are selected from the Profile's frozen configuration, not invented by the model. N02 is optimized for recall, but model confidence is not a recall guarantee. No-hit means no Profile was returned; it is not a final `no_violation` Decision.

## 5. N03 — Grounding

Inputs:
- original image;
- N02 ProfileHits;
- Profile-specific grounding capability and Model/Tool binding.

Outputs:
- zero or more Object records per Profile;
- original-image pixel XYXY boxes;
- coordinate system and image dimensions;
- grounding provenance and scores;
- visualization/evidence references;
- explicit per-Profile grounding status, including no-target, failed and partial.

Rules:
- Ground each Profile independently.
- Do not perform N02 instance matching; N02 has no instances.
- Do not modify Question selection.
- Do not decide violation.
- Preserve localization failure records; do not silently drop a Profile hit.
- Object IDs are unique within an Execution, not persistent real-world identities.

Coordinate contract:
- origin top-left;
- `xyxy = [x_min, y_min, x_max, y_max]`;
- pixel coordinates relative to original RGB image;
- validate bounds and positive area;
- transformed/cropped coordinates require explicit transform provenance.

## 6. N04 — VLM Observation

Request granularity:
`one Object → one normal-path joint VLM request → all effective Questions for that Object`.

Inputs:
- original image and Object crop/ROI reference with transform metadata;
- exact Profile and Question versions;
- Context View built only from each Question's allowlist;
- Prompt Compiler output;
- generated response schema.

Output:
- one ObservationBatch per Object/request;
- independent QuestionResult entries;
- independently validated Facts;
- one ModelInvocation reference;
- parse/schema validation diagnostics.

Each QuestionResult includes:
- question_ref;
- fact_key/schema_ref;
- status;
- typed value if known;
- reason when required;
- model-reported confidence if available;
- evidence references;
- validation status.

Error semantics:
- transport failure: request Attempt failed; no fabricated Facts;
- response parse failure: batch parse error;
- one invalid Question field: that Question Fact is `error`, valid siblings may be retained only if the response envelope and association are trustworthy;
- explicit insufficient visibility: `unknown`;
- inapplicable Question: `not_applicable` only when applicability is established by configuration/context;
- omitted field is not `false`.

Retries:
- normal path is one request per Object.
- A retry is a new whole-batch Attempt with a new invocation ID and explicit retry reason.
- Do not silently issue per-Question follow-up requests.
- If a future repair-request mode is introduced, it must be a separately versioned execution policy and must preserve original and repair attempts.

## 7. N05 — Tool Execution

Inputs:
- Object;
- Profile-pinned ToolPlan;
- original image/ROI and transforms;
- validated N04 Facts only where declared as Tool inputs;
- allowlisted Tool Registry and resource policy.

Outputs:
- ToolExecution records per step;
- Tool Facts with typed schema;
- evidence references (mask, geometry, OCR output, etc.);
- DAG status and failure diagnostics.

Rules:
- ToolPlan is fixed by Profile/Release.
- It does not depend on N06 predicted or final violation category.
- Dependencies and input mappings are statically validated.
- Conditional branches may only use declared context or validated Facts and must be deterministic.
- Required tool failure blocks dependent required steps and marks required evidence unavailable.
- Optional tool failure may permit continuation only if the Rule/Evidence policy explicitly permits it.
- Tool failure is never negative evidence.
- Persist actual implementation/model version and input/output digests.

## 8. N06 — Candidate Prediction and Rule Decision

### 8.1 Candidate prediction

May use a model to predict likely violation categories. Store candidates, scores, model identity and invocation. Candidate output is advisory.

### 8.2 Rule completeness and deterministic evaluation

The Runtime must determine all Rules applicable to the Object's Profile and scene from the pinned Bundle. Candidate predictions may prioritize evaluation, but may not be the sole filter for mandatory Rules.

At minimum:
- evaluate all mandatory applicable Rules, or
- use a separately validated high-recall candidate gate with a defined safety fallback and measured slice-level recall threshold.

V1 safe default: evaluate all applicable mandatory Rules. Optimize later only with evidence.

Rule Engine:
1. resolve RuleSet and applicability;
2. resolve required Fact refs and schema versions;
3. apply missing/unknown/error policies;
4. evaluate typed AST operators;
5. resolve conflicts deterministically;
6. validate required evidence;
7. emit Decision and evaluation trace.

Decision result:
- `violation`
- `no_violation`
- `uncertain`
- `not_applicable`

A Rule with required unknown/error Facts generally yields `uncertain` or an explicitly defined policy result, never an implicit `no_violation`.

Decision records evaluated, skipped, blocked and matched Rules, required Facts, evidence and Rule Engine version.

## 9. Task and execution state machines

Task:
`created → accepted → queued → running → completed|partial_failed|failed|cancelled`.

Execution:
`initialized → running → completed|partial_failed|failed|cancelled`.

NodeRun:
`pending → ready → running → succeeded|failed|blocked|skipped|cancelled`.

Attempt:
`created → running → succeeded|failed|timed_out|cancelled|superseded`.

Fact and Decision use their own enums as specified in Domain Model.

## 10. Retry, idempotency and recovery

- Task acceptance is idempotent within a documented `(tenant/project, request_id)` scope.
- Execution creation is explicit; retries do not overwrite prior Execution evidence.
- Each Attempt has a unique ID and monotonic attempt number.
- Use fencing/version checks to prevent stale Workers from committing terminal state.
- External model calls may be duplicated after ambiguous network failure; never claim exactly-once.
- Record provider request IDs when available.
- Reconcile accepted tasks and terminal results by durable query.
- Cancellation is cooperative; late provider responses are retained as superseded/late artifacts but cannot overwrite the authoritative terminal state.

## 11. Evidence and trace

Evidence types include original image, crop, bbox visualization, mask, tool output, geometry result and model response artifact. Store large bytes in object storage and metadata/hash in PostgreSQL.

Trace joins:
Task → Execution → NodeRun → Attempt → ModelInvocation/ToolExecution → Object → Fact → Decision → Evidence.

## 12. Acceptance tests

- N02 schema rejects boxes/Object IDs.
- N03 cannot alter Question refs.
- N04 normal path has one joint request per Object.
- N04 partial invalid Fact does not corrupt valid sibling Facts.
- N05 runs independently of candidate prediction.
- ToolPlan cycle and invalid mapping rejected.
- N06 evaluates all mandatory applicable Rules in safe-default mode.
- Unknown/error Facts never silently become false.
- stale Worker Attempt cannot overwrite current state.
- duplicate Task submission returns same accepted identity.
- cancellation and late response preserve audit history.
- artifact replay performs no model/tool invocation.
