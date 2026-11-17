# Dependency Baseline and Compatibility Policy

## 1. Principle

This document defines dependency selection policy, not a claim that any exact package matrix has already been tested.

Do not install `latest` blindly. Before pinning, create a compatibility matrix for the target OS, NVIDIA driver, CUDA runtime, Python, PyTorch, vLLM, Transformers, Grounding and SAM3 implementation.

## 2. Proposed dependency groups

### Core API/domain
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

## 3. Compatibility gates

Before locking:
1. Record Ubuntu version and kernel.
2. Record NVIDIA driver and `nvidia-smi`.
3. Confirm CUDA runtime compatibility.
4. Confirm PyTorch wheel/runtime compatibility.
5. Confirm vLLM supports selected Python/CUDA/PyTorch combination.
6. Confirm model architecture and remote/local structured-output behavior.
7. Confirm SAM3/Grounding dependencies do not conflict with vLLM environment.
8. Test co-resident VRAM peak on RTX 4090.
9. Record container image digests and lock file hashes.

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

- uv vs Poetry for package management.
- Alembic vs explicit SQL migration runner.
- MinIO vs external S3 service.
- Redis required in first deployment or deferred.
- vLLM and vision worker same image or separate images.
- exact CUDA/PyTorch/vLLM/SAM3 versions.
