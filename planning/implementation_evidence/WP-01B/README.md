# WP-01B — G1 interim evidence

This is an interim contract-review packet, not WP-01B final acceptance.
Base commit: 51a10416d882fe85e5a1cfc5440eac6498fc48f4.
Branch: dev/wp-01b-definition-fact-registry.
Original main files are unchanged. No Commit/Push/PR/Merge.

Latest authorization is preserved in USER_AUTHORIZATION.md.
source_baseline.json covers 143 tracked and 27 known untracked original files.
worktree_transfer.json records their byte-preserving transfer; before_planning/
preserves the previous plan/review/hash packet and original ADR before edits.
g1_reviews/ stores unedited native Sol final replies; agent_sessions.json contains
only safely selected identity/model/effort/cwd metadata.

G1-D01 needs a user scope decision: full existing conditional Policy/Producer
contracts require AST data, while the current Goal forbids AST in B. A narrow
data-model exception and an explicitly reduced delivery alternative were presented.
D02 scope is explicitly authorized. D03/D04 support is authorized, but some exact
field contracts remain C2 technical work; they are not additional user decisions.
The plan marks these honestly instead of claiming the four gates have passed.

Code Acceptance: NOT_IMPLEMENTED.
Test Acceptance: TEST_NOT_RUN.
Runtime Acceptance: N/A / RUNTIME_NOT_RUN.
Independent Review: STATIC_REVIEW_ONLY / CONDITIONAL.
Overall: INCOMPLETE. Goal is not complete.

There is no FINAL_ACCEPTANCE_REPORT.md claiming engineering completion.
No project pytest, schema validation, Ruff or mypy was executed. Historical 324
passed belongs to WP-01A only. Hash/Git/document checks are not functional tests.
The Goal explicitly instructs a pause when a business/scope decision is required;
after saving this packet, that conditional pause applies until D01 is answered.
