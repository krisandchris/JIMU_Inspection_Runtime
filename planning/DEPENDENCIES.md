# Dependency Baseline and Compatibility Policy

## 1. Principle

This document defines dependency selection policy, not a claim that any exact package matrix has already been tested.

WP-00-R2 candidate, baseline `46388ce1d8f03e0b122b3038a5930eb0aa3ceb01`:
D01 remains PROPOSED / PENDING_USER_APPROVAL. No dependencies were installed or
compatibility tests run. Do not install latest blindly. Pinning uses two separate
gates: WP-01 CPU contract environment and WP-04 target GPU worker environments.
This clarifies the applicable matrix; it does not waive verification or AGENTS.md.

## 2. Proposed dependency groups

### WP-01 CPU-only contract core (candidate)
- Python 3.12 (candidate; exact patch still unverified)
- Pydantic v2 (bounded reviewed range, no exact pin claimed)
- pytest
- jsonschema
- Ruff
- mypy

uv is the D01 package-manager candidate. One CPU-only lock records exact reviewed
versions after authorized compatibility verification; no FastAPI/ORM/httpx/SDK/
PyTorch/CUDA/vLLM/SAM3 packages enter this environment. Hypothesis/coverage tooling
may be proposed separately; they are not unapproved mandatory additions.

### Later API/infrastructure environments (outside WP-01)
- Python 3.12 (candidate; verify library support)
- FastAPI
- Pydantic v2
- SQLAlchemy 2
- psycopg 3
- Alembic (proposed migration manager)
- httpx
- uvicorn

### Workflow and data
- PostgreSQL driver
- Redis client (optional)
- MinIO/S3 SDK
- OpenTelemetry API/SDK and instrumentation
- structured logging library

### Model gateway
- OpenAI-compatible HTTP adapter using httpx
- vLLM client/API integration (only if local serving is enabled)
- provider-specific SDK only behind adapter boundary

### Vision workers
- PyTorch
- Transformers (as required by selected model)
- OpenCV
- SAM3/Grounding dependencies selected from actual implementation
- ONNX Runtime/TensorRT only after profiling and compatibility validation

### Testing and quality
- pytest
- pytest-asyncio
- httpx test client
- Hypothesis (recommended for schema/AST property tests)
- Ruff
- mypy or pyright (choose one)
- coverage tooling

## 3. Two-stage compatibility gates (PROPOSED)

### Gate CPU — WP-01 before CPU lock acceptance

1. Record OS/architecture/kernel, Python patch version and interpreter origin.
2. Review bounded Python/Pydantic v2/pytest/jsonschema/Ruff/mypy compatibility and package-manager version; generate the CPU lock only after installation/test authorization.
3. Verify strict Pydantic + generated 2020-12 schemas, offline schema validation, static typechecking and deterministic generation in that environment.
4. Record exact commands/exit codes, dependency versions, lock hash and clean-rebuild evidence. Until actually executed: TEST_NOT_RUN / compatibility UNVERIFIED.
5. Mark GPU matrix entries NOT_APPLICABLE_TO_CPU_GATE / DEFERRED_TO_WP04, never PASS by inference. No model SDK/API service or GPU library in the domain dependency graph.

### Gate GPU — WP-04 before GPU image/lock acceptance

1. Inventory exact target NVIDIA driver, GPU, OS, CUDA runtime and container base.
2. Verify Python/PyTorch wheel/CUDA compatibility for actual selected models.
3. Verify vLLM, Transformers, model code, SAM3/Grounding and Tool worker support; test separately managed model/vision environments where dependencies conflict.
4. Measure model cold start/reload, peak VRAM, co-resident peak, worker concurrency and OOM/restart behavior on the approved host. No invented RTX4090 budget.
5. Verify provider/model structured-output contracts and explicitly evaluated fallback bindings; real API/model costs require their own approval.
6. Record separate environment locks/image digests, measured matrix, exact commands and outcomes. CPU lock acceptance is not evidence of GPU compatibility.

GPU stacks may need separate images/environments for vLLM and vision workers to avoid incompatible dependency pinning.

## 4. Version policy

- Pin exact versions in lock files after compatibility tests.
- Pin container images by digest for accepted deployments.
- Upgrade one dependency family at a time.
- Record tested and unsupported combinations.
- Do not mix CPU-only and CUDA wheels accidentally.
- Do not commit local cache/model weights.
- Keep secrets out of dependency and build configuration.

## 5. Open choices

- uv candidate vs alternatives for package management: D01 PROPOSED / PENDING_USER_APPROVAL.
- Alembic vs explicit SQL migration runner.
- MinIO vs external S3 service.
- Redis required in first deployment or deferred.
- vLLM and vision worker same image or separate images.
- exact CUDA/PyTorch/vLLM/SAM3 versions.
