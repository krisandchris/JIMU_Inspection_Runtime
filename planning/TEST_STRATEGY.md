# Test Strategy and Evidence Standard

## 1. Test layers

WP-00-R2.1.1 is documentation-only: STATIC_REVIEW_ONLY / TEST_NOT_RUN /
RUNTIME_NOT_RUN. Cases below are plans, not executed evidence. Baseline:
`2ae36ff0c074b1d0c07f99b7cc40a4f7e49a4ed9`. WP-01 is limited to CPU-only
Domain/Fact Registry/AST static type checking/generated Schema tests; complete
Bundle validation is WP-02 and model/workflow/Rule execution tests are later WPs.

| Layer | Purpose | Examples |
|---|---|---|
| Unit | Pure domain behavior | status invariants, typed AST operators |
| Contract | Input/output compatibility | Pydantic, JSON Schema, OpenAPI |
| Property | Invariants across generated values | digest determinism, AST type safety |
| Integration | Infrastructure boundary | PostgreSQL, MinIO, Redis, provider adapters |
| Workflow | End-to-end orchestration | six nodes, retries, cancellation |
| Model evaluation | Empirical model behavior | Profile recall, grounding, Fact accuracy |
| Operational | Deploy/recover/security | Compose, backup restore, GPU OOM |

## 2. Mandatory negative tests

- unknown fields rejected;
- wrong Fact value type rejected;
- `known` Fact without value rejected;
- invalid status/value combination rejected;
- unsupported Rule operator rejected;
- Rule Fact type mismatch rejected;
- cyclic ToolPlan rejected;
- missing Fact producer rejected;
- Bundle path traversal rejected;
- invalid signature rejected;
- stale Attempt cannot commit;
- duplicate Task submission idempotent;
- model fallback without policy rejected;
- N02 output containing BBox rejected;
- N03 output attempting to mutate Question selection rejected;
- N04 extra per-Question call on normal path detected;
- N05 plan selection based on N06 candidate prohibited;
- N06 candidate omission cannot skip mandatory Rule scan.

## 2.1 R2 contract closure cases (planned CPU-only WP-01)

| Topic | Positive fixtures | Rejection fixtures |
|---|---|---|
| ArtifactKind / bindings | All 25 kinds map to model/path; model/tool grounding union; prompt variables/output refs | Undefined capability kind; wrong-kind ContentRef; executable resource; tool localization with fake direct ModelInvocation |
| Question selection | Explicit A/B mode, exhaustive partition, mandatory union, include-all fallback, stable ordered set/selection pin | Unregistered Questions, mandatory omission, N03 set modification, N04 retry changing set, optional producer for mandatory Rule/required Tool |
| Facet / Schema | Primary Schema in every Facet allowlist; semantic/scope/context-compatible reuse | Union-only membership, empty intersection, same shape/different digest, unit/semantic mismatch |
| Policy Catalog | All 16 closed subtypes, explicit values/branch fields and typed refs | Any/extra config, omitted required behavior, invented default, Fact use in rule_applicability, N06 input in tool_condition |
| Producer / authority | Question 1:1, Tool step 1:N paths, one producer/key, explicit batch/step authority revisions | Unmapped Tool JSON Fact, collision, duplicate Attempt with different bytes, last-write authority, cross-Attempt cherry-picking, mismatched FactSelector |
| Forward authority | Profile-only membership, derived reverse indexes, unchanged Question reusable by new Profile version | Serialized reverse membership, digest cycles, applicability selector executing content |
| Coverage / identity | Empty/no_hits/no_target/failure/partial distinct, subset violation retained, cross-Profile Objects separate | Empty→no_violation, none→complete, drop uncovered Profile, implicit merge/Fact sharing |

Finite injected catalogues/IO type paths suffice for WP-01 type checks. Generating
the full ProducerCatalogue, Bundle graph/conditional producer completeness,
packaging/cryptography and admission are WP-02, not hidden WP-01 services.
Selection quality, authority persistence, Rule execution and fallback behavior
need later runtime tests, not contract fixtures alone.

## 2.2 D14 evaluation protocol requirements (not run)

Compare A/B/C using a pinned independently labelled dataset plus held-out slices.
Record no_hits fraction/conditional target miss, violation miss, false acceptance,
auto-clear risk/coverage, unresolved-review fraction, joint misses, invocations,
measured latency/cost/review effort. Explicit denominators/confidence bounds and
zero-sample undefined values are required. Minimum samples/risk/review thresholds
remain OPEN; no performance data exist. Synthetic fixtures do not establish recall
or approve auto-clearing.

## 2.3 F01–F05 final closure cases (planned only)

All fixtures below are future authorized CPU-only model/Schema/static-reference
tests, not executed in R2.1. Invocation, review, node and Tool records are synthetic
finite catalogues, not proof of real calls, approval, evaluation quality or routing.

