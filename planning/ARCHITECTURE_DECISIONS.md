# Architecture Decision Register

Decision statuses: CONFIRMED / PROPOSED / OPEN.

| ID | Topic | Status | Decision / recommendation | Notes |
|---|---|---|---|---|
| ADR-001 | Runtime built before requirements-platform changes | CONFIRMED | Runtime is independent; requirements platform adapted later | No edits to existing platform |
| ADR-002 | First deployment | CONFIRMED | Single GPU server, Docker Compose | No Kubernetes initially |
| ADR-003 | GPU | CONFIRMED | RTX 4090 | Capacity must be measured |
| ADR-004 | Model strategy | CONFIRMED | Hybrid, cloud inference primary | Local fallback only if explicitly validated |
| ADR-005 | Edge role | CONFIRMED | Capture/context/upload only in V1 | Edge inference future |
| ADR-006 | Input modality | CONFIRMED | One RGB image per Task in V1 | Video out of scope |
| ADR-007 | Six-node deterministic workflow | CONFIRMED | N01–N06 fixed responsibilities | LLM does not own workflow control |
| ADR-008 | Definition vs runtime domain | PROPOSED | Separate aggregates and persistence concerns | Formalize in WP-01 |
| ADR-009 | Runtime Bundle | PROPOSED | Immutable, signed, self-contained config package | Digest/signature details must be frozen |
| ADR-010 | Release authority | PROPOSED | Runtime technical admission; authorized approver business approval; activation separately audited | Standalone V1 authority model OPEN |
| ADR-011 | N06 candidate predictor | PROPOSED | Candidate prediction advisory; evaluate all mandatory applicable Rules in V1 | Optimize only after measured recall and safety fallback |
| ADR-012 | Rule AST | PROPOSED | Typed allowlisted AST; no arbitrary code | Operator catalogue OPEN |
| ADR-013 | Database | PROPOSED | PostgreSQL as durable state authority | Confirm backup/retention |
| ADR-014 | Object storage | PROPOSED | MinIO/S3-compatible | Provider/retention OPEN |
| ADR-015 | Cache/queue | PROPOSED | Redis optional; PostgreSQL durable jobs initially | Load tests decide |
| ADR-016 | Workflow engine | PROPOSED | Explicit Python state machine with durable DB state | Temporal deferred |
| ADR-017 | ORM/migrations | PROPOSED | SQLAlchemy 2 + Alembic | Confirm after WP-00 |
| ADR-018 | Dependency manager | OPEN | uv or Poetry | Choose before WP-01 |
| ADR-019 | Local model set | OPEN | Select only after task/model benchmark | Do not assume model fits |
| ADR-020 | Exact GPU concurrency | OPEN | Measure co-resident peak VRAM and throughput | No invented values |
| ADR-021 | Fact value representation | PROPOSED | Schema registry + discriminated typed payloads | Final implementation in WP-01 |
| ADR-022 | Region geometry | OPEN | Runtime metadata vs spatial subsystem | Not needed to block initial domain skeleton |
| ADR-023 | Question reuse | PROPOSED | Reuse allowed only with schema/context compatibility | Validate at Bundle compile |
| ADR-024 | ToolPlan conditional branches | PROPOSED | Only deterministic branches over declared context/validated Facts | No LLM-authored runtime graph |
| ADR-025 | Result transport | OPEN | Callback + query/reconciliation; transport choice later | Define after task/result contracts |

## WP-00-R1 — D01–D10 decision candidates

Revision date: 2026-11-17. Original ADR-001–025 rows above are preserved as the
starter decision history. The following table refines recommendations without
changing any candidate to CONFIRMED. Candidate status and user approval are
separate: **every D row is PROPOSED / PENDING_USER_APPROVAL**. Document revision
authorization is not WP-01 implementation, dependency installation or test authorization.

