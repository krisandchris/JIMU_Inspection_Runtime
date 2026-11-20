# 03 — Six Node Execution Contract V1.0

**Status:** Proposed normative execution contract  
**Input:** One RGB image per Task  
**Orchestration:** Deterministic state machine

**Review revision:** WP-00-R2.1.1, PROPOSED / PENDING_USER_APPROVAL.
WP-01 builds data contracts/static type checks and CPU-only tests, not node,
workflow or Rule evaluation execution. R2.1 source commit is
`2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`; before bytes are in
`planning/review_evidence/WP-00-R2.1.1/before/`. R1/R2/R2.1 evidence is unchanged.

## 1. Global invariants

- Workflow engine owns control flow; LLMs do not choose arbitrary next nodes.
- Every NodeRun has immutable input references/digest, config reference, Attempt history and output references.
- Large payloads move by immutable object references, not in-process mutable globals.
- Every output is schema-validated before downstream use.
- Node, Task, Execution, Fact and Decision states are distinct.
- N02 does not produce instances; N03 does not associate instances with N02.
- N04 preserves one joint observation batch per Object; §6 defines the physical VLM request scope.
- N05 executes Profile-bound ToolPlan regardless of N06 candidate prediction.
- N06 candidate prediction cannot silently suppress mandatory Rule applicability checks.

## 2. Generic Node envelope

Input:
- schema_version (explicit on every independently persisted envelope/record)
- task_id, execution_id, release_id, bundle_digest
- node_run_id, attempt_id, trace_id
- upstream artifact references and digests
- exact node config reference

Output:
- schema_version
- node_id, node_run_id, attempt_id
- status
- output artifact references and digests
- error code/class/retryability
- started_at, finished_at, duration
- trace_id

The invocation envelope requires attempt_id. A NodeRun skipped/blocked/cancelled
before invocation uses a separate lifecycle record with no fabricated attempt_id
or started_at. Actual model identities are appended in ModelInvocation after the
call; creation pins configured bindings/constraints, never invented actual values.

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

### 3.1 Pre-N02 Profile Eligibility Gate — Workflow Engine precondition

Contract chain: N01 ContextSnapshot → Pre-N02 Gate → N02 discovery of eligible
Profiles → N03 grounding → N04 joint observation → N05 pre-bound Tool execution
→ N06 RuleSet/Rule gates and Rule conditions. This internal deterministic check
is not a seventh business node or a new architecture layer; N01 stays Context
Builder only. WP-01 defines its data, not an evaluator or Workflow implementation.

After N01 succeeds, the Workflow Engine reads the pinned Bundle's complete
configured Profile scope and each Profile.applicability_policy_ref. It permits
only declared ContextSnapshot/Region/Profile metadata, never Object Facts,
N02 Candidates/hits, N06 Candidates/Decisions or model confidence. A required
context failure before a valid Snapshot blocks this check; no fake gate result.

ProfileEligibilityEvaluation outputs exactly the partition eligible_profiles,
excluded_profiles, unresolved_profiles plus applicability_traces (typed
RuntimeRecordRef list). Every configured Profile has one evaluated trace;
TRUE/FALSE/UNKNOWN respectively chooses its partition. FALSE requires decisive
metadata evidence; UNKNOWN requires unresolved diagnostics and keeps a required
obligation. Output is immutable and pins ContextSnapshot, Release/Bundle and
workflow version. The result is workflow evidence, not a Gate NodeRun/Attempt.

N02 receives only eligible_profiles and the evaluation reference, never asks the
model to re-decide eligibility, and never authors the Gate's exclusions. Excluded
and unresolved branches remain in ExecutionCoverage, outside N02 discovery input.
If the eligible set is empty, record N02 skipped/no_eligible_profiles, discovery
not_evaluated and no invocation. All proven exclusions allow not_applicable;
any unresolved Profile yields none/uncertain. By contrast, an actual N02 no_hits
over a nonempty eligible set means uncovered discovery under unchanged D09.
An unresolved branch cannot erase a valid match from another eligible Profile;
the incomplete scope remains visible in coverage.

