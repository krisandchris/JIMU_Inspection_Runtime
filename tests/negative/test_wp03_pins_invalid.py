import json
from dataclasses import replace
from pathlib import Path

import pytest

from jimu_runtime.bundle.archive import write_archive
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_enums import CapabilityFamily
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.workflow.pins import ExecutionPinError, resolve_execution_pins
from tests.fixtures.wp02.admission.support import build_scenario, rebuild_unsigned
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    approve_source_bytes,
    source_scenario,
)
from tests.fixtures.wp02.trust.support import AT
from tests.fixtures.wp03.pins.support import accepted_scenario


def test_refuses_cross_scope_task_and_altered_acceptance(tmp_path: Path) -> None:
    scenario = accepted_scenario(tmp_path)
    with pytest.raises(ExecutionPinError, match="task_scope_mismatch"):
        resolve_execution_pins(
            scenario.manager,
            scenario.store,
            scenario.task,
            scope=scenario.task.idempotency_scope.model_copy(
                update={"client_id": "different"}
            ),
            host=scenario.host,
            validation_at=AT,
            limits=LIMITS,
        )
    for changes in (
        {"bundle_digest": "f" * 64},
        {"release_id": "forged-id"},
        {"task_id": "task.unknown"},
    ):
        forged = scenario.task.model_copy(update=changes)
        with pytest.raises(ExecutionPinError, match="task_pin_mismatch"):
            replace(scenario, task=forged).resolve()


def test_refuses_store_without_the_admission(tmp_path: Path) -> None:
    scenario = accepted_scenario(tmp_path)
    with pytest.raises(ExecutionPinError, match="admission_pin_mismatch"):
        replace(scenario, store=LocalBundleStore(tmp_path / "empty-ledger")).resolve()


def test_refuses_archive_from_different_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = accepted_scenario(tmp_path)
    other = build_scenario(tmp_path)
    substituted, root = rebuild_unsigned(
        other.archive_bytes, lambda manifest, _: manifest.update(bundle_version="1.0.1")
    )
    assert root != scenario.task.bundle_digest
    monkeypatch.setattr(scenario.store, "read_archive", lambda _: substituted)
    with pytest.raises(ExecutionPinError, match="bundle_pin_mismatch"):
        scenario.resolve()


def test_refuses_invalid_zip_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = accepted_scenario(tmp_path)
    monkeypatch.setattr(
        scenario.store, "read_archive", lambda _: write_archive({}, LIMITS)
    )
    with pytest.raises(ExecutionPinError):
        scenario.resolve()


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"runtime_version": "2.0.0"}, "capability.runtime_version"),
        ({"supported_workflow_versions": ()}, "workflow_version_unavailable"),
        ({"model_deployments": ()}, "model.host_deployment_mismatch"),
        ({"bundle_schema_versions": ("2.0.0",)}, "capability.bundle_schema"),
    ],
)
def test_refuses_unavailable_host_configuration(
    tmp_path: Path, updates: dict[str, object], code: str
) -> None:
    scenario = accepted_scenario(tmp_path)
    with pytest.raises(ExecutionPinError) as error:
        scenario.resolve(host=scenario.host.model_copy(update=updates))
    assert error.value.code == code
    assert error.value.path


def test_receipt_selected_tool_version_cannot_be_replaced(tmp_path: Path) -> None:
    source = source_scenario()
    required = tuple(
        item.model_copy(update={"allowed_implementation_versions": ("1.0.0", "1.0.1")})
        if item.family is CapabilityFamily.TOOL_IMPLEMENTATION
        else item
        for item in source.metadata.required_capabilities
    )
    artifacts = []
    for artifact in source.artifacts:
        if artifact.kind is ArtifactKind.TOOL:
            document = json.loads(artifact.content)
            document["implementation_requirement"][
                "allowed_implementation_versions"
            ] = ["1.0.0", "1.0.1"]
            artifact = replace(artifact, content=json.dumps(document).encode())
        artifacts.append(artifact)
    source = approve_source_bytes(
        replace(
            source,
            metadata=replace(source.metadata, required_capabilities=required),
            artifacts=tuple(artifacts),
        )
    )
    scenario = accepted_scenario(tmp_path, source=source)
    assert scenario.resolve().pins.tool_pins[0].implementation_version == "1.0.0"

    for capabilities in (
        tuple(
            item
            for item in scenario.host.capabilities
            if item.family is not CapabilityFamily.TOOL_IMPLEMENTATION
        ),
        tuple(
            item.model_copy(update={"implementation_version": "1.0.1"})
            if item.family is CapabilityFamily.TOOL_IMPLEMENTATION
            else item
            for item in scenario.host.capabilities
        ),
    ):
        with pytest.raises(ExecutionPinError) as error:
            scenario.resolve(
                host=scenario.host.model_copy(update={"capabilities": capabilities})
            )
        assert error.value.code == "tool_implementation_unavailable"
        assert error.value.path[-1] == "implementation_requirement"


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("missing", "execution_policy_cardinality"),
        ("ambiguous", "execution_policy_cardinality"),
        ("misplaced", "execution_policy_locator"),
    ],
)
def test_resolver_requires_exact_execution_policy_locator(
    tmp_path: Path, mode: str, code: str
) -> None:
    source = source_scenario()
    path = "policies/execution.json"
    policies = tuple(item for item in source.artifacts if item.path == path)
    (policy,) = policies
    rest = tuple(item for item in source.artifacts if item.path != path)
    doc = json.loads(policy.content)
    doc["policy_id"] = "workflow-alt"
    other = replace(
        policy,
        artifact_id="workflow-alt",
        path="policies/registry.json",
        content=json.dumps(doc).encode(),
        origin=replace(policy.origin, artifact_id="policy:workflow-alt"),
    )
    if mode == "missing":
        artifacts = rest
    elif mode == "ambiguous":
        artifacts = (*rest, policy, other)
    else:
        artifacts = (*rest, replace(policy, path="policies/registry.json"))
    scenario = accepted_scenario(
        tmp_path, source=approve_source_bytes(replace(source, artifacts=artifacts))
    )
    with pytest.raises(ExecutionPinError) as error:
        scenario.resolve()
    assert error.value.code == code


def test_refuses_different_zip_for_same_logical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = accepted_scenario(tmp_path)
    release = scenario.manager.get_release(scenario.task.release_id)
    assert release is not None and release.admission_receipt is not None
    original = scenario.store.read_archive(release.admission_receipt.receipt_id)
    detached, digest = rebuild_unsigned(original, lambda _manifest, _payloads: None)
    assert digest == scenario.task.bundle_digest and detached != original
    monkeypatch.setattr(scenario.store, "read_archive", lambda _: detached)
    with pytest.raises(ExecutionPinError, match="archive_receipt_mismatch"):
        scenario.resolve()