| ID | Related ADR / issue | Concrete decision candidate | Candidate status | User approval |
|---|---|---|---|---|
| D01 | ADR-018; CPU boundary | uv, Python 3.12 candidate, Pydantic v2; bounded CPU dependencies with separate lock/matrix; no GPU/service dependencies in WP-01 | PROPOSED | PENDING_USER_APPROVAL |
| D02 | ADR-021; C07 | Offline generated JSON Schema 2020-12 restricted subset; typed scalar/list/closed Record; known requires value, all non-known forbid value and retain reason | PROPOSED | PENDING_USER_APPROVAL |
| D03 | ADR-012; C08 | Typed allowlisted AST; strong Kleene T/F/U predicates plus separate required-input/evidence completeness guard; no arbitrary code or implicit false; WP-01 static type checker only | PROPOSED | PENDING_USER_APPROVAL |
| D04 | ADR-011; C08 | Full mandatory Rule scan including applicability trace; collect_all valid simultaneous matches/categories; unresolved mandatory work blocks no_violation but does not erase valid violations | PROPOSED | PENDING_USER_APPROVAL |
| D05 | C02 | ContentRef = kind/ID/exact version/digest; IdentitySelector = kind/ID/exact version, reverse applicability only; pinned Bundle inventory resolves selectors; content dependency graph acyclic | PROPOSED | PENDING_USER_APPROVAL |
| D06 | C03/C04/C05 | One primary Fact per Question (Record for compound values); configured deployments pinned at creation, actual identities audited after invocation; explicit NodeRun/Attempt transitions and immutable terminal states | PROPOSED | PENDING_USER_APPROVAL |
| D07 | ADR-009; C01/C09 | Self-contained config/resource closure; manifest/checksum exclusions and domain/length-framed root preimage in 02 §4; packaging/hash/signature/admission implementations and signature algorithm gates remain WP-02 | PROPOSED | PENDING_USER_APPROVAL |
| D08 | ADR-023/024 | One ContentRef ToolPlan per Profile; Question/Facet reuse requires compatible exact schema/context/scope; deterministic typed Tool conditions, never N06; structured policies and bounded resources | PROPOSED | PENDING_USER_APPROVAL |
| D09 | C10; Execution Coverage | Explicit discovery/per-Profile grounding/mandatory Rule/evidence coverage; complete/partial/none/not_applicable independent from lifecycle/result; empty/no_hits/no_target/failure cannot prove no_violation | PROPOSED | PENDING_USER_APPROVAL |
| D10 | Cross-Profile Object Identity | Object identity scoped by Execution and Profile; same visible target under multiple Profiles remains distinct Objects; no implicit merge, Fact sharing, unique-physical-object counts or N02 alignment | PROPOSED | PENDING_USER_APPROVAL |

### Approval and compatibility gates

- D01–D10 specify one candidate each; there is no automatic fallback to the
  superseded multi-Fact Question, ambiguous digest reference or linear state chart.
- User approval must name the candidate revision/scope. Until then WP-01 is blocked
  for implementation; resolving the text does not close the approval gate.
- Exact dependency versions remain UNVERIFIED. CPU/GPU matrix separation is a
  D01 candidate, not permission to bypass the existing dependency policy.
- Signatures/trust/activation precedence, deployment identity permissions, spatial
  ownership and real model/GPU choices remain later-WP OPEN decisions. They do not
  authorize placeholder Any/config maps in WP-01 domain contracts.
- Non-backward-compatible shapes are described in WP-01 plan §12. Original
  starter bytes and the initial plan/evidence are retained in
  `planning/review_evidence/WP-00-R1/before/`; PACKAGE_MANIFEST.json remains the
  original distribution manifest, not a regenerated checksum of this revision.

## WP-00-R2 — current candidate register (no approval inferred)

Baseline: main at `46388ce1d8f03e0b122b3038a5930eb0aa3ceb01`.
R1 rows above remain history. Current candidate refinements are below; every
D01–D15 has candidate_status=PROPOSED and approval_status=PENDING_USER_APPROVAL.
None is CONFIRMED/IMPLEMENTED by this document. Business values marked OPEN must
be explicitly configured before the corresponding Bundle/use is accepted.