| Closure | Planned positive fixtures | Required negative/regression fixtures |
|---|---|---|
| F01 hierarchy | Seven gate cases in 01 §3.8.1; Profile/RuleSet/Rule exclusion reasons; active U creates unresolved trace and not_evaluated descendants; skipped/blocked mandatory refs retained without Object | Replacing parent U with F; claiming excluded/complete/no_violation for unresolved parent; child truth present when not_evaluated; missing decisive ancestor/input/evidence; gate uses Object Facts, N02/N06 candidates/Decision; RuleSet exclusion changes N05 plan |
| F02 provider | Explicit remote primary; typed capability-scoped LocalPrimaryException fixture; declared exact fallback with independent review refs; configured/actual provider/route records; local Tool placement is separate | Local as primary in remote mode; missing/expired/wrong-capability exception; implicit ordered-first local selection; timeout-triggered undeclared transition; missing exact IO compatibility/evaluation refs; actual identity copied from expected when unresolved; Tool placement used to bypass an explicit nested gateway policy |
| F03 selection | All/context branches have discovery audit but no proposal call; shared model branch references same actual discovery/proposal Invocation; explicit extra call has distinct ID/binding/budget; fallback keeps trigger Attempt/reason and dispatched-dependent ref | model_advisory missing/dangling/wrong-purpose/wrong-execution Invocation; shared mode with distinct IDs; fake selection call in all/context; extra call without budget/policy or implicit retry budget; fallback missing trigger/reason or fake invocation when undispatched; invalid full discovery envelope repaired into hit |
| F04 projection | Same normalized type/spec, repeated generation identical; Record/member ordering stable; changed parent ID/version/metadata does not change child; child-before-parent ref order represented; explicit generation version/limits | Parent digest/identity in generation input, child `$id`/`$ref` or content; supplied child Schema differs from projection; unsupported/duplicate/recursive type, nonfinite/rounded bound, ambient mutable limit; hand-edited schema drift; claiming changed generation version/spec guarantees identical output |
| F05 Tool origin | Same tool_ref has distinct N03 execution and N05 execution with matching node/Attempt/origin; grounding has no Object or Facts; step maps declared multiple Fact outputs | Missing required common refs; unknown/mixed origin fields; N03 origin on N05 node or foreign Attempt; grounding generates N05 Facts; N03 includes pre-existing Object; step lacks Object/ToolPlan/step declaration; reusing one ToolExecution ID for both origins or wrong actual version provenance |

WP-01 verifies contract shapes, generated Schema repeatability and reference/trace
invariants only. Actual applicability evaluation/mandatory execution is later
workflow/Rule work; trusted approval resolution and Bundle graph/artifact/root
digests are WP-02; provider switching/Tool dispatch/real call budgets are WP-04+.
Add cases to existing contract/negative suites; do not build those runtimes to
satisfy WP-01. No schema generator or test command is run in this revision.

## 2.4 R3-01 / R3-02 Applicability closure cases (planned only)

Future WP-01 tests instantiate precomputed records and inject finite typed
reference catalogues. They check Pydantic/JSON Schema shapes, field conditions,
trace/partition/parent consistency and AST input domains. They do not execute
a Gate Evaluator, Required Evidence Guard, Workflow Engine or Strong Kleene
interpreter. Such behavior tests belong to later workflow/Rule WPs. None of the
following cases is executed in R2.1.1.

### Ordered hierarchy fixtures

P/RS/R below are Profile/RuleSet/Rule evaluated truths. NE is execution status
not_evaluated with no truth field; supplied child truths after stopped parents
are deliberately invalid record fixtures, not calls to child policies.

| Case | Accepted fixture / expected record | Rejected fixture |
|---|---|---|
| H01 P=U, offered RS=F | Profile unresolved with reason; RS/R NE cite P as decisive ancestor | Child FALSE overwrites P uncertainty, child has truth or runs N06; coverage complete/no_violation |
| H02 P=F, offered R=F | Only Profile exclusion evidence; descendant NE projections reference P | Independently evaluated child Rule/exclusion; invented child evidence or N06 Attempt |
| H03 P=T, RS=U, offered R=F | RS unresolved; R NE cites RS | Rule FALSE eliminates unresolved RS, evaluated child condition |
| H04 P=T, RS=T, R=U | Rule unresolved; condition NE; typed metadata diagnostics | Condition evaluated, U converted to F or no_violation |
| H05 P=T, RS=T, R=T | All three reached traces; condition may execute subject to separate guard | Entered condition with an unevaluated/missing parent, complete authority inferred from T/T/T |
| H06 all gates T, condition U | Predicate U with distinct reason; uncertain | U silently changed to no_violation, even with complete guard |
| H07 reached RS=F or R=F | Proven subtree exclusion, siblings retain independent records | Sibling RuleSets excluded, FALSE without decisive evidence, UNKNOWN without unresolved reason |

