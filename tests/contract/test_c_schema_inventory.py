"""Validate WP-01C fixtures against the export and WP-01B against its sealed bytes."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fixtures.wp01c.applicability.cases import wire_case
from fixtures.wp01c.nodes.cases import cases as node_cases
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.bundle.integrity import canonical_json, sha256_digest
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.runtime.executions import NodeRun
from scripts import generate_schemas as exporter

_ROOT = Path(__file__).parents[2]
_FIXTURES = _ROOT / "tests/fixtures/wp01c"
_BASELINE = (
    _ROOT / "planning/implementation_evidence/WP-01B/execution_03/after_sha256.json"
)
_C_BASELINE = (
    _ROOT
    / "planning/implementation_evidence/WP-01C/primary_freeze_a1aec5dc27cc"
    / "product_sha256.json"
)
_GENERATED = _ROOT / "schemas/generated"
_PERSISTED_ROLES = {"definition_root", "runtime_root"}
_PERSISTED_UNIONS = {
    "runtime.selection.QuestionSelectionRecord": (
        "AllDeclaredSelectionRecord",
        "ContextRuleSelectionRecord",
        "ModelAdvisorySelectionRecord",
        "FallbackSelectionRecord",
    ),
    "nodes.context.ContextBuildOutput": (
        "SuccessfulContextBuild",
        "FailedContextBuild",
    ),
    "nodes.discovery.DiscoveryOutput": (
        "DiscoveryAttemptOutput",
        "SkippedDiscoveryOutput",
    ),
    "nodes.envelopes.NodeOutputEnvelope": (
        "SucceededNodeOutput",
        "FailedNodeOutput",
        "CancelledNodeOutput",
    ),
    "runtime.replay.ReplayRun": ("ArtifactReplayRun", "InferenceReplayRun"),
}


def _read(relative_path: str) -> Any:
    return json.loads((_FIXTURES / relative_path).read_text())


def _examples() -> dict[str, dict[str, Any]]:
    release = _read("releases/release.json")
    execution = _read("executions/contracts.json")
    invocation = _read("invocations/contracts.json")
    _, applicability = wire_case("H01")
    nodes = node_cases()
    return {
        "rules.definitions.RuleDefinition": _read("rule_declarations/rule-valid.json"),
        "rules.definitions.RuleSetDefinition": _read(
            "rule_declarations/rule-set-valid.json"
        ),
        "definitions.geometry.GeometryResourceDefinition": _read(
            "geometry_replay/geometry-valid.json"
        ),
        "runtime.bundle_manifest.BundleManifest": _read(
            "releases/bundle_manifest.json"
        ),
        "runtime.bundle_manifest.ChecksumManifest": _read("releases/checksums.json"),
        "runtime.releases.AdmissionReceipt": release["admission_receipt"],
        "runtime.releases.ApprovalReceipt": release["approval_receipt"],
        "runtime.releases.ReleaseRecord": release,
        "runtime.releases.ActivationRecord": _read("releases/activation.json"),
        "runtime.intake.TaskSubmission": _read("intake/task-submission-valid.json"),
        "runtime.intake.AcceptedTask": _read("intake/accepted-task-valid.json"),
        "runtime.intake.ContextSnapshot": _read("intake/context-snapshot-valid.json"),
        "runtime.media.MediaAsset": _read("intake/media-asset-valid.json"),
        "runtime.evidence.EvidenceAsset": _read("intake/evidence-asset-valid.json"),
        "runtime.executions.ExecutionRecord": execution["execution"],
        "runtime.executions.NodeRun": execution["node"],
        "runtime.executions.Attempt": execution["attempt"],
        "runtime.executions.ExecutionEvent": execution["system_event"],
        "runtime.invocations.ModelInvocation": invocation["models"]["observation"],
        "runtime.selection.AllDeclaredSelectionRecord": invocation["selections"]["all"],
        "runtime.selection.ContextRuleSelectionRecord": invocation["selections"][
            "context"
        ],
        "runtime.selection.ModelAdvisorySelectionRecord": invocation["selections"][
            "shared"
        ],
        "runtime.selection.FallbackSelectionRecord": invocation["selections"][
            "fallback_context"
        ],
        "runtime.tool_executions.ToolExecution": invocation["tools"]["step"],
        "runtime.applicability.ProfileEligibilityEvaluation": applicability[
            "profile_eligibility"
        ],
        "runtime.applicability.RuleSetApplicabilityEvaluation": applicability[
            "rule_set_evaluations"
        ][0],
        "runtime.applicability.RuleApplicabilityEvaluation": applicability[
            "rule_evaluations"
        ][0],
        "runtime.applicability.ApplicabilityTrace": applicability["traces"][2],
        "runtime.applicability.UnresolvedObligation": applicability[
            "unresolved_obligations"
        ][0],
        "runtime.coverage.ExecutionCoverage": _read(
            "decisions/execution_coverage.json"
        ),
        "runtime.coverage.ProfileCoverage": _read("decisions/profile_coverage.json"),
        "runtime.decisions.CandidatePrediction": _read(
            "decisions/candidate_prediction.json"
        ),
        "runtime.decisions.DecisionRecord": _read("decisions/decision_record.json"),
        "runtime.perception.ObjectRecord": nodes["object_record"],
        "runtime.perception.GroundingRecord": nodes["grounding_record"],
        "runtime.observations.ObservationBatch": nodes["batch"],
        "nodes.context.ContextBuildInput": nodes["context_input"],
        "nodes.context.SuccessfulContextBuild": nodes["context_output"],
        "nodes.context.FailedContextBuild": nodes["context_failure"],
        "nodes.discovery.DiscoveryInput": nodes["discovery_input"],
        "nodes.discovery.DiscoveryProfileDiagnostic": nodes["discovery_diagnostic"],
        "nodes.discovery.DiscoveryAttemptOutput": nodes["discovery_output"],
        "nodes.discovery.SkippedDiscoveryOutput": nodes["skipped_discovery"],
        "nodes.grounding.GroundingInput": nodes["grounding_input"],
        "nodes.grounding.GroundingOutput": nodes["grounding_output"],
        "nodes.observation.ObservationInput": nodes["observation_input"],
        "nodes.tool_execution.ToolNodeInput": nodes["tool_input"],
        "nodes.tool_execution.ToolNodeOutput": nodes["tool_output"],
        "nodes.decision.DecisionInput": nodes["decision_input"],
        "nodes.decision.DecisionOutput": nodes["decision_output"],
        "nodes.envelopes.NodeInputEnvelope": nodes["input_envelope"],
        "nodes.envelopes.SucceededNodeOutput": nodes["output_envelope"],
        "nodes.envelopes.FailedNodeOutput": nodes["failed_envelope"],
        "nodes.envelopes.CancelledNodeOutput": nodes["cancelled_envelope"],
        "runtime.replay.ArtifactReplayRun": _read(
            "geometry_replay/artifact-replay-run-valid.json"
        ),
        "runtime.replay.InferenceReplayRun": _read(
            "geometry_replay/inference-replay-run-valid.json"
        ),
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, bytes]:
    return exporter.render_exports()


def test_published_b_schemas_keep_their_sealed_file_hashes(
    rendered: dict[str, bytes],
) -> None:
    sealed = json.loads(_BASELINE.read_text())["files"]
    baseline = {
        Path(path).name: digest
        for path, digest in sealed.items()
        if path.startswith("schemas/generated/") and path.endswith(".schema.json")
    }
    assert len(baseline) == 162
    assert set(baseline) == {entry.filename for entry in exporter.WP01B_INVENTORY}
    for name, digest in baseline.items():
        assert hashlib.sha256(rendered[name]).hexdigest() == digest, name
        assert hashlib.sha256((_GENERATED / name).read_bytes()).hexdigest() == digest, (
            name
        )


def test_c_index_roles_hashes_and_open_scope(rendered: dict[str, bytes]) -> None:
    index = json.loads(rendered["index.json"])
    assert index["status"] == "COMPLETE_WP01C_CONTRACTS"
    assert index["generation"]["dialect"] == exporter.DIALECT
    assert {"deterministic Rule evaluation", "Release and Bundle admission"} <= set(
        index["deferred_groups"]
    )
    entries = {item["filename"]: item for item in index["schemas"]}
    assert set(rendered) == entries.keys() | {"index.json"}
    assert len(entries) == len(exporter.INVENTORY)
    for entry in exporter.WP01C_INVENTORY:
        item = entries[entry.filename]
        assert item["source_symbol"] == entry.source_symbol
        assert item["role"] == entry.role
        assert item["sha256"] == hashlib.sha256(rendered[entry.filename]).hexdigest()
    assert (
        entries["runtime.applicability.ApplicabilityLevelTrace.schema.json"]["role"]
        == "named_union"
    )
    assert (
        entries["runtime.executions.ExecutionPins.schema.json"]["role"]
        == "runtime_component"
    )
    assert entries["runtime.executions.NodeId.schema.json"]["role"] == "foundation"
    assert (
        entries["runtime.replay.ArtifactReplayRequest.schema.json"]["role"]
        == "nonpersistent_input"
    )


def test_published_c_schema_hashes_and_revision_links(
    rendered: dict[str, bytes],
) -> None:
    baseline = {
        Path(path).name: digest
        for path, digest in json.loads(_C_BASELINE.read_text()).items()
        if path.startswith("schemas/generated/") and path.endswith(".schema.json")
    }
    assert set(baseline) == {
        entry.filename
        for entry in (*exporter.WP01B_INVENTORY, *exporter.WP01C_INVENTORY)
    }
    for filename, digest in baseline.items():
        assert hashlib.sha256(rendered[filename]).hexdigest() == digest, filename
        assert (
            hashlib.sha256((_GENERATED / filename).read_bytes()).hexdigest() == digest
        )

    index = json.loads(rendered["index.json"])
    entries = {item["filename"]: item for item in index["schemas"]}
    active = [
        item for item in entries.values() if item.get("export_revision") == "1.1.0"
    ]
    assert active
    for item in active:
        filename = item["filename"]
        prior = entries[item["supersedes"]]
        assert prior["export_revision"] == "1.0.0"
        assert prior["source_symbol"] == item["source_symbol"]
        assert prior["role"] == item["role"]
        assert item["sha256"] == hashlib.sha256(rendered[filename]).hexdigest()
        assert item["sha256"] != prior["sha256"]


def test_grouping_profile_schema_revision_and_legacy_digest(
    rendered: dict[str, bytes],
) -> None:
    source = _ROOT / "tests/fixtures/wp01b/o1/contracts.json"
    old = json.loads(source.read_text())["profile"]
    stem = "definitions.capabilities.ProfileDefinition"
    published = Draft202012Validator(json.loads(rendered[f"{stem}.schema.json"]))
    active = Draft202012Validator(json.loads(rendered[f"{stem}.v1.1.0.schema.json"]))
    published.validate(old)
    active.validate(old)
    loaded = ProfileDefinition.model_validate_json(json.dumps(old))
    limits = {"max_bytes": 65536, "max_depth": 16}
    old_digest = sha256_digest(canonical_json(old, **limits))
    assert sha256_digest(canonical_json(loaded.model_dump(mode="json"), **limits)) == (
        old_digest
    )

    grouped = {
        **old,
        "grounding_grouping": {"mode": "connected_boxes", "max_gap_pixels": 0},
    }
    active.validate(grouped)
    assert not published.is_valid(grouped)
    grouped_profile = ProfileDefinition.model_validate_json(json.dumps(grouped))
    assert grouped_profile.model_dump(mode="json") == grouped
    assert sha256_digest(canonical_json(grouped, **limits)) != old_digest

    per_box = {"mode": "per_box"}
    mode_schema = json.loads(
        rendered["definitions.capabilities.GroundingGrouping.schema.json"]
    )
    mode_validator = Draft202012Validator(mode_schema)
    mode_validator.validate(grouped["grounding_grouping"])
    mode_validator.validate(per_box)
    grouped["grounding_grouping"] = per_box
    active.validate(grouped)
    assert (
        ProfileDefinition.model_validate_json(json.dumps(grouped)).model_dump(
            mode="json"
        )
        == grouped
    )
    for invalid_mode in (None, {"mode": "per_box", "max_gap_pixels": 0}):
        invalid = {**grouped, "grounding_grouping": invalid_mode}
        assert not active.is_valid(invalid)
        with pytest.raises(ValidationError):
            ProfileDefinition.model_validate_json(json.dumps(invalid))


def test_grouped_observation_requires_the_active_schema(
    rendered: dict[str, bytes],
) -> None:
    legacy = node_cases()["observation_input"]
    stem = "nodes.observation.ObservationInput"
    published = Draft202012Validator(json.loads(rendered[f"{stem}.schema.json"]))
    active = Draft202012Validator(json.loads(rendered[f"{stem}.v1.1.0.schema.json"]))
    published.validate(legacy)
    _check_wire(legacy, active, TypeAdapter(ObservationInput))
    obj = legacy["object_record"]
    marked = {**obj["source_media_ref"], "media_id": "marked", "sha256": "b" * 64}
    grouped = {
        **legacy,
        "object_record": {
            **obj,
            "grounding_member_boxes": [obj["bbox"]],
            "marked_frame": {
                "media_ref": marked,
                "byte_size": 512,
                "mime_type": "image/png",
                "renderer_version": "1.0.0",
            },
        },
        "image_view": {**legacy["image_view"], "marked_media_ref": marked},
    }
    _check_wire(grouped, active, TypeAdapter(ObservationInput))
    assert not published.is_valid(grouped)


def test_node_run_revision_has_one_extra_physical_slot_only_for_n05(
    rendered: dict[str, bytes],
) -> None:
    node = _read("executions/contracts.json")["node"]
    stem = "runtime.executions.NodeRun"
    published = Draft202012Validator(json.loads(rendered[f"{stem}.schema.json"]))
    active = Draft202012Validator(json.loads(rendered[f"{stem}.v1.1.0.schema.json"]))
    node["node_id"] = "N05"
    node["attempt_history"] = [
        {
            "attempt_ref": {"record_kind": "attempt", "record_id": f"attempt-{n}"},
            "attempt_number": n,
        }
        for n in range(1, 1026)
    ]
    assert not published.is_valid(node)
    active.validate(node)
    assert len(NodeRun.model_validate_json(json.dumps(node)).attempt_history) == 1025
    for wrong_node in ("N03", "N04", "N06"):
        invalid = {**node, "node_id": wrong_node}
        assert not active.is_valid(invalid)
        with pytest.raises(ValidationError):
            NodeRun.model_validate_json(json.dumps(invalid))
    node["attempt_history"].append(
        {
            "attempt_ref": {"record_kind": "attempt", "record_id": "attempt-1026"},
            "attempt_number": 1026,
        }
    )
    assert not active.is_valid(node)
    with pytest.raises(ValidationError):
        NodeRun.model_validate_json(json.dumps(node))


@pytest.fixture(scope="module")
def examples() -> dict[str, dict[str, Any]]:
    return _examples()


def _source_name(entry: Any) -> str:
    return entry.source_symbol.removeprefix("jimu_runtime.domain.")


_C_PERSISTED = tuple(
    entry for entry in exporter.WP01C_INVENTORY if entry.role in _PERSISTED_ROLES
)


def test_every_c_persisted_root_has_a_registered_fixture(
    examples: dict[str, dict[str, Any]],
) -> None:
    assert {_source_name(entry) for entry in _C_PERSISTED} == examples.keys()


@pytest.mark.parametrize(
    "entry", _C_PERSISTED, ids=[_source_name(entry) for entry in _C_PERSISTED]
)
def test_c_root_export_accepts_valid_fixture_and_rejects_structural_invalids(
    entry: Any, rendered: dict[str, bytes], examples: dict[str, dict[str, Any]]
) -> None:
    raw = examples[_source_name(entry)]
    published = json.loads(rendered[entry.filename])
    assert published["additionalProperties"] is False
    assert "schema_version" in published["required"]
    validator = Draft202012Validator(published, format_checker=FormatChecker())
    adapter = TypeAdapter(getattr(entry.module, entry.symbol))
    _check_wire(raw, validator, adapter)


@pytest.mark.parametrize("source", _PERSISTED_UNIONS)
def test_c_persisted_union_dispatches_every_fixtured_branch(
    source: str, rendered: dict[str, bytes], examples: dict[str, dict[str, Any]]
) -> None:
    entry = next(
        item for item in exporter.WP01C_INVENTORY if _source_name(item) == source
    )
    assert entry.role == "named_union"
    published = json.loads(rendered[entry.filename])
    branches = _PERSISTED_UNIONS[source]
    assert len(published["oneOf"]) == len(branches)
    validator = Draft202012Validator(published, format_checker=FormatChecker())
    adapter = TypeAdapter(getattr(entry.module, entry.symbol))
    for branch in branches:
        _check_wire(
            examples[f"{source.rsplit('.', 1)[0]}.{branch}"], validator, adapter
        )


def _check_wire(
    raw: dict[str, Any], validator: Draft202012Validator, adapter: TypeAdapter[Any]
) -> None:
    validator.validate(raw)
    model = adapter.validate_json(json.dumps(raw))
    serialized = adapter.dump_json(model)
    validator.validate(json.loads(serialized))
    assert adapter.validate_json(serialized) == model
    without_version = {
        key: value for key, value in raw.items() if key != "schema_version"
    }
    for invalid in (without_version, {**raw, "invalid_extra_field": True}):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(invalid))
