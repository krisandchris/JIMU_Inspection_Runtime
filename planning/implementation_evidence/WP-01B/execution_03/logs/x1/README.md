# X1 shared condition data and complete Policy catalogue

PLAN-4-A/CONTRACT_FIELDS11, explicit D01A + local-Tool-selector amendment. This is
subagent engineering evidence, not wholeWP acceptance. Original execution02 not edited.

Goal/scope: strict frozen recursive ConditionExpr, scalar/list Literal(ValueContract),
exact and local selector data, owner-specific structure/domain checks, complete16-way
PolicyArtifact and registration/resolve plus exact ToolSkip subtype check. Existing14
specialized artifacts/NonExpressionPolicyArtifact remain. No new dependencies or A core
semantics. Local selector omits Plan ref and retains exact Tool/FactSchema pins.

Public interface: validate_condition_data(raw,*,input_domain)->ConditionExpr;
condition_references(expr)->ConditionReferences(context_selectors,fact_selectors,
local_tool_selectors), preorder preserving duplicate occurrences. ConditionDataError
contains a bounded code. ConditionInputDomain/ComparisonOperator/ProducerCardinality live
in definition_enums.py. C/A were notified of stable interfaces before downstream work.

Root32/depth,512occurrences,64logical/in-literal budgets; active-ancestor cycle check
counts shared subtrees repeatedly. Oversized logical lists are rejected before tuple
copy or traversal-stack expansion. Literal's own A ValueContract validates closed enum/
list conformance; Record literals reject. Refs may have unresolved Boolean return types.
No operand inference, units/nominal compatibility, evaluation, simplification, truth
folding, operator registry, RuleDefinition or RuleSetDefinition is implemented.

Validation:269targeted tests pass,0fail/error/skip; final_pytest_corrected.json/xml.
These include new X1 tests and original14Policy tests. Generated-model Draft202012
shape checks and explicit cross-field/data checks are distinct; Schema alone does not
prove recursive budgets/owner-domain restrictions or expression type correctness.
Own4source mypy passes, Ruff/format13maintainedfiles pass. Fullsrc mypy was actually run:
last result has2errors in A's concurrently adapting definitions.py, not in X1 files.
No full-srcPASS claimed; final package regression after writers release is still needed.
Exporter/generated/index were deliberately not modified; E2 owns the old partial markers.

Failures preserved: initial Ruff forward annotations; initial own mypy sequence narrowing/
nested-Annotated alias issue, resolved with private named tagged aliases (no ignores).
The minimal -c tooling probes used isolated config only to reproduce mypy1.18alias bug;
project acceptance checks used the real pyproject config. Primary's pre-expansion budget
Finding was first reproduced2failures then fixed. A new unit/contract basename collision
caused collection error; only the new unit test was renamed to *_binding.py, and the full
targeted set rerun. No test deleted/skipped/xfail and old coverage retained/strengthened.
Exact commands/cwd/UTC/exit/stdout/stderr/JUnit are in JSON logs. Before original bytes and
hashes are retained in before/ + before_sha256.json; final12file hashes recorded separately.

Contract impact: new AST/Policy branches and additive enums. Existing14wire bytes/classes
remain compatible; partial14name stays partial. New complete registries accept all16with
nested policy_kind tagged selection, no input filtering. No published release/data migration
was run. Security: no credentials, executable hooks, shell/model URLs or dynamic code.
No Model/GPU/DB/Rule/Workflow execution, Git mutation or requirements-platform changes.
Known limitations: selector use-site availability, operand typing/nominal units, real
activation, Producer/authority admission and Bundle authenticity are downstream responsibilities.
Runtime acceptance=N/A. X1 targeted readinessPASS; Primary independent review pending.