### Ownership, references and guard fixtures

| Case | Accepted fixture | Required negative/regression fixture |
|---|---|---|
| A01 ownership | N01 ContextSnapshot only; successful N01 precedes Workflow Profile gate; N06 owns reached RS/R levels | N01/N02 authors Profile partition; new N07/extra business NodeRun; evaluation before successful N01; projected record with fake Attempt |
| A02 input domain | Gate refs resolve only declared Context/Region/Profile metadata | Object Facts, N02 Candidates/hits, N06 Decision/Candidate selectors; undeclared context fields |
| A03 partition | Disjoint/exhaustive/ordered eligible, excluded, unresolved lists with exactly one Profile trace each | Duplicate/omitted/out-of-Bundle Profile, wrong truth-to-partition mapping, mismatched Execution/Release/Snapshot |
| A04 N02 boundary | Only eligible_profiles and evaluation ref; registered hits in that subset; DiscoveryProfileDiagnostic audits invalid proposals | Profile outside eligible accepted as hit; N02 re-emits Gate exclusion records; excluded/unresolved Profiles supplied as model candidates |
| A05 empty eligible | N02 skipped/no_eligible_profiles; discovery NE and no invocation; proven all excluded → not_applicable, any unresolved → none/uncertain | Fabricated no_hits, ModelInvocation or no_violation; exclusion conflated with discovery omission |
| A06 real no_hits | Nonempty eligible catalogue with actual discovery invocation returning no_hits → none/uncertain under D09 | Eligible but unreturned Profile removed from expected scope or labelled inapplicable |
| A07 ancestor trace | Child NE has decisive_ancestor_ref in same pinned chain; inherited prefix points to original evaluation | NE carrying truth/evaluated-input evidence; missing/wrong ancestor; lower false replaces parent U; rewritten Profile prefix |
| A08 result references | RS/R wrappers reference their single target trace, exact parents and actual N06 NodeRun/Attempt when evaluated | Contradictory second truth fields; foreign Attempt; reusing superseded revision implicitly; last-write-wins |
| A09 ledger | Shared obligation_id, unresolved parent plus NE descendants; Coverage present even without Objects/Decision | Double-counted unresolved child, hidden blocked scope, invented object_id/Decision; Decision missing available coverage/trace links |
| A10 guard separation | RuleEvaluation guard complete/blocked with Fact/Evidence/explicit Producer Authority refs; predicate truth retained separately | Guard given TruthValue; gate reads Facts; missing/ambiguous authority treated as complete; F AND U simplification conceals required missing input |
| A11 N05 independence | Same bound ToolPlan and declared Context/Fact guard data regardless of RS/R traces | RuleSet/Rule exclusion or N06 Candidate rewrites/cancels N05 plan/steps |
| A12 immutability | Pinned context/Bundle and selected evaluation revisions; append-only new Attempts | Mutation of gate result or historical Decision/Execution records; fabricated cross-record digests |
| A13 unreached execution | Failed N01 has upstream blocker/no Gate ref; eligible no_hits or grounding failure leaves N06 unreached with dependency reason | Invented Gate after failed N01; fake applicability UNKNOWN/ancestor trace for an execution-only blocker |

Required Evidence Guard outcomes and Strong Kleene tables are contract fixtures,
not evidence of actual evaluator behavior. In particular F AND U=F is legal
inside one AST; P=U followed by RS=F is an invalid reached-gate path. No common
boolean helper may be inferred from those fixtures. WP-01 acceptance additionally
requires generated schemas for every independently persisted applicability root
and rejection of extra fields; semantic cross-reference rules need finite-catalogue
validation beyond what JSON Schema alone can express. Planned suite mapping:
WP-01 plan §8 contract/test_applicability_contract.py and existing negative suites.

## 3. Evidence report

Every run records:
- commit SHA and dirty/clean status;
- exact command;
- environment versions;
- start/end time;
- exit code;
- test counts;
- skipped tests;
- logs/artifact references;
- whether a real provider/GPU was used.

Use `TEST_NOT_RUN` if not executed. Use `RUNTIME_NOT_RUN` if no service execution occurred. Never infer passing from test definitions or CI configuration.

## 4. Evaluation separation

Model quality evaluation requires labeled/independently reviewed data. Keep these separate:
- Profile discovery recall;
- Grounding recall/IoU;
- Question Fact accuracy and coverage;
- Tool success and geometric correctness;
- Rule consistency;
- final event precision/recall;
- false acceptance rate.

Model-reported confidence is not an evaluation metric by itself.

## 5. Replay tests

Artifact Replay:
- must not invoke models or tools;
- records scoring protocol version;
- preserves original outputs.

Inference Replay:
- creates a new Execution;
- pins target Release and deployment versions;
- retains source Task linkage;
- never overwrites original Execution.
