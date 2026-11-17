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