## 4. N02 — Omni-JEV Profile Discovery

Inputs:
- original RGB image reference;
- N01 ContextSnapshot;
- eligible_profiles from ProfileEligibilityEvaluation, exact refs into the pinned Bundle;
- profile_eligibility_evaluation_ref (same Execution/ContextSnapshot/Release pins);
- Profile.discovery_binding_ref (ModelBinding capability=profile_discovery), typed PromptTemplate and QuestionSelectionPolicy.

Output:
- ProfileHit records with registered profile_ref, question_selection_ref, effective_question_refs, model-reported score/confidence and invocation reference;
- DiscoveryProfileDiagnostic records for invalid discovery proposals, scoped to
  the eligible catalogue; never Profile eligibility exclusions;
- discovery invocation metadata.

DiscoveryProfileDiagnostic has diagnostic_id, execution_id, the actual discovery
model_invocation_ref, raw_response_ref (EvidenceRef), nonempty validation_issues
and reason_code=invalid_profile_proposal/noneligible_profile_proposal. It carries
schema_version if independently persisted; invalid raw IDs remain untrusted
response evidence, never unresolved ContentRefs or applicability exclusions.

Forbidden outputs:
- BBox;
- Object ID;
- instance-level identity or alignment;
- violation category/decision;
- arbitrary unregistered Question.
- Profile eligibility partition/exclusion/UNKNOWN decisions owned by the Pre-N02 Gate.

Every accepted ProfileHit must belong to eligible_profiles. An eligible Profile
not returned is a discovery omission, not an inapplicable Profile. Rejected
unregistered/noneligible proposals are validation diagnostics, never valid hits
or new Profile exclusion records. N02 input does not include the excluded or
unresolved partition as model candidates. N03 copies each hit's exact effective
Question set; N04 retains all its Questions in the Object observation batch.

Questions are selected from the Profile's frozen configuration, not invented by the model. N02 is optimized for recall, but model confidence is not a recall guarantee. No-hit means no Profile was returned; it is not a final `no_violation` Decision.

Release freezes Profile.question_refs and question_selection_policy_ref; it does
not precompute every task's optional subset. D12 candidate supports all_declared
or mandatory_plus_optional, requiring an explicit mode, exhaustive mandatory/
optional partition and conservative fallback. Initial recommendation all_declared;
model_advisory optional selection is an explicitly configured alternative, never
a default or free-form Question generation. See 01 §11/14 for the complete fields.
System includes all mandatory Questions regardless of proposal. Omission of an
optional Question required by a mandatory Rule/Tool is a configuration error,
not permission to bypass downstream work. SelectionRecord records pinned config,
context, proposal, validation and actual invocation; no model-reported reason is
treated as truth. N02 still emits no instance identifiers or boxes.

F03 (01 §11.1): all_declared/context_rules records have no selection
model_invocation_ref; discovery_model_invocation_ref still audits real discovery.
model_advisory requires a real proposal invocation: prefer shared_n02, with the
same ID as discovery and both invocation purposes. A separate optional-proposal
call requires explicit_additional policy, exact binding/resource refs, a bounded
additional-call budget including retries, distinct invocation/Attempt identity and
actual provider audit. It is not a hidden repair. FallbackSelectionEvidence records
triggered method, actual failed operation/Attempt, reason and diagnostic refs, plus
trigger_invocation_ref iff dispatched; no fabricated call when selection failed
before dispatch. Whole discovery failure does not become a ProfileHit by fallback.

## 5. N03 — Grounding

Inputs:
- original image;
- N02 ProfileHits;
- Profile.grounding_binding (discriminated ModelBinding or ToolDefinition binding, 01 §10).

