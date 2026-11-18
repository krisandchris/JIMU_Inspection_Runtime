# P1 independent non-expression Policy delivery

Scope: frozen CONTRACT_FIELDS6.3 plus approved finite compatibility interfaces.
Code review pending Primary; RUNTIME_NOT_RUN / runtime acceptance N/A.

Fourteen individually complete Policy shapes and specialized root artifacts are delivered.
PolicyKind remains16; ApplicabilityPolicy/ToolSkip and the complete PolicyArtifact union
are deliberately absent (D01 blocked). NonExpressionPolicyArtifact is explicitly partial,
not an alias presented as the normative full catalogue. All behavior/budgets explicit.

policies.py contains closed QuestionSelection, Unknown, QuestionValidation, Evidence,
ToolExecution, ToolFailure, ResourceRequirements(cpu/remote/gpu), Provider(remote/local
exception), Fallback(disabled/evaluated), Rule, Conflict, Coverage, ContextProvenance and
Execution models. Resource/model placement has no inferred route. Policy refs remain flat.

validation/policies.py provides caller-pinned PolicyRegistration, PromptRegistration,
ModelBindingRegistration and RetentionVocabularyRegistration. Pure functions validate
model Policy subtype/logical identity/prompt, exact fallback transition+trigger+version
coverage, local exception coverage/time/scope; Tool budget tightening/nested vision binding;
step FailurePolicy requirement; Profile Question partition/explicit additional refs;
Execution Policy refs; Context derivation graph/external owner roots; retention Vocabulary.
ReviewMetadata is trusted resolver output, NOT report-hashed bytes or genuine quality
proof. Version-product coverage is checked by exact allowed combinations/cardinality,
without enumerating an unbounded Cartesian product. validation_at is explicit.

TEST_PASSED:121 tests, no skip/fail/error; final_pytest.json/xml. Generated Draft202012
Schema structure and strict positive/negative cases run in those tests, including branch
presence and all14 artifact roots. Ruff/check/format and mypy3sourcefiles exit0.
Exact command arrays/cwd/UTC timestamps/output are preserved in final_*.json.
Initial failures and expanded-test failure retained. environment.json records real versions;
after_sha256.json identifies the files validated. No dependency/lock changes by Sol-B.
M1 remediation also passed34tests and its own static checks; see siblingm1 logs.

Limits: no actual report authentication/independent quality measurement, model/tool call,
host capacity, provider route, derivation execution, Rule AST/evaluation, Workflow,
full producer completeness, full Schema/Bundle closure, DB/migrations, commit/push/Git
mutation. Reports retain these as deferred/incomplete where relevant; Policy shape is
not complete Runtime admission. Partial-object mapping coverage remains static only.

All synthetic fixtures are test data, not approved production values. Security: no secrets,
endpoints/executable hooks or dynamic code accepted. Data migration impact:none.
Acceptance:targeted checksPASS; IndependentReviewPENDING; wholeWP not evaluated.
