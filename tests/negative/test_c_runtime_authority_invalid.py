"""Single-field mutations must fail even when each record retains its wire shape."""

import json

import pytest
from fixtures.wp01b.x3.integration import parse
from fixtures.wp01c.nodes.cases import content, ref
from fixtures.wp01c.runtime_authority.cases import check, documents

from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords


def _validate(claim, actual):
    return check(
        parse(claim), RuntimeAuthorityRecords.model_validate_json(json.dumps(actual))
    )


def _swap(root, path, value):
    for key in path[:-1]:
        root = root[key]
    root[path[-1]] = value


@pytest.mark.parametrize(
    "target,path,replacement,diagnostic",
    [
        (
            "actual",
            ("execution", "pins", "release_id"),
            "different-release",
            "Release and Bundle Manifest pins differ",
        ),
        (
            "actual",
            ("manifest", "artifacts", 4, "content_ref", "digest"),
            "b" * 64,
            "producer catalogue differs",
        ),
        (
            "actual",
            ("eligibility", "release_id"),
            "different-release",
            "actual Execution and context",
        ),
        (
            "actual",
            ("objects", 0, "entity_ref"),
            content("entity", "different"),
            "Object entity differs",
        ),
        ("actual", ("node_runs", 0, "node_id"), "N05", "N04 NodeRun"),
        ("actual", ("attempts", 0, "attempt_number"), 2, "Attempt history"),
        (
            "actual",
            ("question_selections", 0, "context_snapshot_digest"),
            "b" * 64,
            "matching context pins",
        ),
        (
            "actual",
            ("observations", 0, "object_ref"),
            ref("object", "different-object"),
            "primary Fact must belong",
        ),
        (
            "actual",
            ("node_runs", 0, "output_refs"),
            [ref("evidence", "other")],
            "absent from actual NodeRun output refs",
        ),
        (
            "actual",
            ("model_invocations", 1, "actual_provider", "model_version"),
            "9.9.9",
            "outside the Execution pin",
        ),
        (
            "actual",
            ("facts", 0, "value", "value"),
            True,
            "candidate Fact claims differ",
        ),
        (
            "claims",
            ("history", "node_runs", 0, "node_id"),
            "N03",
            "NodeRun claims differ",
        ),
        (
            "claims",
            ("history", "observation_results", 0, "arrival_qualification"),
            "late",
            "ObservationBatch claims differ",
        ),
        (
            "claims",
            ("history", "question_selections", 0, "eligibility_qualification"),
            "unresolved",
            "QuestionSelection claims differ",
        ),
        (
            "claims",
            ("history", "proposed_selections", 0, "selected_attempt_id"),
            "different",
            "authority revision claims differ",
        ),
        (
            "claims",
            ("history", "candidates", "entries", 0, "raw_bytes"),
            "{}",
            "invalid_candidate_history",
        ),
    ],
)
def test_scope_or_identity_substitutions_fail(target, path, replacement, diagnostic):
    claim, actual = documents()
    _swap(actual if target == "actual" else claim, path, replacement)
    with pytest.raises(ValueError, match=diagnostic):
        _validate(claim, actual)


def test_tool_requires_actual_witness_and_pinned_declaration():
    claim, actual = documents("tool")
    actual["tool_executions"][0].pop("authority_observation")
    with pytest.raises(ValueError, match="Tool step claims differ"):
        _validate(claim, actual)
    claim, actual = documents("tool")
    actual["tool_executions"][0]["invocation_origin"]["producer_declaration_ids"] = [
        "question.primary"
    ]
    with pytest.raises(ValueError, match="exact producer declaration"):
        _validate(claim, actual)


def test_tool_cannot_substitute_declared_version_for_unavailable_actual():
    claim, actual = documents("tool")
    tool = actual["tool_executions"][0]
    tool.pop("authority_observation")
    tool.pop("actual_implementation_version")
    tool.update(
        implementation_version_resolution="unavailable",
        implementation_version_unavailable_reason="Implementation version could not be verified",
        generated_fact_refs=[],
    )
    with pytest.raises(ValueError, match="Tool step claims differ"):
        _validate(claim, actual)


def test_legacy_system_event_cannot_be_enriched_from_claimed_optional_absence():
    claim, actual = documents("system")
    actual["execution_events"][0]["payload"].pop("authority_output")
    with pytest.raises(ValueError, match="System output claims differ"):
        _validate(claim, actual)


def test_release_without_approval_record_cannot_supply_authority():
    claim, actual = documents()
    actual["release"].pop("approval_receipt")
    actual["release"]["status"] = "accepted"
    with pytest.raises(ValueError, match="Release lacks its approval receipt"):
        _validate(claim, actual)