Outputs:
- zero or more Object records per Profile;
- original-image pixel XYXY boxes;
- coordinate system and image dimensions;
- grounding provenance and scores;
- original media and grounding evidence references;
- explicit per-Profile grounding status, including no-target, failed and partial.
- GroundingRecord producer branch model/tool, with corresponding ModelInvocation/ToolExecution provenance; no fake model invocation for tool-only localization.
- ToolExecution.invocation_origin.kind=grounding_binding, node_id=N03, same execution/Profile/binding and actual Attempt; object_ref absent, generated_fact_refs empty (F05, 01 §4.5).

Rules:
- Ground each Profile independently.
- Do not perform N02 instance matching; N02 has no instances.
- Do not modify Question selection.
- Copy question_selection_ref and effective_question_refs unchanged from ProfileHit into ProfileGroundingResult/Object. Validate equality with SelectionRecord; no localization-based reselection or cross-Profile reuse.
- Do not decide violation.
- Preserve localization failure records; do not silently drop a Profile hit.
- Object IDs are unique within an Execution, not persistent real-world identities.
- Object identity is Profile-specific: overlapping detections across Profiles remain separate records and cannot implicitly share Facts or Decisions (D10; 01 §4.3).

Coordinate contract:
- origin top-left;
- `xyxy = [x_min, y_min, x_max, y_max]`;
- pixel coordinates relative to the original RGB image. The group box encloses its ordered member boxes. A group remains one Object even when it contains multiple vehicles;
- validate bounds and positive area;
- transformed/cropped coordinates require explicit transform provenance.

## 6. N04 — VLM Observation

### Physical request and Object projection

The signed observation binding selects the protocol. Legacy bindings submit one joint request per Object. A binding marked `JIMU_N04_REGIONS_V1` uses a projection-capable provider. On the normal path the provider sends the original image once with all selected N03 regions and their assigned Questions. Empty grounding produces zero N04 model calls. Eligible regions must share a compatible binding. An unsupported mix or an oversized request fails explicitly; it must never trigger per-region fallback calls.

The compiler uses top-left-origin `xyxy` coordinates normalized to 0–999, retains grouped member order, and lists each shared Question once. Short region IDs are scoped to this request. The mapping to N03 Object IDs is pinned along with the effective question sets. Each logical Object request to the adapter receives only its own answer slots. Each Object keeps a distinct ObservationBatch, Fact authority and N05/N06 linkage. Physical VLM call counts come from upstream dispatch receipts; logical Object projections are counted separately.

The per-run provider journal binds the execution and attempt to a shared request identity, exact original image, region manifest, model request and response. Reopening a completed request reads the recorded response. An unresolved prior dispatch cannot authorize another physical call under the same identity. An explicit new Attempt can call again. Legacy pinned Releases and archived evidence retain their original semantics.

Inputs:
- the pinned original RGB image and the N03 Object ROI. N03 publishes geometry directly; N04 locates targets using coordinates in either request protocol. Historical marked-media fields are readable for artifact replay; new observations use the original;
- exact Profile and Question versions;
- Context View built only from each Question's allowlist;
- Prompt Compiler output;
- generated response schema.
- Profile.observation_binding_ref and the N02-pinned question_selection_ref/effective_question_refs; exact Question prompt/primary-Fact contracts compose the joint response schema.

Output:
- one ObservationBatch per Object;
- independent QuestionResult entries;
- independently validated Facts;
- one ModelInvocation reference;
- parse/schema validation diagnostics.

Each QuestionResult includes:
- question_ref;
- exactly one primary fact_key/schema_ref and one primary Fact result;
- status;
- typed value if known;
- reason when required;
- model-reported confidence if available;
- evidence references;
- validation status.

QuestionDefinition declares primary_fact_key and primary_fact_schema_ref, not an
output-schema list. Compound observations are one closed Record value. Invalid
required Record fields invalidate that Question's entire Fact; valid other
Question results may survive under trustworthy association. Duplicate/missing
Question association is diagnosed, never silently overwritten. Optional Record
fields may be absent per Schema but do not carry independent Fact status; use
separate Questions for independently uncertain observations.

