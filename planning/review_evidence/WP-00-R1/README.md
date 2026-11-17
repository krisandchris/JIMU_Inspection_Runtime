# WP-00-R1 document revision evidence

Status: STATIC_REVIEW_ONLY / TEST_NOT_RUN / RUNTIME_NOT_RUN.
This archive preserves document history; it is not implementation authorization,
a Runtime Bundle, test output or a deployment artifact.

## Contents

- `before/`: exact bytes of all 16 project files present before this revision,
  including the complete original starter pack and the first WP-01 plan. No
  original content was deleted from this evidence copy.
- `before_sha256.json`: pre-edit paths, byte sizes and SHA-256 values.
- `after_sha256.json`: post-edit sizes and hashes for the same 16 paths. It excludes
  audit files themselves to avoid recursive/self-referential audit hashes.
- `changes.patch`: full unified diff for the six edited document paths. `before/`
  and `after/` diff labels are logical sides, not a Git repository or branches.

The original PACKAGE_MANIFEST.json is deliberately unchanged. Its hashes describe
the original distribution, not the revised specs/ADR. Original values remain
auditable against the before copies; do not regenerate it to conceal revision.

## Observed static operations

The repository-state queries were read-only:

| Command | Exit code | Output |
|---|---:|---|
| `git status --short --branch` | 128 | fatal: not a git repository; discovery stopped at /mnt boundary |
| `git rev-parse --show-toplevel --abbrev-ref HEAD HEAD` | 128 | same error; branch/HEAD/working-tree state unavailable |
| `rg --files --hidden -g '!.git/**'` | 0 | 16 pre-revision document files |

File reads used cat/sed/rg. Copies and document edits used Python standard library
and apply_patch. A Python standard-library audit (`hashlib`, `json`, `difflib`,
`pathlib`, `datetime`) generated the after inventory and patch, exit 0, output:

```text
Archived copy hash mismatches: []
Changed original paths: ["planning/ARCHITECTURE_DECISIONS.md", "planning/WP-01_IMPLEMENTATION_PLAN.md", "specs/01_Runtime_Domain_Model_V1.0.md", "specs/02_Runtime_Bundle_Specification_V1.0.md", "specs/03_Six_Node_Execution_Contract_V1.0.md", "specs/04_Runtime_Technical_Architecture_V1.0.md"]
Unchanged original paths: 10
Diff lines: 994
Audit only; no tests or runtime commands executed.
```

No pytest/Schema/lint/typecheck/test assertion was executed. Hashing is document
preservation evidence only. No dependencies were installed, no Git repository was
initialized, no code/DB/provider/GPU/service was run and nothing was committed.

## Revision review boundary

The working plan §3.2 maps C01–C11 to candidate resolutions; §10.1 maps D01–D10;
§12 records compatibility differences, planned edge cases and final acceptance
gates. All decision candidates remain PROPOSED / PENDING_USER_APPROVAL. The
revision self-review is CONDITIONAL pending independent review and explicit user
authorization for WP-01. Exact signature algorithms/trust and Bundle execution
remain later work packages. The historical plan's successful static operations
are not present-run tests and were not rerun.

Rollback, if explicitly chosen later, can restore only the six edited document
paths from their before copies; retain this audit history and unrelated files.
No rollback or deletion was performed here.