| ID | Current decision candidate / R2 qualification | Candidate status | Approval status |
|---|---|---|---|
| D01 | uv + Python 3.12/Pydantic v2, pytest/jsonschema/Ruff/mypy CPU lock gate in WP-01; independent GPU gate in WP-04; no installed/verified pins | PROPOSED | PENDING_USER_APPROVAL |
| D02 | Typed scalar/list/closed Record, exact FactSchemaDefinition + generated value_schema, offline 2020-12; non-known value absent | PROPOSED | PENDING_USER_APPROVAL |
| D03 | Allowlisted AST, T/F/U plus independent completeness guard; static input_domain disallows Facts in applicability | PROPOSED | PENDING_USER_APPROVAL |
| D04 | All mandatory Rules scanned regardless of candidates; collect_all valid matches, blocked trace retained | PROPOSED | PENDING_USER_APPROVAL |
| D05 | ContentRef is digest-pinned dependency; selectors never execute content; R2 D15 removes redundant reverse Profile membership without reintroducing digest cycles | PROPOSED | PENDING_USER_APPROVAL |
| D06 | Question→one primary Fact; Tool step→declared many Facts (V1 N05 object-scoped; execution Facts from system producers); whole-batch/step authority explicit; configured vs actual identity distinct; R1 terminal-state rules retained | PROPOSED | PENDING_USER_APPROVAL |
| D07 | Full typed config/resource closure, non-self-referential root; R2 adds producer catalogue and Fact definition/value schema distinction; full compiler/admission/signatures WP-02 | PROPOSED | PENDING_USER_APPROVAL |
| D08 | One ToolPlan per Profile, typed guards/context/validated Facts only, Schema/Facet/context-compatible reuse | PROPOSED | PENDING_USER_APPROVAL |
| D09 | Preserve none/partial coverage semantics and no_hits/no_target/failed distinctions; no implicit definite negative on empty Objects | PROPOSED | PENDING_USER_APPROVAL |
| D10 | Profile-specific Object/Fact identity, no cross-Profile merging or unique-physical-object claims | PROPOSED | PENDING_USER_APPROVAL |
| D11 | Closed ArtifactKind catalogue in 01 §9; reuse ModelBinding/ToolDefinition instead of independent capability artifacts; versioned PromptTemplate, typed grounding provenance | PROPOSED | PENDING_USER_APPROVAL |
| D12 | Explicit QuestionSelectionPolicy: all_declared or mandatory_plus_optional; recommend initial all_declared; optional model/context subset only under explicit policy, mandatory union and configured conservative fallback | PROPOSED | PENDING_USER_APPROVAL |
| D13 | Keep Facet.fact_schema_refs allowlists; Question primary Schema in every linked Facet intersection; exact semantic/schema/scope compatibility and Profile context checks | PROPOSED | PENDING_USER_APPROVAL |
| D14 | Retain A conservative no_hits handling for first V1; evaluate C secondary review offline against B complete-negative protocol requirements before any auto-clear change | PROPOSED | PENDING_USER_APPROVAL |
| D15 | Recommend B: Profile forward ContentRefs sole authoritative membership; remove Question/ToolPlan reverse Profile fields, derive indexes; selectors retained only for explicit applicability constraints | PROPOSED | PENDING_USER_APPROVAL |

### D15 alternatives and compatibility

| Alternative | Version/reuse effect | Closure / conflict handling |
|---|---|---|
| A retain exact reverse membership selectors as consistency check | New Profile version often forces unchanged Question/ToolPlan revision; shared Questions couple otherwise independent Profiles | No digest cycle but two relationship sources can disagree; admission must reject disagreement |
| B forward-only membership (recommended) | Profile changes alone do not re-version reusable Questions/ToolPlans; semantic/context incompatibility still rejected | Forward closure + compiler-derived reverse index; one authority, no membership selector target obligations |
| C optional non-authoritative reverse applicability selectors | Can express intentional restrictions but exact-version restrictions still couple reuse | They must never grant membership; conflicting restrictions can block valid forward refs, requiring explicit semantics |

B removes applicable_profile_selectors from Question and applicable_profile_selector
from ToolPlan in the R2 candidate. IdentitySelector survives as an applicability
constraint over Profile/Region, with full inventory resolution for those remaining
targets. This is a breaking candidate schema change, not a migration of historical
published artifacts. R1 data remain immutable; any future importer must explicitly
validate forward relationships and record conversion provenance, not silently drop
meaningful restrictions. User may choose A/C instead; update contracts/tests before
implementation in that case. D15 qualifies D05, not overwrites its historical row.

### Policy and producer closure

01 §14 is the authoritative typed Policy Catalog; it includes all 15 requested
policies plus ExecutionPolicy. 01 §13 defines producer cardinality, collision
domains, Tool output mapping and append-only authority selection. These complete
D02/D03/D06/D08 contracts without creating new execution services. Registry/type
checks over finite injected definitions are WP-01; graph admission, runtime leases,
model selection/execution, Rule evaluation and review routing remain later WPs.

### Remaining OPEN values and approval gates

- D12: approve mode/fallback per initial Profile; no hidden default or model
  reliability threshold. Required producer Questions cannot be optional.
- D14: business risk/review-rate targets, independently labelled slices/sample
  counts, protocol thresholds and second-review capability remain OPEN; no runtime
  auto-clear protocol is enabled by R2. D09 is unchanged.
