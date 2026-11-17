# JIMU Inspection Runtime — Codex Starter Pack

**Package:** `jimu-inspection-runtime-starter`  
**Version:** `1.0.0`  
**Status:** Architecture baseline for independent review; not implementation authorization.

## 1. Mission

Build a new, independent, configuration-driven multimodal inspection runtime. The runtime is built first. The existing JIMU requirements platform is explicitly out of scope for modification in this project phase. A future adapter may translate approved upstream configurations into this runtime's native contract; V4 is not the runtime domain model.

## 2. Confirmed operating constraints

| Item | Baseline |
|---|---|
| Deployment | One GPU server, Docker Compose |
| GPU | NVIDIA RTX 4090 |
| Model strategy | Hybrid; cloud inference is primary, local inference/tooling is available |
| Edge | Capture, device/context metadata, upload only |
| Input | One RGB image per task in V1 |
| Workflow | Deterministic six-node pipeline |
| First end-to-end scenario | Non-motor vehicle illegal parking |
| Requirements platform | No changes in V1 runtime build |

## 3. Six-node responsibilities (normative)

1. **N01 Context Builder** — resolve device/camera/region and immutable task context.
2. **N02 Omni-JEV Profile Discovery** — output registered Profile hits and their configured Question sets only. It must not output boxes, Object IDs, instance associations, or violation decisions.
3. **N03 Grounding** — independently ground each hit Profile and create Object instances. It must not match instances to N02 (N02 has no instances), reselect Questions, or decide violations.
4. **N04 VLM Observation** — one normal-path joint VLM request per Object for all effective Questions; validate and persist each Question's Fact independently.
5. **N05 Tool Execution** — execute the pre-bound Profile Tool Plan, independent of N06 violation predictions; produce Tool Facts and evidence.
6. **N06 Decision** — predict candidate violation categories, then perform deterministic Rule evaluation. Candidate prediction must not silently suppress applicable mandatory Rules.

## 4. Package map

- `AGENTS.md` — mandatory Codex project instructions.
- `specs/01_Runtime_Domain_Model_V1.0.md` — definition and runtime domain contracts.
- `specs/02_Runtime_Bundle_Specification_V1.0.md` — immutable bundle, validation and release contract.
- `specs/03_Six_Node_Execution_Contract_V1.0.md` — six-node I/O, state, errors and retries.
- `specs/04_Runtime_Technical_Architecture_V1.0.md` — services, persistence, model/GPU scheduling and deployment.
- `planning/DEVELOPMENT_ROADMAP.md` — work packages and gates.
- `planning/DEPENDENCIES.md` — dependency policy and compatibility validation.
- `planning/ARCHITECTURE_DECISIONS.md` — confirmed/proposed/open decisions.
- `planning/TEST_STRATEGY.md` — testing and evidence policy.
- `examples/nonmotor_vehicle_illegal_parking/` — illustrative Bundle fragments and task/result contract examples.

## 5. First Codex session

Codex must read `AGENTS.md` and all four specifications before acting. The first session is limited to:
- inspect the newly initialized repository (if one exists);
- identify contradictions, missing schemas and implementation blockers;
- produce a WP-01 implementation plan and contract checklist;
- propose file/module boundaries and tests;
- ask for approval before implementing WP-01.

Do not implement the whole platform in the first session. Do not modify the existing JIMU requirements-platform repository. Do not create GitHub resources or push code unless separately authorized.

## 6. Status semantics

This package distinguishes:
- **CONFIRMED** — explicitly established by the user or current project baseline.
- **PROPOSED** — recommended architecture, requires review before it becomes binding.
- **OPEN** — unresolved; do not invent a value.
- **IMPLEMENTED** — code exists and has evidence; not synonymous with runtime acceptance.
- **TEST_NOT_RUN / RUNTIME_NOT_RUN** — validation was not performed.

## 7. Important limitation

Examples are illustrative contracts, not production evidence, trained model outputs, measured GPU capacity, or verified provider compatibility. Concrete model names, model versions, quantization, concurrency, latency and throughput remain deployment decisions until tested on the target host.
