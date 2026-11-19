# AGENTS.md — JIMU Inspection Runtime

These instructions are mandatory for Codex and every coding agent working in this repository. In case of conflict, follow the more restrictive instruction and ask the user.

## 1. Project mission and scope

Build **JIMU Inspection Runtime** as a new, independent, configuration-driven inspection execution platform.

Confirmed constraints:
- Single GPU server with NVIDIA RTX 4090.
- Docker Compose for the first deployment.
- Hybrid inference; remote/cloud model APIs are primary, local vLLM and local vision workers are available where appropriate.
- Edge devices capture images and upload image plus trusted metadata; no edge inference is required in V1.
- V1 input is one RGB image per task.
- Six-node deterministic workflow.
- First end-to-end scenario: non-motor vehicle illegal parking.
- Existing JIMU requirements platform is not to be modified in this phase.

The requirements platform may be integrated later through an explicit adapter. **Do not treat its V4 model, Build, Publication, Check, or `subject_id` as this runtime's native domain model.** Do not modify or migrate that platform.

## 2. Mandatory reading order

Before planning or coding:
1. Read this file completely.
2. Read `README.md`.
3. Read all four files under `specs/`.
4. Read `planning/ARCHITECTURE_DECISIONS.md`, `DEVELOPMENT_ROADMAP.md`, `DEPENDENCIES.md`, and `TEST_STRATEGY.md`.
5. Inspect relevant examples.
6. Inspect current repository state and report branch, HEAD, working-tree status and existing files before proposing changes.

Do not rely on conversation memory when repository documents are available. If a document is missing or contradictory, stop and report it.

## 3. Workflow and authorization

Use this loop for every work package:

**Plan → Challenge → Approval → Execute → Verify → Review → Decide**

