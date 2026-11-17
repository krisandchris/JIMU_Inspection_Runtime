# 04 — Runtime Technical Architecture V1.0

**Status:** Proposed architecture baseline  
**Deployment:** Single RTX 4090 server, Docker Compose  
**Inference:** Hybrid, remote-first

**WP-00-R1 scope clarification (PROPOSED / PENDING_USER_APPROVAL):** WP-01 is
limited to Domain Model, an in-memory Fact Schema Registry, Rule AST static type
checking, generated JSON Schema and CPU-only tests. None of the services below,
Rule runtime evaluation, Bundle compiler/admission, Provider adapters or durable
scheduling is part of WP-01. CPU-only dependency compatibility/lock is a separate
candidate from the later GPU worker matrix; no GPU compatibility is claimed by
contract tests. This revision authorizes document changes only. Original bytes
are retained under `planning/review_evidence/WP-00-R1/before/`.

## 1. Architecture shape

V1 uses a modular control-plane application plus separately managed inference processes. Do not split into many microservices before operational need is demonstrated.

Logical components:
- API and Task Manager;
- Bundle/Release Manager;
- Workflow Engine and durable Scheduler;
- Model Gateway;
- Vision/Tool Workers;
- PostgreSQL;
- object storage;
- optional Redis;
- observability stack.

## 2. Recommended stack (selection policy)

| Concern | Candidate | Rule |
|---|---|---|
| API | FastAPI | Python API, OpenAPI |
| Contracts | Pydantic v2 | Authoritative schemas |
| ORM | SQLAlchemy 2 | Keep domain independent of ORM |
| Database | PostgreSQL | Durable state authority |
| Object store | MinIO/S3-compatible | Large evidence objects |
| Cache/coordination | Redis | Never sole durable task store |
| Local LLM/VLM | vLLM | Only after model/hardware compatibility check |
| Remote model | OpenAI-compatible adapter | Remote-first policy, strict response validation |
| Vision | PyTorch-based workers | Grounding/SAM/other adapters |
| GPU optimization | TensorRT/ONNX as measured | Not a V1 prerequisite |
| Workflow | Explicit Python state machine | Durable state in PostgreSQL |
| Telemetry | OpenTelemetry + Prometheus/Grafana | Trace/metrics correlation |
| Deployment | Docker Compose | Single-host V1 |

Exact versions are intentionally not hardcoded. GPU worker versions must be
selected after testing the target NVIDIA driver, CUDA runtime, PyTorch, vLLM,
Transformers, model code and SAM3 dependencies. D01 separately proposes a
CPU-only contract environment/matrix for WP-01; it must not claim GPU validation.
Lock files and accepted image digests follow the applicable compatibility gate;
Git commits still require separate user authorization.

## 3. Process boundaries

### API process
Handles authentication, Task submission/query/cancel, Bundle admission and Release administration. No model weights loaded.

### Workflow/Scheduler process
Claims durable jobs, advances node state, applies retry policies, enforces concurrency budgets and persists events.

### Model Gateway
Routes by capability and approved ModelBinding. Adapters normalize provider responses into internal contracts. Records actual provider/model/deployment and request/response digest.

### Local vLLM process
Serves explicitly configured local models. Must be isolated from API process and monitored for memory/health.

### Vision Worker
Runs Grounding/SAM/other GPU vision models. Model loading is process-scoped and shared; nodes must not repeatedly load weights.

### CPU Tool Worker
Runs geometry, transformations and CPU-friendly post-processing where appropriate.

## 4. Remote-first hybrid inference

- Remote API is primary where configured and quality/cost/privacy policy permits.
- Local vLLM is used only for explicitly assigned capabilities, approved fallback, privacy-sensitive workloads or unavailable remote capability.
- Do not silently substitute a different model.
- Fallback requires compatible schema, validated quality and explicit policy.
- Record configured binding and actual invocation separately.
- Secrets are injected at deployment and never enter Bundle or logs.
- Remote requests need timeout, bounded retries, rate limits, request IDs and provider error normalization.

## 5. RTX 4090 resource management

The GPU is shared by local vLLM and vision workers. No fixed concurrency or memory budget is asserted in this document.

Before enabling concurrent GPU workloads:
1. inventory exact model weights and precision;
2. measure peak VRAM for each worker;
3. measure co-resident peak VRAM;
4. test cold start and model reload;
5. test OOM and worker restart;
6. set admission/concurrency limits with safety headroom;
7. record the tested hardware/software matrix.

