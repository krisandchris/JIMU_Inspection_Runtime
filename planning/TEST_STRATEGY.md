# Test Strategy and Evidence Standard

## 1. Test layers

WP-00-R2.1 is documentation-only: STATIC_REVIEW_ONLY / TEST_NOT_RUN /
RUNTIME_NOT_RUN. Cases below are plans, not executed evidence. Baseline:
`40af8a17855b27063da94b3c9891272f3e3fc4e3`. WP-01 is limited to CPU-only
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