- Evidence requirements/retention durations, actual provider/deployment identities,
  resource limits and context clock tolerances remain OPEN configuration values,
  represented by closed typed fields, never arbitrary objects or invented defaults.
- D01 exact dependency versions, GPU compatibility, signing/trust, activation scope
  precedence and spatial ownership remain UNVERIFIED or later-WP OPEN as before.
- Approve D01–D15/current candidate revision and the bounded WP-01 work package
  separately from dependency/test execution. R2 itself authorizes documents only.

## WP-00-R2.1 — F01–F05 limited closure

Baseline: main / `40af8a17855b27063da94b3c9891272f3e3fc4e3`.
This revision refines five existing contracts only; it creates no D16/new
architecture programme. All D01–D15 remain **PROPOSED / PENDING_USER_APPROVAL**.
ADR-004 cloud-first is already CONFIRMED; F02 supplies its missing contract
enforcement, without approving a local-primary exception or changing D status.
R1/R2 historical audit directories and decision history remain unchanged.

| Closure | Affected decisions | Candidate clarification / approval gate |
|---|---|---|
| F01 | D03/D04/D09 | Profile AND RuleSet AND Rule, parent-first trace; active required UNKNOWN blocks, never becomes false; context metadata only. Scope/trace semantics await contract approval |
| F02 | ADR-004, D11 | Remote Primary required for normal gateway routing; fallback needs exact transition/contract/independent quality evidence. Local Primary requires separately approved capability-scoped validity-bounded exception. Tool Worker scheduling is separate |
| F03 | D12 | Selection methods have conditional evidence; model_advisory requires a real ModelInvocation, preferably the shared N02 call. Additional call only under explicit binding/budget/identity/audit; fallback retains actual failed operation/Attempt/reason |
| F04 | D02/D07 | Independent normalized TypeDefinition projection + fixed generation spec; value_schema gets content/digest before parent ref/digest; no parent input. WP-01 schema contracts/generation vs WP-02 complete Bundle integrity stay separate |
| F05 | D06/D11 | ToolExecution.invocation_origin union grounding_binding/tool_plan_step; N03 GroundingRecord never automatically supplies N05 Fact producers; same Tool calls have distinct origin/Attempt identities |

No decision is promoted to CONFIRMED by this patch. D09 conservative no_hits,
D12 A/B modes, D13 exact Facet intersection, D15 forward-only membership,
single primary Question Fact, multiple declared Tool Facts, mandatory full scan,
collect_all, reference separation and CPU/GPU gates are retained. User approval
must cover current F01–F05 field/semantics plus bounded WP-01 implementation;
real fallback/local-primary exceptions additionally need reviewed deployment
configuration and independent quality evidence, never inferred from approval
of a schema. No model/Tool routing or Rule execution code is implemented here.

## WP-00-R2.1.1 — Applicability ownership and ordered gating

Baseline: main / `2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`.
This limited clarification supersedes any reading of the earlier F01 "AND"
shorthand as a flattened cross-level Strong Kleene expression. It does not alter
decision history, add a business node/architecture layer, or approve WP-01.

| Closure | Existing decisions refined | Candidate contract / approval requirement |
|---|---|---|
| R3-01 execution ownership | D03/D04/D09 | N01 only creates ContextSnapshot. After success, an internal deterministic Workflow Engine Pre-N02 Gate partitions the pinned configured Profiles into eligible/excluded/unresolved. N02 receives only eligible Profiles and does not generate gate exclusions. N06 inherits this fixed result and owns RuleSet/Rule applicability. N05 remains bound independently. User approval of these clarified records/ownership is still required |
| R3-02 semantic boundary | D03/D04/D09 | Ordered Profile→RuleSet→Rule: FALSE proves subtree exclusion; UNKNOWN creates unresolved obligation and descendant not_evaluated without TruthValue; TRUE advances. Strong Kleene applies inside one AST only. Required Evidence Guard independently checks Facts/Evidence/Producer Authority with complete/blocked outcome. Child FALSE never erases parent UNKNOWN; condition UNKNOWN stays uncertain. No runtime evaluator is authorized |