Error semantics:
- transport failure: request Attempt failed; no fabricated Facts;
- response parse failure: batch parse error;
- one invalid Question field: that Question Fact is `error`, valid siblings may be retained only if the response envelope and association are trustworthy;
- explicit insufficient visibility: `unknown`;
- inapplicable Question: `not_applicable` only when applicability is established by configuration/context;
- omitted field is not `false`.

Retries:
- each logical request contains all effective Questions for its Object.
- A retry is a new whole-batch Attempt with a new invocation ID and explicit retry reason.
- Do not silently issue per-Question follow-up requests.
- If a future repair-request mode is introduced, it must be a separately versioned execution policy and must preserve original and repair attempts.

## 7. N05 — Tool Execution

Inputs:
- Object;
- Profile-pinned ToolPlan;
- the current Object’s original-pixel group box and ordered member boxes. Image tools read pixels from the pinned original and may crop member regions for processing. Parking zones and other scene geometry remain separate tool inputs;
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

Tool output_schema_ref validates the normalized ToolResult envelope. Each declared
fact_output_mappings entry extracts one typed value_path and validates that value
against its own FactSchemaDefinition (01 §13); one step can produce many declared
Facts, not one unrestricted JSON Fact. Whole-step failure is not negative evidence.
AuthoritativeResultSelection selects an explicit step Attempt/output group;
N04 authority selects a whole Object observation batch. Consumers resolve these
pinned revisions, not the last stored Fact. WP-01 models the records and static
paths only; execution/persistence enforcement remains later WPs.

Every N05 ToolExecution has invocation_origin.kind=tool_plan_step with tool_plan_ref,
step_id and matching producer_declaration_ids, node_run_ref=N05 and actual attempt_ref,
object_ref, input/output refs and resolved actual implementation version. The same
tool_ref called in N03 and N05 has distinct tool_execution_id/origin/Attempt audit.
N03 localization output feeds GroundingRecord; only N05's declared step origin can
produce mapped Tool Facts for FactProducerDeclaration. Tool reuse never promotes
N03 output to N05 evidence/authority silently. No Tool runtime is built in WP-01.

## 8. N06 — Candidate Prediction and Rule Decision

### 8.1 Candidate prediction

May use a model to predict likely violation categories. Store candidates, scores, model identity and invocation. Candidate output is advisory.

### 8.2 Rule completeness and deterministic evaluation

The Runtime must determine all Rules applicable to the Object's Profile and scene from the pinned Bundle. Candidate predictions may prioritize evaluation, but may not be the sole filter for mandatory Rules.

V1 requires a full scan of all mandatory Rules in the pinned Profile/scene scope.
Candidate scores, empty candidates, candidate failure and early matches cannot
filter or stop this scan. Each mandatory Rule gets an applicability trace:
applicable → evaluated/blocked; inapplicable → skipped with deterministic reason;
unknown applicability → blocked/unresolved, never silently excluded. A high-recall
candidate gate is not an alternative V1 execution mode; later optimization needs
a separately approved version and measured evidence.

Rule applicability uses only declared Context/Region/Profile metadata. Object
Facts belong to condition_ast, never applicability. Rule FactSelectors match the
Question primary output or named Tool step output and declared Record field_path;
actual operands use pinned authoritative selection revisions. Tool guards may use
validated upstream Facts and context, never Candidate/Decision (01 §13.3/14).

R3 hierarchy is ordered Profile → RuleSet → Rule on the same pinned Snapshot
(01 §3.8.1). N06 consumes the selected ProfileEligibilityEvaluation; it does not
re-evaluate Profile policies or generate the Profile gate's exclusion records.
Only an eligible Profile's reached branches enter N06 RuleSet then Rule gates.
RuleSetApplicabilityEvaluation and RuleApplicabilityEvaluation pin their parent
evaluations and real N06 NodeRun/Attempt when actually evaluated. Descendant
not_evaluated projections cite the decisive ancestor, without fabricated calls.

