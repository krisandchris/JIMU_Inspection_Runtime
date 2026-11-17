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