- First produce a bounded plan with scope, non-goals, affected files, contracts, tests, risks and rollback.
- Challenge your own plan: identify hidden assumptions, failure modes and compatibility risks.
- Before implementation, match the WP, actions and environment against explicit user authorization. The standing grant for WP-01C through WP-07 covers roadmap-scoped local implementation and isolated tests. The primary agent releases each ready package after independent plan review. Consult the [current authorization register](planning/active/PLATFORM_GOAL.md#当前授权记录) for external actions and business decisions. Record its terms once. Start each covered WP after its prerequisites and delegated review pass. Honor any more specific plan-approval condition the user retained.
- Implement only the approved work package.
- Run only authorized, relevant tests. Record exact commands and outcomes.
- Report code evidence separately from runtime evidence.
- At each WP boundary publish the engineering result and check the next WP against the recorded grant. Ask once for an uncovered decision and identify which tasks depend on it. Continue authorized independent tasks. On an automatic continuation, reuse the pending request until the answer, scope, plan or external state changes.

The first Codex session is **planning-only**: repository initialization review and WP-01 plan/contract review. Do not implement the entire platform or begin WP-02+.

### Development subagent model

For new JIMU development subagents (implementation, tests and fixes), call native `spawn_agent` with `model="gpt-6.2-sol"`, `reasoning_effort="medium"` and `fork_turns="none"`. Supply the worktree, scope and acceptance criteria in the task message. Start a new subagent when an existing thread uses an older model.

## 4. Architecture invariants

### 4.1 Six-node responsibilities

- N01 builds an immutable Context Snapshot from task/device/camera/region/time inputs.
- N02 returns only registered Profile hits and each Profile's configured Question set. N02 does not emit boxes, Object IDs, instance-level associations or violation decisions.
- N03 independently grounds each hit Profile and creates Object instances. It does not align instances with N02, reselect Questions or decide violations.
- N04 issues one normal-path joint VLM request per Object for all effective Questions. Each Question's Fact is independently validated and persisted.
- N05 executes the pre-bound Tool Plan for the Profile. Tool execution is not selected based on N06's predicted violation. Tool Facts and execution provenance are persisted.
- N06 predicts candidate violation categories, then performs deterministic Rule evaluation. Candidate prediction must not be the sole gate for checking mandatory applicable Rules; include a safe completeness path.

Never collapse these responsibilities into one autonomous Agent. The workflow engine, not an LLM, owns control flow.

### 4.2 Domain boundaries

Keep definition-time and execution-time objects separate:
- Definition: Entity, Region, Profile, Facet, Question, Tool, ToolPlan, Rule, RuleSet, ModelBinding.
- Runtime: Release, Task, Execution, NodeRun, Object, Fact, ToolExecution, Evidence, ModelInvocation, Decision.

A Profile is not a violation category. An Object is not an Entity. A Question definition is not a Fact. A Tool definition is not a ToolExecution. A Rule definition is not a Decision.

### 4.3 Data and truth semantics

Never conflate:
- `unknown`, `error`, `not_applicable`, `not_evaluated`, `known`;
- Task status, Execution status, NodeRun status, Fact status and Decision result;
- no detection, detection failure, and confirmed absence;
- model confidence and calibrated probability;
- tool failure and negative evidence.

Do not coerce missing or invalid facts to `false`.

### 4.4 Release and replay

Every Execution pins an immutable Release ID and Bundle digest plus workflow, runtime, model deployment and tool versions. Activating a new Release must not mutate in-flight executions.

Distinguish:
- Artifact replay: rescore already persisted outputs without calling models/tools.
- Inference replay: create a new Execution and invoke models/tools again against a pinned Release.

Never claim exactly-once model execution. Use idempotent task acceptance, attempt identities, deduplication and reconciliation.

## 5. Contract-first development

- Pydantic v2 models are the Python source of truth.
- Generate JSON Schema and OpenAPI from authoritative models; do not hand-maintain divergent copies.
- Use strict models (`extra="forbid"`) for external contracts.
- Every externally persisted contract has an explicit `schema_version`.
- Every reference is versioned and digest-pinned where immutable identity matters.
- Use explicit discriminated unions for typed values and results.
- Avoid unbounded `dict[str, Any]` in normative contracts. If an extension map is necessary, define namespace, validation and compatibility rules.
- Define enums centrally. Do not duplicate string literals across nodes.
- Contract changes require compatibility analysis, migration notes and tests.

## 6. Rule Engine safety

- Rule AST is data, not executable Python.
- Only allow registered, typed operators.
- Validate operand types and Fact Schema compatibility at Bundle compile/load time.
- Define missing/unknown/error semantics per operator and Rule policy.
- Do not use `eval`, `exec`, dynamic imports or user-provided code.
- Candidate prediction is an optimization, not a correctness boundary. Mandatory Rule applicability must still be checked or protected by a validated high-recall safety mechanism.
- Preserve evaluated, skipped, blocked and matched Rule references in the Decision trace.

## 7. Model and tool safety

- No API keys, credentials or secrets in source, Bundle, logs, examples or tests.
- Load secrets from environment/secret files or a dedicated secret provider.
- Never log raw authorization headers or secret-bearing request configuration.
- Model Provider fallback is allowed only when explicitly configured, contract-compatible and independently evaluated. Do not silently substitute models.
- Record configured model identity and actual provider/deployment/model version for every invocation.
- Treat remote API output as untrusted data; validate it against schema.
- Tool execution is allowlisted through a registry. Bundle data cannot provide executable code or arbitrary shell commands.
- Apply timeouts, bounded concurrency, input-size limits and resource budgets.
- Preserve original image and source coordinate system; every transform must be recorded.

## 8. Persistence and migration rules

- PostgreSQL is the source of truth for task and execution state.
- Redis is not the only durable record of a task.
- Object storage holds large images, masks and artifacts; PostgreSQL stores immutable references, hashes and metadata.
- Do not assume a PostgreSQL transaction and object-store write are atomic. Use staged upload, hash verification, state transition and compensating cleanup.
- Every migration must be additive where practical, reviewed, reversible or accompanied by a documented forward-recovery plan.
- Never run migrations against production or an unknown database without explicit approval.
- Never delete or rewrite historical Release/Execution evidence as a shortcut.
- Use UTC timestamps internally; preserve original capture time and timezone/clock provenance.

## 9. Testing and evidence

For each WP, include:
- unit tests;
- contract/schema tests;
- negative tests for invalid inputs and state transitions;
- integration tests where dependencies are available;
- deterministic fixtures;
- regression tests for prior failures.

Do not claim a test passed unless it was actually executed. Report:
- exact command;
- environment and dependency versions;
- exit code;
- relevant output;
- skipped tests and reason.

Use explicit labels:
- `STATIC_REVIEW_ONLY`
- `TEST_NOT_RUN`
- `TEST_PASSED`
- `RUNTIME_NOT_RUN`
- `RUNTIME_VERIFIED`
- `UNVERIFIED`

Mock Provider tests do not establish real model quality or real GPU capacity. Synthetic fixture tests do not establish production readiness.

## 10. Dependency policy

- Use `pyproject.toml` with bounded, reviewed dependency ranges and a committed lock file once the package manager is selected.
- Do not install latest versions blindly.
- Validate Python, PyTorch, CUDA, NVIDIA driver, vLLM, Transformers, SAM3 and model-specific compatibility before pinning.
- Separate CPU-only dependencies from GPU images where possible.
- Do not upgrade the whole stack to solve one package conflict without a compatibility report.
- Record tested matrix and image digests.

## 11. Code quality

- Type hints for public functions and contract boundaries.
- Small modules with explicit interfaces.
- No circular dependencies between domain, application, infrastructure and API layers.
- Domain logic must not depend on FastAPI, SQLAlchemy, Redis or model SDKs.
- Provider adapters must not leak vendor-specific response shapes into domain contracts.
- Use structured logs and trace correlation IDs.
- Avoid hidden global mutable state.
- Use async I/O for network calls; isolate blocking GPU/model work in workers.

## 12. Scope protection

- Do not edit the existing JIMU requirements platform.
- Do not create or modify GitHub repositories, Issues, PRs, branches or remote settings without explicit authorization.
- Do not commit or push unless explicitly authorized for the specific action.
- Do not add UI before runtime contracts and the mock six-node workflow are accepted.
- Do not introduce Kubernetes, Kafka, Temporal, LangGraph or a microservice fleet without a documented need and approval.
- Do not invent throughput, latency, GPU memory or accuracy claims.

## 13. Required WP completion report

Every WP report must include:
1. Goal and approved scope.
2. Changed files.
3. Contract changes.
4. Design decisions and unresolved questions.
5. Tests actually run and exact outcomes.
6. Runtime evidence (or `RUNTIME_NOT_RUN`).
7. Security and data migration impact.
8. Known limitations.
9. Acceptance criteria: PASS / CONDITIONAL / FAIL.
10. Recommended next WP, without starting it.

## 14. First-session instruction

First session only:
- inspect repository state;
- verify this starter pack is present;
- review the four specifications for contradictions;
- produce WP-01 plan and schema inventory;
- identify open decisions that block WP-01;
- propose test cases and acceptance gates.

Do not implement WP-01 until the user approves the plan. Do not implement the whole runtime.