At each reached level, FALSE excludes only its subtree with decisive evidence;
UNKNOWN creates an UnresolvedObligation, descendants not_evaluated; TRUE advances.
not_evaluated has no TruthValue. Child FALSE must never erase parent UNKNOWN:

| Offered inputs | Required recorded path / consequence |
|---|---|
| P=U, lower RS=F | P unresolved; RS/Rule not_evaluated; reject the offered child result |
| P=F, lower Rule=F | Only P exclusion evidence; all lower policies not_evaluated |
| P=T, RS=U, lower Rule=F | RS unresolved; Rule not_evaluated, cannot override RS |
| P=T, RS=T, Rule=U | Rule unresolved; condition not_evaluated |
| P=T, RS=T, Rule=T | Condition may be evaluated, subject to separate required evidence guard |
| All gates T, condition=U | Uncertain; never implicit no_violation |

No gate reads Object Facts, N02 Candidates or N06 Decisions. ApplicabilityTrace
keeps the fixed Profile prefix and per-level target/policy/input refs, owner,
evaluated truth or not_evaluated status, reason/evidence and decisive ancestor.
Mandatory scanning accounts for the entire configured scope, including
ancestor-based exclusions and unresolved branches even when no Object/N06 call
exists. ExecutionCoverage owns this execution-wide ledger; do not invent an
Object Decision or N06 Attempt for a Profile stopped before discovery.
N05 is already independently bound: no RuleSet/Rule result implicitly changes,
cancels or prunes its ToolPlan. Its own Context/validated-Fact conditions remain.

Rule Engine:
1. inherit Profile eligibility and resolve ordered RuleSet/Rule gates;
2. for reached Rules, resolve typed Fact/schema refs and pinned producer authority;
3. apply declared missing/unknown/error policies and check Required Evidence Guard;
4. evaluate typed Rule Condition AST where allowed, retaining predicate diagnostics;
5. require complete Facts/Evidence/Producer Authority before any definitive result;
6. collect every valid match and preserve all unresolved/skipped/blocked work;
7. emit Decision and evaluation/coverage references without rewriting ancestors.

Decision result:
- `violation`
- `no_violation`
- `uncertain`
- `not_applicable`

A Rule with required unknown/error Facts generally yields `uncertain` or an explicitly defined policy result, never an implicit `no_violation`.

Decision records evaluated, skipped, blocked and matched Rules, required Facts, evidence and Rule Engine version.

### 8.3 Three-valued predicates and simultaneous matches (D03/D04)

Three separate contracts must not be implemented as one generic boolean function:

| Contract | Inputs / output | Boundary |
|---|---|---|
| Applicability Gate | Declared metadata; evaluated T/F/U plus ordered obligation/trace outcome | Parent FALSE/UNKNOWN stops descendants; ownership in §3.1/8.2 |
| Strong Kleene expression semantics | Typed operands within one Policy AST or Rule Condition AST; T/F/U | Does not flatten Profile/RuleSet/Rule lifecycle or turn not_evaluated into truth |
| Required Evidence Guard | Required validated Facts, Evidence and pinned authoritative Producer/Attempt selections; complete/blocked with typed reasons | No applicability decision or TruthValue; blocks a definitive result on missing/ambiguous authority or evidence |

Predicate truth is T/F/U (true/false/unknown), separate from FactStatus and
RuleEvaluation outcome. Known valid operands use typed comparisons. Missing
records and unknown/error/not_applicable/not_evaluated operands produce U with
their original distinct reasons preserved; applicability is established separately.
There is no implicit false conversion. Logical expressions use strong Kleene:

