# O1 handoff — pending Primary review

Scope: CONTRACT_FIELDS.md frozen §4.1/6.1, only local capability/Prompt shapes
and literal syntax. Worktree: JIMU_Inspection_Runtime_WP01B. No shared/B/C/D/A
files, dependencies, commit/push or runtime were modified by this task. Product,
test and fixture paths and SHA-256 are in after_sha256.json. Imported interface
snapshot hashes are separate, so later integration can detect dependency changes.

## Actual verification

132 targeted tests passed, zero failures/errors/skips (11_pytest.json/11_junit.xml).
Ruff check, mypy src (26 files), Ruff format check, 24 generated model/alias Schema
checks passed (12–15). Environment/packages are in 16_environment.json; RFC8785
was already installed by the authorized C owner, not installed by O1.
Each numbered command JSON records exact argv, cwd, UTC start/end, exit, stdout/stderr.

Original failures retained: 04_mypy.json (2 errors in Prompt Schema JSON typing and
TypeAdapter result annotation); 07_ruff_check.json (2 import-order findings after
that fix). Both resolved in owned files without ignores, weaker constraints or skips.
Runtime: RUNTIME_NOT_RUN. No full-suite, actual model/tool or GPU test was run.

## Exported interfaces

capabilities.py: FacetDefinition, QuestionDefinition, ProfileDefinition,
FacetMetadataEntry, ModelGroundingBinding, ToolGroundingBinding, GroundingBinding,
GroundingInputBinding and supporting exact reference/tuple aliases.
GroundingInputBinding inherits the existing B InputBinding wire and validation,
with a genuine context/image-only source union; its Schema exposes the restriction.
It also preserves required=>block and rejects duplicate/prefix-overlapping target
writes. Shape validity does not prove known target IO coverage or compatibility.

Profile Questions/Facets/RuleSets may be empty. Facet.fact_schema_refs and
Question.facet_refs remain nonempty. Exact refs and conflicting digest duplicate
lists reject; Question has one primary output and no reverse Profile membership.
Facet metadata is a bounded namespace/schema/RecordValue declaration, not raw JSON;
metadata namespace and exact vocabulary/member tags have their own uniqueness keys.

prompts.py: PromptVariable, PromptTemplateDefinition, PromptVariableName, TemplateText,
PromptPurpose, PromptSource, PromptTemplateContentRef, ResponseSchemaContentRef,
and placeholder_names(template_text)->tuple[str,...]. The parser returns named uses
in source order, preserving repeated uses and ignoring doubled-brace escapes.
It does not render or resolve variables. Template syntax, source-to-Schema kind and
strict required/on_missing pairing are enforced in Pydantic and generated Schema.
Purpose reuses the approved ModelCapability subset, with no shared enum mutation.

## Explicit deferred checks

These shape models do not assert injected catalogue membership/content authenticity,
Facet exact Schema intersection, Profile Question/Facet completeness, FactSchema
scope/producer compatibility, metadata conformance to trusted type, vocabulary domain
or member existence, Model/Tool capability or grounding target IO correctness, Policy
subtypes or complete QuestionSelectionPolicy/Applicability admission.

Partial refs/variables are local declarations only. D-owned complete injected-graph
validation must check references/cycles, consistent repeated declarations across
partials, declared/used set equality, inherited context variables, Question allowlist
and ContextSource/owner ambiguity. Local shapes intentionally allow an unresolved
placeholder whose declaration might come from a partial; no closure PASS is implied.
Optional-null, copied/nested instance taint and upper bounds have dedicated regressions.
A numeric representation differences are unchanged. There are no data migrations.
No overall WP acceptance or downstream execution authorization follows from O1.
