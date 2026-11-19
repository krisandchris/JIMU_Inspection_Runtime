import json
from dataclasses import replace

import pytest

from jimu_runtime.bundle.compiler import compile_bundle
from jimu_runtime.domain.enums import ArtifactKind
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    PLAN,
    QUESTION,
    TOOL,
    approve_source_bytes,
    compile_environment,
    source_scenario,
)


def _replace_artifact(source, kind, identity, mutate):
    artifacts = []
    for artifact in source.artifacts:
        if artifact.kind is kind and artifact.artifact_id == identity:
            artifact = mutate(artifact)
        artifacts.append(artifact)
    return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))


def _edit(artifact, callback):
    value = json.loads(artifact.content)
    callback(value)
    return replace(artifact, content=json.dumps(value).encode())


def test_duplicate_source_artifact_is_rejected(tmp_path):
    source = source_scenario()
    duplicated = replace(source, artifacts=(*source.artifacts, source.artifacts[0]))
    result = compile_bundle(
        duplicated, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "source_identity_duplicate"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing", "source_reference_missing"),
        ("cycle", "source_reference_cycle"),
        ("occupied", "source_reference_slot_invalid"),
    ],
)
def test_source_bindings_fail_closed(change, reason, tmp_path):
    source = source_scenario()

    def mutate(artifact):
        first, *rest = artifact.reference_slots
        if change == "missing":
            changed = replace(
                first, target=(first.target[0], "missing", first.target[2])
            )
        elif change == "cycle":
            changed = replace(first, target=artifact.identity)
        else:
            return _edit(
                artifact, lambda fields: fields.update(prompt_template_ref="reserved")
            )
        return replace(artifact, reference_slots=(changed, *rest))

    rejected = _replace_artifact(source, ArtifactKind.QUESTION, QUESTION, mutate)
    result = compile_bundle(
        rejected, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == reason


def test_wrong_fact_child_pin_rejected_before_packing(tmp_path):
    source = source_scenario()
    bad = _replace_artifact(
        source,
        ArtifactKind.FACT_SCHEMA,
        "parked",
        lambda artifact: _edit(
            artifact,
            lambda fields: fields.update(
                payload_schema_ref={
                    "artifact_kind": "value_schema",
                    "artifact_id": "wrong",
                    "version": "1.0.0",
                    "digest": "aa" * 32,
                }
            ),
        ),
    )
    result = compile_bundle(
        bad, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "source_value_schema_pin"


def test_tool_dag_cycle_is_rejected(tmp_path):
    source = source_scenario()
    bad = _replace_artifact(
        source,
        ArtifactKind.TOOL_PLAN,
        PLAN,
        lambda artifact: _edit(
            artifact,
            lambda fields: fields["steps"][0].update(depends_on=[TOOL]),
        ),
    )
    result = compile_bundle(
        bad, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors
    assert any(
        "depend" in item.message.lower() or "cycle" in item.message.lower()
        for item in result.errors
    )


def test_required_capability_unavailable(tmp_path):
    environment = compile_environment(tmp_path)
    environment = replace(
        environment,
        capabilities=environment.capabilities.model_copy(update={"capabilities": ()}),
    )
    result = compile_bundle(source_scenario(), limits=LIMITS, environment=environment)
    assert result.bundle is None
    assert any(item.code.startswith("capability.") for item in result.errors)


def test_mandatory_rule_without_a_tool_fact_producer_is_rejected(tmp_path):
    source = source_scenario()
    unsupported = _replace_artifact(
        source,
        ArtifactKind.TOOL_PLAN,
        PLAN,
        lambda artifact: _edit(
            artifact, lambda fields: fields["steps"][0].update(fact_output_mappings=[])
        ),
    )
    result = compile_bundle(
        unsupported, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "rule_type.tool_output_identity"


def test_existing_source_pin_is_checked_without_a_slot(tmp_path):
    source = source_scenario()

    def mutate(artifact):
        first, *rest = artifact.reference_slots
        assert first.location == ("prompt_template_ref",)
        kind, identity, version = first.target
        edited = _edit(
            artifact,
            lambda fields: fields.update(
                prompt_template_ref={
                    "artifact_kind": kind.value,
                    "artifact_id": identity,
                    "version": version,
                    "digest": "aa" * 32,
                }
            ),
        )
        return replace(edited, reference_slots=tuple(rest))

    invalid = _replace_artifact(source, ArtifactKind.QUESTION, QUESTION, mutate)
    result = compile_bundle(
        invalid, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "reference_digest_mismatch"


def test_unlisted_provenance_source_rejected(tmp_path):
    source = source_scenario()
    external = replace(
        source,
        metadata=replace(
            source.metadata,
            source=source.metadata.source.model_copy(
                update={
                    "source_system": "unlisted.fixture",
                }
            ),
        ),
    )
    result = compile_bundle(
        external, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "trust.source_unauthorized"


def test_distinct_schema_sources_sharing_path_are_rejected(tmp_path):
    source = source_scenario()
    context = next(
        item for item in source.artifacts if item.kind is ArtifactKind.CONTEXT_SCHEMA
    )
    conflicting = replace(
        context,
        artifact_id="other-context",
        origin=replace(context.origin, artifact_id="other-context"),
    )
    ambiguous = approve_source_bytes(
        replace(source, artifacts=(*source.artifacts, conflicting))
    )
    result = compile_bundle(
        ambiguous, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "source_path_overlap"
    assert result.errors[0].path == context.path


def test_provenance_row_must_match_raw_input_bytes(tmp_path):
    source = source_scenario()
    first, *rest = source.artifacts
    # Even whitespace changes the approved source bytes before JCS conversion.
    revised = replace(
        source, artifacts=(replace(first, content=b" " + first.content), *rest)
    )
    result = compile_bundle(
        revised, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == "source_provenance_digest_mismatch"
    assert result.errors[0].path == first.path


@pytest.mark.parametrize("change", ["missing", "orphan", "duplicate"])
def test_provenance_requires_exact_bijection(tmp_path, change):
    source = source_scenario()
    pins = source.metadata.source
    a, b, *other = source.artifacts
    if change == "missing":
        changed_pins = pins.model_copy(update={"artifacts": pins.artifacts[1:]})
        source = replace(source, metadata=replace(source.metadata, source=changed_pins))
    elif change == "orphan":
        source = replace(source, artifacts=(b, *other))
    else:
        source = replace(source, artifacts=(a, replace(b, origin=a.origin), *other))
    result = compile_bundle(
        source, limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.bundle is None
    assert result.errors[0].code == f"source_provenance_{change}"