| A | B | all(A,B) | any(A,B) |
|---|---|---|---|
| T | T | T | T |
| T | F | F | T |
| T | U | U | T |
| F | T | F | T |
| F | F | F | F |
| F | U | F | U |
| U | T | U | T |
| U | F | F | U |
| U | U | U | U |

not(T)=F; not(F)=T; not(U)=U. all/any require nonempty argument lists.
Complete operand diagnostics are retained even if the truth value is already
determined. Before a definitive Rule result, a separate safety guard requires all
declared required Facts, Evidence and Producer Authority to be available/valid.
Authority checks resolve the exact declared producer/schema/scope and selected
authoritative Attempt revision, not the last stored Fact. Missing/ambiguous/stale
selection blocks with its own reason; guard outcome is complete/blocked, not T/F/U.
Thus F AND U inside a single AST may have
predicate F but a missing required Fact still yields outcome=blocked and effective
result=uncertain. This guard is not a different truth table. Optional inputs obey
the declared typed policy; they cannot conceal missing mandatory inputs.

V1 conflict_policy candidate is `collect_all`: retain every evidence-valid matched
Rule and category, with no winner, implicit priority or early termination. Duplicate
categories are presented as a set while all matched Rule refs remain. Mutually
exclusive/winner-takes-all RuleSets are unsupported V1 capabilities and must be
rejected, not resolved by storage order or a model.

Per Object: any valid match → violation (even if another Rule is blocked, recorded
with incomplete coverage); no match plus blocked/U/unresolved mandatory work →
uncertain; complete applicable mandatory evaluation with every effective result
false → no_violation; deterministic proof that no mandatory Rule applies →
not_applicable. Optional advisory outcomes cannot veto a mandatory valid match.
All mandatory Rules are still scanned after a match. Rule evaluation implementation
and behavioral truth-table tests belong to WP-05, not WP-01.

### 8.4 Execution Coverage and empty-object cases (D09)

ExecutionCoverage is required even when no Object Decision exists. Expected scope
is resolved from pinned configuration and trusted context, not just N02 hits.
No candidate/model score proves complete physical-scene recall.

| Case | coverage_status | Execution summary constraint |
|---|---|---|
| Context deterministically excludes every configured inspection obligation | not_applicable | not_applicable with exclusion evidence; not inferred from empty detection |
| Pre-N02 has no eligible Profiles and at least one unresolved Profile | none | uncertain; discovery not_evaluated, no N02 call/no_hits; unresolved parent and descendant not_evaluated retained |
| Pre-N02 proves all configured Profiles excluded | not_applicable | not_applicable; N02 skipped/no_eligible_profiles, discovery not_evaluated, never fabricated no_hits |
| Some Profiles eligible, others unresolved | partial when some obligations completed, otherwise none | valid violation preserved; otherwise uncertain; excluded branches do not clear unresolved ones |
| JEV succeeds with no_hits, no Objects | none | uncertain; record successful discovery/no_hits, not confirmed absence |
| JEV failure before usable hits | none | uncertain with failure; Execution may failed, a different status domain |
| Hit Profile grounds no_target, no Objects | none for that Profile | uncertain; retain no_target, do not rewrite as grounding failed or negative evidence |
| Grounding failed for all hit Profiles | none | uncertain; downstream object nodes blocked/skipped with reason |
| Some Profiles/Objects succeed, others grounding partial/failed or required evidence missing | partial | violation if any valid match; otherwise uncertain; preserve successful subset Decisions |
| Nonempty grounded Objects, all in-scope obligations and mandatory evidence/evaluations complete | complete | violation if any valid match; otherwise no_violation only with complete false results; all deterministically inapplicable → not_applicable |
| Unexpected empty Object list without matching per-Profile reasons | invalid report | reject contract; never infer complete/no_violation |

