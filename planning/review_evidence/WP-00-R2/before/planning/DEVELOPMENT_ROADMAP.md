# Development Roadmap and Work Package Gates

## Delivery strategy

Runtime-first. Contract-first. One WP at a time. Each WP requires explicit user approval before implementation and an acceptance report before the next WP begins.

## WP-00 — Repository bootstrap and architecture review

**First Codex session scope only.**
- inspect repo status and toolchain;
- install starter documents;
- review consistency and identify unresolved decisions;
- create implementation plan for WP-01;
- do not implement runtime features.

Gate: user approves repository layout, dependency approach and WP-01 plan.

## WP-01 — Domain Model and Contract Foundation

Deliver:
- Python package skeleton;
- strict Pydantic domain models;
- explicit status enums;
- ArtifactRef/version/digest primitives;
- Fact Schema Registry contract;
- typed Rule AST model and operator registry interfaces;
- generated JSON Schema;
- domain and contract tests.

Out of scope: database, API, model calls, GPU, UI.

Acceptance:
- all normative types validate;
- invalid/unknown fields rejected;
- versioned refs validated;
- Fact status/value invariants tested;
- Rule AST rejects unsupported operators and incompatible operand types;
- JSON Schema generated deterministically;
- no dependency on FastAPI/SQLAlchemy/provider SDK in domain package.

## WP-02 — Bundle Compiler, Admission and Release

Deliver:
- Bundle manifest and deterministic file inventory;
- digest/signature verification interfaces;
- dependency closure validation;
- Fact producer/consumer graph;
- ToolPlan DAG validation;
- Rule AST validation;
- Runtime capability report;
- admission receipt;
- immutable Release and activation records.

Acceptance:
- tampered bundle rejected;
- missing producer rejected;
- invalid DAG rejected;
- incompatible required capability rejected;
- activation does not mutate Release;
- pinned Task remains on original Release after activation change.

## WP-03 — Durable Workflow Engine

Deliver:
- Task intake model;
- Execution/NodeRun/Attempt/Event persistence;
- PostgreSQL job claim/lease/fencing;
- deterministic six-node orchestration interfaces;
- cancellation, retry and recovery policy;
- mock node providers.

Acceptance:
- full mocked six-node workflow;
- duplicate request idempotency;
- stale worker cannot commit;
- crash/restart recovery test;
- explicit failed/blocked/skipped distinctions.

## WP-04 — Model Gateway and Tool Executor

Deliver:
- provider interface and adapters;
- remote-first policy;
- optional local vLLM adapter;
- ModelInvocation audit;
- tool registry and worker interface;
- GPU resource admission controls.

Acceptance:
- provider contract tests;
- timeout/rate-limit/error mapping;
- no silent fallback;
- local GPU integration only on approved compatible host;
- measured memory/concurrency report.

## WP-05 — Real inspection pipeline

Deliver:
- real N02/N03/N04/N05/N06 adapters;
- non-motor vehicle illegal parking Bundle;
- typed Fact persistence;
- Rule Engine execution;
- evidence association.

Acceptance:
- representative labeled images;
- independently reviewed per-node outputs;
- no N02 boxes;
- N04 one normal request per Object;
- N05 independent of N06 candidate;
- N06 deterministic Rule evaluation;
- traceable final Decision.

## WP-06 — Evidence, Replay and Evaluation

Deliver:
- evidence lifecycle;
- artifact replay;
- inference replay;
- evaluation protocol and scoring;
- human review integration primitives;
- Bad Case attribution.

Acceptance:
- Artifact Replay makes zero model/tool calls;
- Inference Replay creates a new Execution;
- historical records remain immutable;
- evaluation reports bind dataset, truth, Bundle and model/tool versions.

## WP-07 — Operational hardening and future integration contract

Deliver:
- Compose deployment;
- backup/restore;
- metrics/traces/dashboards;
- security review;
- external Runtime API contract;
- requirements-platform adapter design (not platform modification).

Acceptance:
- restore drill;
- auth/scope tests;
- evidence access tests;
- operational health checks;
- API contract suite;
- no direct DB coupling to requirements platform.

## Cross-WP rules

- A WP may not silently absorb later WPs.
- Every schema change includes migration and compatibility notes.
- Do not use real model APIs or incur cost without explicit authorization.
- Mock tests do not count as model quality validation.
- Runtime readiness and model quality are separate acceptance axes.