Use one owner/worker pool per loaded model where practical. CPU preprocessing and geometry should not occupy GPU. Scheduler must enforce per-model and per-tool concurrency.

## 6. Persistence and storage

PostgreSQL stores:
- definition versions and refs;
- Bundle/Release/activation;
- Task/Execution/NodeRun/Attempt/Event;
- Object/Fact/Decision metadata;
- ExecutionCoverage and per-Profile coverage provenance (separate from completion and business result);
- ModelInvocation/ToolExecution metadata;
- Evidence metadata and hashes;
- replay records.

Object storage stores:
- original images;
- crops and visualizations;
- masks;
- large tool outputs;
- model request/response artifacts where policy permits.

Redis may cache Bundle indexes and coordinate short-lived worker signals. PostgreSQL remains durable state authority.

Object-store writes are not atomic with DB transactions. Use staged asset state, upload, hash verification, finalize and compensating cleanup.

## 7. Workflow durability

Use PostgreSQL-backed job claims, leases and fencing in V1. Ensure claim/lease/attempt transitions are transactional. Define worker heartbeat, lease expiry and stale-worker fencing.

Do not add Temporal/Kafka/Kubernetes without a documented requirement, load evidence and approval. The architecture should expose interfaces that allow later replacement of the scheduler/transport.

## 8. Security

- Separate human and service identities.
- Runtime service identity uses minimum scopes.
- Bundle signing key trust is configured outside Bundle.
- No credentials in repository or artifacts.
- Restrict remote URL fetches to prevent SSRF.
- Validate uploaded image type, size, dimensions and decompression limits.
- Restrict evidence access by project/tenant/task scope.
- Audit Bundle admission, activation, task cancellation and result access.
- Protect internal services from public ingress.
- Redact sensitive prompt/request fields from logs where required.

## 9. Observability

Every request carries trace_id and correlation IDs. Emit spans for Task, Execution, NodeRun, Attempt, ModelInvocation and ToolExecution.

Minimum metrics:
- task accepted/completed/failed;
- queue wait and task duration;
- per-node duration/failure/retry;
- model latency/error/token usage;
- tool duration/error;
- GPU utilization/VRAM/OOM;
- evidence upload/verification failures.

Business metrics (Profile recall, Grounding recall, Fact accuracy, Decision precision/recall, false acceptance) are computed by evaluation against reviewed truth, not from model self-report.

## 10. API surface (proposed)

`/api/v1`
- `POST /bundles`
- `GET /bundles/{bundle_id}`
- `POST /bundles/{bundle_id}/validate`
- `POST /releases`
- `GET /releases/{release_id}`
- `POST /releases/{release_id}/accept`
- `POST /releases/{release_id}/approve`
- `POST /releases/{release_id}/activate`
- `POST /releases/{release_id}/deprecate`
- `POST /inference/tasks`
- `GET /inference/tasks/{task_id}`
- `POST /inference/tasks/{task_id}/cancel`
- `GET /inference/tasks/{task_id}/result`
- `GET /executions/{execution_id}`
- `GET /executions/{execution_id}/trace`
- `POST /executions/{execution_id}/artifact-replay`
- `POST /executions/{execution_id}/inference-replay`
- `GET /evidence/{evidence_id}`

All routes are proposals until Pydantic contracts and auth scopes are approved. Do not implement an API solely from this route list.

## 11. Edge boundary

V1 edge client captures RGB image, device/camera IDs, timestamp/clock metadata and region reference; uploads image and metadata. Cloud Runtime performs all six nodes. Future edge inference results must be represented as explicitly versioned, provenance-bearing inputs and cannot silently replace cloud facts.

## 12. Deployment

Compose services may include:
- api
- workflow-worker
- scheduler
- vision-worker
- vllm (optional if local model binding enabled)
- postgres
- minio
- redis (optional)
- prometheus
- grafana

GPU device access is granted only to required services. Persistent volumes and backup/restore procedures are mandatory. Pin image digests for accepted deployments.

## 13. Non-functional validation

Do not claim throughput, P95, concurrency, GPU utilization or cost until measured. Establish targets after:
- model/provider selection;
- image resolution distribution;
- tool workload profile;
- remote API quota/latency;
- measured GPU memory;
- representative task concurrency.

## 14. Future integration

A separate adapter may translate approved requirements-platform artifacts into the native Bundle contract. The adapter is not part of V1 runtime core. No direct database sharing. No direct import of V4 domain types into runtime domain packages.