complete means completed configured execution obligations, not measured recall.
Expected/observed Profile identities, per-Profile grounding status/Object refs,
mandatory applicable/evaluated/blocked/unresolved refs and reasons must reconcile.
An eligible Profile omitted by discovery remains an uncovered obligation. N02
cannot reclassify it as excluded or remove it from expected scope because it
was not returned; the Pre-N02 partition is fixed. A MandatoryRuleObligation identifies the
Profile scope before Object creation and the Object scope where available; empty
grounding does not require a fabricated object_id to record blocked coverage.
No-target/no-hits never becomes complete merely because the remaining Object loop
is empty. Discovery partial prevents complete coverage. All task-scope aggregation
retains Object/Profile provenance and does not claim unique-vehicle counts (D10).
Execution.completed can coexist with coverage.none/summary.uncertain when a
successful invocation found nothing; infrastructure failure is tracked separately.
Coverage pins ProfileEligibilityEvaluation when evaluated, selected RuleSet/Rule evaluations,
ApplicabilityTraces and the shared obligation identities. It separately retains
excluded, unresolved, blocked and not_evaluated refs; every descendant projection
references its decisive ancestor. No lower result replaces ancestor uncertainty
and no unresolved required branch permits global no_violation. Where an Object
Decision exists, it keeps these references and skipped/blocked/not_evaluated Rule
refs; where none exists, Coverage alone preserves the gaps. Profile metadata
exclusion is not discovery no_hits, confirmed absence or a Rule Condition FALSE.
An upstream N01 failure retains its lifecycle blocker without a fabricated Gate
reference. If no applicability ancestor stopped the chain but N06 is unreached
(e.g. discovery no_hits/grounding failed), preserve that upstream reason; do not
invent an UNKNOWN applicability trace just to represent execution not_evaluated.

### 8.5 no_hits operational tradeoffs and pending D14

D09 stays unchanged: current V1 no_hits/no_target/empty required scope gives
uncertain, not confirmed absence. An uncertain result is an input to a future
review/routing policy; it does not assert that a human-review service already
exists. Normal clean images can therefore increase the unresolved/review workload.

| Candidate | False acceptance / miss risk | Review load and compute | Annotation/evaluation/config needs |
|---|---|---|---|
| A conservative | Does not auto-clear no_hits; cannot recover targets missed by discovery, but avoids treating that miss as no_violation | Potentially high unresolved/review fraction; no extra inference pass | Representative positive/negative/no_hits slices and review capacity; simplest policy |
| B independently validated complete-negative protocol | Could permit definite negatives only under reviewed scope/quality protocol, never from raw JEV confidence; residual miss risk must be bounded | May reduce review load; protocol can require more computation | Independent labelled target/violation truth, slice-level confidence bounds, versioned protocol and drift/revalidation gates; highest proof/config burden |
| C independent second review on no_hits | May recover first-stage misses; independence cannot be assumed, correlated misses remain | Additional model/tool calls for no_hits cases; possible reduction in review if validated, otherwise unresolved | Paired labelled first/second outputs, joint-miss analysis, costs and quota, explicit binding/policy/budgets; more config |

Recommendation: retain A for first accepted V1 and evaluate C offline, comparing
against B's acceptance requirements before any auto-clear change. D14 remains
PROPOSED / PENDING_USER_APPROVAL; B/C are not supported CoveragePolicy modes in
the current candidate. They require a separately approved policy/protocol version
and Runtime capability. C, if later approved, must be an explicitly configured
N02 internal secondary capability with independent invocations/audit, not a new
autonomous workflow node; N02 output stays Profile/Question-only, and N03 still
grounds independently. No implicit fallback API or detector is called now.

Required measured metrics (no measurements or thresholds are claimed):
- no_hits_fraction = discovery no_hits tasks / all eligible tasks;
- no_hits_target_miss = labelled relevant-target images among no_hits / all no_hits;
- violation_miss_rate = labelled violating tasks not detected / all labelled violating tasks;
- false_acceptance_rate = labelled violating tasks auto-cleared / all labelled violating tasks;
- auto_clear_risk = labelled violating tasks auto-cleared / all auto-cleared tasks;
- unresolved_or_review_fraction, auto_clear_coverage, per-Profile/scene/lighting/
  occlusion slice recall, secondary recovered positives and joint-miss rate;
