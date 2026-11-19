import json
from dataclasses import replace
from pathlib import Path

import pytest

from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.workflow.pins import ExecutionPinError
from tests.fixtures.wp02.compiler.scenario import approve_source_bytes, source_scenario
from tests.fixtures.wp02.releases.support import submission
from tests.fixtures.wp03.pins.support import (
    accepted_scenario,
    activate_scenario,
    evaluated_fallback,
)


def test_resolves_actual_admission_configuration_and_ignores_registry_order(
    tmp_path: Path,
) -> None:
    scenario = accepted_scenario(tmp_path)
    resolved = scenario.resolve()
    assert resolved.pins.release_id == scenario.task.release_id
    assert resolved.pins.bundle_digest == scenario.task.bundle_digest
    assert resolved.pins.runtime_version == scenario.host.runtime_version
    assert resolved.pins.workflow_version == resolved.policy.workflow_version == "1.0.0"
    assert (
        resolved.catalogue.resolve(resolved.policy_ref).inventory.path
        == "policies/execution.json"
    )
    assert len(resolved.pins.model_pins) == len(resolved.model_deployments) == 4
    for pin, actual in zip(
        resolved.pins.model_pins, resolved.model_deployments, strict=True
    ):
        assert (
            resolved.catalogue.resolve(pin.binding_ref).ref.artifact_kind
            is ArtifactKind.MODEL_BINDING
        )
        assert (
            resolved.catalogue.resolve(pin.provider_policy_ref).ref.artifact_kind
            is ArtifactKind.POLICY
        )
        assert (
            resolved.catalogue.resolve(pin.fallback_policy_ref).ref.artifact_kind
            is ArtifactKind.POLICY
        )
        assert pin.binding_ref == actual.binding_ref
        assert pin.primary_deployment_id == actual.deployments[0].deployment_id
        assert actual.deployments[0].model_version == "1.0.0"
    (tool,) = resolved.pins.tool_pins
    assert (
        resolved.catalogue.resolve(tool.tool_ref)
        == resolved.catalogue.of_kind(ArtifactKind.TOOL)[0]
    )
    assert tool.implementation_version == "1.0.0"
    assert (
        scenario.resolve(
            host=scenario.host.model_copy(
                update={"capabilities": scenario.host.capabilities[::-1]}
            )
        ).pins
        == resolved.pins
    )


def test_history_survives_an_actual_activation_switch_and_reopen(
    tmp_path: Path,
) -> None:
    scenario = accepted_scenario(tmp_path)
    original = scenario.resolve()
    source = source_scenario()
    artifacts = []
    for artifact in source.artifacts:
        if artifact.path == "policies/execution.json":
            data = json.loads(artifact.content)
            data["policy_id"] = "next-workflow"
            artifact = replace(
                artifact, artifact_id="next-workflow", content=json.dumps(data).encode()
            )
        artifacts.append(artifact)
    new_source = approve_source_bytes(
        replace(
            source,
            metadata=replace(source.metadata, bundle_version="1.0.1"),
            artifacts=tuple(artifacts),
        )
    )
    activate_scenario(
        tmp_path / "build-two",
        scenario.store,
        scenario.manager,
        scenario.authority,
        new_source,
        scenario.host,
        number=2,
        expected="activation-1-global",
    )
    store = LocalBundleStore(tmp_path / "ledger")
    manager = scenario.authority.manager(store)
    again = manager.accept_task(submission(), principal=scenario.authority.task_client)
    new_task = manager.accept_task(
        submission("second"), principal=scenario.authority.task_client
    )
    assert new_task.release_id == "release-2"
    new = replace(scenario, manager=manager, store=store, task=new_task).resolve()
    assert original.policy_ref.version == new.policy_ref.version
    assert original.policy_ref != new.policy_ref
    assert original.pins.bundle_digest != new.pins.bundle_digest
    assert original.pins.input_digest != new.pins.input_digest
    assert (
        original.pins
        == replace(scenario, manager=manager, store=store, task=again).resolve().pins
    )


def test_pins_approved_fallback_and_requires_exact_deployment_and_review(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    base = accepted_scenario(baseline)
    source, host = evaluated_fallback(base.resolve(), base.host)
    alternate = tmp_path / "explicit-fallback"
    alternate.mkdir()
    scenario = accepted_scenario(alternate, source=source, host=host)
    resolved = scenario.resolve()
    for model, actual in zip(
        resolved.pins.model_pins, resolved.model_deployments, strict=True
    ):
        assert model.primary_deployment_id == model.deployments[0].deployment_id
        assert model.deployments[1].deployment_id == "fixture.remote.fallback"
        assert actual.deployments[1].deployment_id == "fixture.remote.fallback"

    with pytest.raises(ExecutionPinError, match="model.host_deployment_mismatch"):
        scenario.resolve(
            host=host.model_copy(
                update={"model_deployments": host.model_deployments[:1]}
            )
        )
    with pytest.raises(ExecutionPinError, match="model.fallback_review_missing"):
        scenario.resolve(host=host.model_copy(update={"model_reviews": ()}))


def test_digest_binds_compatible_actual_runtime_and_model_versions(
    tmp_path: Path,
) -> None:
    source = source_scenario()
    artifacts = []
    for artifact in source.artifacts:
        if artifact.kind is ArtifactKind.POLICY and artifact.artifact_id == "provider":
            document = json.loads(artifact.content)
            constraints = document["policy"]["deployments"][0][
                "actual_version_constraints"
            ]
            constraints["allowed_model_versions"].append("1.0.1")
            artifact = replace(artifact, content=json.dumps(document).encode())
        artifacts.append(artifact)
    scenario = accepted_scenario(
        tmp_path,
        source=approve_source_bytes(replace(source, artifacts=tuple(artifacts))),
    )
    original = scenario.resolve()
    host = scenario.host
    (deployment,) = host.model_deployments
    new_identity = deployment.identity.model_copy(update={"model_version": "1.0.1"})
    new_deployment = deployment.model_copy(update={"identity": new_identity})
    new_model = scenario.resolve(
        host=host.model_copy(update={"model_deployments": (new_deployment,)})
    )
    assert new_model.pins.model_pins == original.pins.model_pins
    assert new_model.pins.input_digest != original.pins.input_digest
    assert new_model.model_deployments[0].deployments[0].model_version == "1.0.1"

    new_runtime = scenario.resolve(
        host=host.model_copy(update={"runtime_version": "1.0.1"})
    )
    assert new_runtime.pins.runtime_version == "1.0.1"
    assert new_runtime.pins.input_digest != original.pins.input_digest