01 §3.8.1 defines the six Runtime schemas: ProfileEligibilityEvaluation,
RuleSetApplicabilityEvaluation, RuleApplicabilityEvaluation, ApplicabilityTrace,
ApplicabilityLevelTrace and UnresolvedObligation. They reuse RuntimeRecordRef and
the existing obligation ledger, with no new ArtifactKind/PolicyKind/NodeId.
Coverage retains excluded/blocked/unresolved/not_evaluated branches even without
Objects. Decision traces link the same identities where a Decision actually exists.

Empty eligible scope skips N02 with discovery not_evaluated; all proven excluded
means not_applicable, any unresolved branch prevents no_violation. Actual no_hits
over eligible Profiles retains D09 none/uncertain. This is not an automatic-clear
exception or a change to pending D14.

**D01, D02, D03, D04, D05, D06, D07, D08, D09, D10, D11, D12, D13, D14 and
D15 each remain PROPOSED / PENDING_USER_APPROVAL**, with their R2 table topics
unchanged. D12 A/B, D13 exact intersection, D15 forward-only authority, mandatory
full scan, collect_all, immutable references and CPU WP-01/GPU WP-04 gates remain.
No new decision or candidate is marked CONFIRMED. R1/R2/R2.1 audit directories
and AGENTS.md are protected. STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN.


## WP-01B Goal authorization addendum — 2026-11-17

Earlier ADR/D01–D15 rows remain historical review records; this appendix does not
rewrite their original approval dates/status. The user has approved those current
contracts and WP-01A is merged at main 51a10416d882fe85e5a1cfc5440eac6498fc48f4.
The latest explicit WP-01B Goal text is preserved byte-for-byte in
implementation_evidence/WP-01B/USER_AUTHORIZATION.md
(SHA-256 0b492c517bcd40b84d84234087beed6a7019f808161580738007bf4e3a256863).

Only explicitly authorized scope decisions are recorded here:

| Authorization | Status | Boundary |
|---|---|---|
| Complete WP-01B Definition & Fact Registry engineering, CPU-only dependencies/tests/static checks and independent review | USER_AUTHORIZED | Concrete unresolved contract conflict still requires resolution; no automatic WP-01C |
| Branch dev/wp-01b-definition-fact-registry and independent local worktree | USER_AUTHORIZED / CREATED | No modification to main files; no Commit/Push/PR/Merge |
| RFC8785 JCS for independent ValueSchema generation input and child Schema digest | USER_AUTHORIZED | Full parent/artifact graph/Bundle root/signatures/admission remain WP-02; no implementation validation yet |
| Necessary Fact/Attempt/result/authority typed finite validation inputs and history checks | USER_AUTHORIZED | No Workflow, database/CAS/fencing or real invocation |
| Pure local deterministic ToolPlan DAG validation | USER_AUTHORIZED | Does not implement full Bundle closure or scheduler |
| Astra technical adjudication within already approved semantics | USER_AUTHORIZED | New business semantics or changing prohibited scope requires user decision |

G1-D01 AST staging remains an unresolved scope question in active/WP-01B/FINDINGS.md.
Neither proposed option is entered here as approved. Technical drafts in PLAN-2
do not imply new business approvals; G1-D03/D04 still require explicit field completion.


## WP-01B execution amendments — GOAL-002 explicit user approvals

These are implementation-boundary amendments, not a renumbering or overwrite of D01–D15.
Original decision histories and WP-00 review evidence remain unchanged.

| Item | Decision | Approval evidence / status |
|---|---|---|
| G1-D01 option A | Move existing shared condition AST data models into WP-01B; retain prohibition on RuleDefinition, TypeChecker and Evaluator | Exact user async reply in execution_03/AUTHORIZATION.md; APPROVED_BY_USER |
| X-C01 local Tool Fact selector | Add a tool_condition-only current-ToolPlan local Fact selector; bind enclosing pinned Plan/ancestor identity at use, retaining existing exact reference types | Exact user async reply in execution_03/AUTHORIZATION.md; APPROVED_BY_USER |

X-C01 repairs the ToolPlan→skip Policy→exact ToolProducerSelector→same ToolPlan digest
cycle. Fields and constraints are Domain §13.4 and active CONTRACT_FIELDS §11.2. No
new artifact/policy kind, implicit hash exclusion, cross-Object association or automatic
condition evaluation is authorized. Existing exact selectors and produced Fact identities
remain unchanged; unsupported new nodes fail closed in older readers. Definitions using
the new local node require a new immutable artifact version/digest when published; no
historical content is rewritten. This unshipped WP-01B data-contract addition has no
persisted production migration. CPU synthetic tests do not prove Runtime execution.