- invocation count/task, no_hits conditional extra calls, measured latency P50/P95,
  provider cost/task and reviewed minutes/image, all with actual version pins.

Report denominators and confidence intervals (recommend a stated 95% convention),
including zero-event upper bounds; zero samples is undefined, not perfect accuracy.
Dataset/dedup rules, independent truth review, per-slice minimum sample counts,
maximum acceptable risk/review fraction and protocol thresholds are business OPEN
decisions. No score or synthetic fixture substitutes for labelled evaluation.

## 9. Task and execution state machines

Task:
`created → accepted → queued → running → completed|partial_failed|failed|cancelled`.

Execution:
`initialized → running → completed|partial_failed|failed|cancelled`.

NodeRun permitted transitions (all unlisted edges rejected):

| From | To | Guard |
|---|---|---|
| pending | ready | required dependencies/context available |
| pending / ready | blocked | prerequisite unavailable; structured reason; no invocation Attempt |
| pending / ready | skipped | configured deterministic inapplicability/empty-object fanout; reason; no invocation Attempt |
| pending / ready | cancelled | cancellation before start; no invocation Attempt |
| ready | running | first Attempt claimed/started |
| running | succeeded | authoritative successful output validated |
| running | failed | nonretryable failure or retry budget exhausted |
| running | cancelled | cancellation fenced and recorded |
| succeeded / failed / blocked / skipped / cancelled | none | terminal immutable |

Retryable Attempt failure keeps NodeRun running; a new Attempt uses a strictly
increasing attempt_number. No running→ready or failed→running reset. A terminal
NodeRun retry/replay requires a new explicit run identity linked to its predecessor.
Failures after a Node starts are failed (not pre-start blocked/skipped).

Attempt permitted transitions (all unlisted edges rejected):

| From | To | Guard |
|---|---|---|
| created | running | valid claim/fencing token, actual invocation starts |
| created | cancelled / superseded | pre-start cancellation or replaced claim |
| running | succeeded | validated response and authoritative commit rights |
| running | failed / timed_out | normalized failure / elapsed deadline |
| running | cancelled / superseded | cancellation or loss of authoritative lease/fence |
| succeeded / failed / timed_out / cancelled / superseded | none | terminal immutable |

Late responses append invocation/late-artifact records; they cannot rewrite a
terminal Attempt to superseded or succeeded. Worker fencing/lease enforcement is
WP-03 behavior. WP-01 only models transitions/guards and tests pure validity.

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
- N04 region mode sends one physical request per nonempty compatible image/attempt; legacy mode sends one per Object.
- N04 partial invalid Fact does not corrupt valid sibling Facts.
- N05 runs independently of candidate prediction.
- ToolPlan cycle and invalid mapping rejected.
- N06 evaluates all mandatory applicable Rules in safe-default mode.
- Unknown/error Facts never silently become false.
- stale Worker Attempt cannot overwrite current state.
- duplicate Task submission returns same accepted identity.
- cancellation and late response preserve audit history.
- artifact replay performs no model/tool invocation.
- empty Object/JEV no_hits/grounding no_target/failed/partial coverage never fabricates no_violation; retain subset violations and distinct coverage/status domains.
- overlapping cross-Profile detections keep distinct Object IDs and cannot silently merge Facts.
- pending/ready blocked/skipped creates no fake Attempt; retries retain terminal Attempt history; late results never rewrite terminal state.
- R3-01/R3-02: Gate ownership/eligible-only N02, ordered parent blockers without child truth, separate AST logic and Facts/Evidence/Producer Authority guard; empty eligible is not no_hits and N05 remains independent. WP-01 verifies record/static-type fixtures only (TEST_STRATEGY §2.4); actual evaluators are later WPs. No test is executed in R2.1.1.
