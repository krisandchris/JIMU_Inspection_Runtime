# Test Strategy and Evidence Standard

## 1. Test layers

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
