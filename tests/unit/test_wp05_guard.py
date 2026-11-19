"""Exercise reached guards with the compiled, signed and approved parking fixture."""

import asyncio
import importlib
import json
from dataclasses import replace
from hashlib import sha256

import pytest
from fixtures.wp03.coverage.scenario import scenario
from fixtures.wp03.nodes.scenario import RGB_IMAGE

from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.runtime.decisions import (
    BlockedRequiredEvidenceGuard,
    CompleteRequiredEvidenceGuard,
    RequiredEvidenceReason,
    RuleEvaluation,
)
from jimu_runtime.domain.runtime.facts import EvidenceRef, KnownFact
from jimu_runtime.domain.runtime.media import StagedAsset
from jimu_runtime.domain.validation.coverage_sources import (
    validate_decision_guard_authority,
)
from jimu_runtime.domain.values import BooleanValue, StringValue
from jimu_runtime.inspection.rules.evidence_guard import (
    RequiredEvidenceGuardBuilder,
)
from jimu_runtime.workflow.mock_support import artifact
from jimu_runtime.workflow.worker_coverage import build_coverage_authority


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    return scenario(tmp_path_factory.mktemp("guard"))


def builder(
    runtime, *, inputs=None, evidence=None, reader=None, pin_rows=True, **overrides
):
    assert runtime.runtime is not None
    stage = runtime.stages[0]
    actual_rows = stage.evidence if evidence is None else evidence
    inputs = stage.draft.inputs if inputs is None else inputs
    if pin_rows:
        inputs = inputs.model_copy(
            update={
                "envelope": inputs.envelope.model_copy(
                    update={
                        "upstream_artifacts": (
                            *inputs.envelope.upstream_artifacts,
                            *(
                                artifact(
                                    RuntimeRecordKind.EVIDENCE, a.evidence_id, a
                                ).pin
                                for a in actual_rows
                            ),
                        ),
                    }
                ),
            }
        )
    args = {
        "catalogue": runtime.scope.catalogue,
        "task": runtime.scope.task,
        "media": runtime.scope.media,
        "authority": build_coverage_authority(runtime.resolved, runtime.runtime),
        "inputs": inputs,
        "evidence_refs": tuple(
            EvidenceRef(
                record_kind=RuntimeRecordKind.EVIDENCE, record_id=asset.evidence_id
            )
            for asset in actual_rows
        ),
        "evidence": actual_rows,
        "read_evidence": reader or (lambda asset, limit: RGB_IMAGE),
        "max_evidence_bytes": len(RGB_IMAGE) * 2,
    }
    return RequiredEvidenceGuardBuilder(**(args | overrides))


def test_complete_guard_is_accepted_by_runtime_authority_and_coverage(runtime):
    evaluated = runtime.stages[0].decision.rule_evaluations[0]
    result = builder(runtime).build(evaluated.rule)
    assert isinstance(result.guard, CompleteRequiredEvidenceGuard)
    assert len(result.guard.fact_proofs) == 2
    assert len(result.predicate_facts) == 2
    assert result.guard == evaluated.result.guard
    assert runtime.runtime is not None
    validate_decision_guard_authority(
        result.facts,
        result.selections,
        authority=build_coverage_authority(runtime.resolved, runtime.runtime),
    )
    new_result = evaluated.result.model_copy(update={"guard": result.guard})
    new_evaluation = RuleEvaluation.model_validate(
        evaluated.model_copy(
            update={
                "result": new_result,
                "fact_refs": result.fact_refs,
                "evidence_refs": result.guard.required_evidence_refs,
                "optional_fact_proofs": result.optional_fact_proofs,
            }
        )
    )
    stage = runtime.stages[0]
    decision = stage.decision.model_copy(update={"rule_evaluations": (new_evaluation,)})
    draft = stage.draft.model_copy(update={"decision": decision})
    updated = replace(runtime, stages=(replace(stage, draft=draft),))
    assert updated.finalize().decisions[0].rule_evaluations == (new_evaluation,)


@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        ("staged", RequiredEvidenceReason.EVIDENCE_UNAVAILABLE),
        ("unreadable", RequiredEvidenceReason.EVIDENCE_UNAVAILABLE),
        ("bad_bytes", RequiredEvidenceReason.EVIDENCE_UNAVAILABLE),
        ("no_row", RequiredEvidenceReason.EVIDENCE_UNAVAILABLE),
        ("fact_unpinned", RequiredEvidenceReason.REQUIRED_FACT_UNAVAILABLE),
        ("authority_unpinned", RequiredEvidenceReason.PRODUCER_AUTHORITY_UNRESOLVED),
    ],
)
def test_unavailable_required_inputs_produce_precise_reasons(runtime, defect, expected):
    stage = runtime.stages[0]
    inputs = stage.draft.inputs
    evidence = stage.evidence
    (asset,) = evidence
    calls = []

    def read(asset, limit):
        calls.append(asset.evidence_id)
        assert limit == len(RGB_IMAGE)
        if defect == "unreadable":
            raise FileNotFoundError
        if defect == "bad_bytes":
            return b"X" + RGB_IMAGE[1:]
        return RGB_IMAGE

    if defect == "staged":
        evidence = (
            asset.model_copy(
                update={
                    "stage": StagedAsset(
                        stage="staged", staged_at=asset.stage.staged_at
                    )
                }
            ),
        )
    elif defect == "no_row":
        evidence = ()
    elif defect == "fact_unpinned":
        inputs = inputs.model_copy(update={"fact_refs": inputs.fact_refs[:-1]})
    elif defect == "authority_unpinned":
        inputs = inputs.model_copy(
            update={"authority_selection_refs": inputs.authority_selection_refs[:-1]}
        )
    guard = (
        builder(runtime, inputs=inputs, evidence=evidence, reader=read)
        .build(stage.decision.rule_evaluations[0].rule)
        .guard
    )
    assert isinstance(guard, BlockedRequiredEvidenceGuard)
    assert guard.reasons == (expected,)
    if defect in ("staged", "no_row"):
        assert calls == []


def test_wrong_scope_and_unpinned_evidence_are_rejected(runtime):
    stage = runtime.stages[0]
    (asset,) = stage.evidence
    with pytest.raises(ValueError, match="another Task, Project, Execution"):
        builder(
            runtime,
            evidence=(
                asset.model_copy(
                    update={
                        "access_scope": asset.access_scope.model_copy(
                            update={"project_id": "foreign-project"}
                        )
                    }
                ),
            ),
        )
    with pytest.raises(ValueError, match="was not pinned"):
        builder(runtime, evidence_refs=())


def test_rule_path_requires_exact_digest(runtime):
    path = runtime.stages[0].decision.rule_evaluations[0].rule
    altered = path.model_copy(
        update={"rule_ref": path.rule_ref.model_copy(update={"digest": "0" * 64})}
    )
    with pytest.raises(ValueError, match="outside the exact selected RuleSet"):
        builder(runtime).build(altered)


def test_optional_nonknown_operand_does_not_block_guard(tmp_path, monkeypatch):
    from fixtures.wp02.compiler.scenario import approve_source_bytes

    from jimu_runtime.domain.enums import ArtifactKind
    from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
    from jimu_runtime.workflow import mock_decision
    from tests.fixtures.wp03.nodes import scenario as source_fixture

    parking_source = source_fixture.parking_source

    def optional_rule_source(**kwargs):
        source = parking_source(**kwargs)
        revised = []
        for entry in source.artifacts:
            if entry.kind is ArtifactKind.RULE:
                doc = json.loads(entry.content)
                doc["optional_fact_selectors"] = [doc["required_fact_selectors"].pop(0)]
                slots = tuple(
                    replace(
                        slot,
                        location=(
                            "optional_fact_selectors"
                            if slot.location[1] == 0
                            else "required_fact_selectors",
                            0,
                            *slot.location[2:],
                        ),
                    )
                    if slot.location[0] == "required_fact_selectors"
                    else slot
                    for slot in entry.reference_slots
                )
                entry = replace(
                    entry, content=canonical_bytes(doc), reference_slots=slots
                )
            revised.append(entry)
        return approve_source_bytes(replace(source, artifacts=tuple(revised)))

    synthetic_condition = mock_decision._condition

    def legacy_condition(scope, call, obj, rule, facts):
        # Only prepare the old mock scenario's upstream runtime records.
        # Its synthetic N06 supports an all-required conjunction.
        return synthetic_condition(
            scope,
            call,
            obj,
            rule.model_copy(
                update={
                    "required_fact_selectors": (
                        *rule.required_fact_selectors,
                        *rule.optional_fact_selectors,
                    ),
                    "optional_fact_selectors": (),
                }
            ),
            facts,
        )

    monkeypatch.setattr(source_fixture, "parking_source", optional_rule_source)
    monkeypatch.setattr(mock_decision, "_condition", legacy_condition)
    coverage_module = importlib.import_module(scenario.__module__)
    observation_case = coverage_module.observation_case

    def invalid_observation(*args):
        return observation_case(
            *args, readings=(StringValue(kind="string", value="invalid"),)
        )

    monkeypatch.setattr(coverage_module, "observation_case", invalid_observation)
    fixture = scenario(tmp_path)
    path = fixture.stages[0].draft.inputs.mandatory_rule_paths[0]
    result = builder(fixture).build(path)
    assert isinstance(result.guard, CompleteRequiredEvidenceGuard)
    assert len(result.guard.fact_proofs) == len(result.optional_fact_proofs) == 1
    assert len(result.predicate_facts) == 2
    optional = result.predicate_facts[-1]
    assert not isinstance(optional, KnownFact)
    assert optional.fact_id == result.optional_fact_proofs[0].fact_ref.record_id


def test_forged_fact_with_actual_id_is_rejected(runtime):
    assert runtime.runtime is not None
    authority = build_coverage_authority(runtime.resolved, runtime.runtime)
    original = authority.records.facts[0]
    assert isinstance(original, KnownFact)
    forged = original.model_copy(
        update={"value": BooleanValue(kind="bool", value=False)}
    )
    assert forged != original
    records = authority.records.model_copy(
        update={"facts": (forged, *authority.records.facts[1:])}
    )
    with pytest.raises(ValueError, match="candidate Fact claims differ"):
        builder(runtime, authority=replace(authority, records=records))


def test_only_selected_tool_output_evidence_satisfies_guard(tmp_path, monkeypatch):
    from fixtures.wp02.compiler.scenario import approve_source_bytes

    from jimu_runtime.domain.definition_enums import EvidenceType
    from jimu_runtime.domain.enums import ArtifactKind
    from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
    from jimu_runtime.domain.runtime.evidence import NonSpatialEvidence
    from jimu_runtime.domain.runtime.facts import ToolFactSource
    from jimu_runtime.domain.runtime.tool_executions import ReturnedToolAttempt
    from tests.fixtures.wp03.nodes import scenario as source_fixture

    parking_source = source_fixture.parking_source

    def tool_evidence_source(**kwargs):
        source = parking_source(**kwargs)
        artifacts = []
        for entry in source.artifacts:
            if entry.kind is ArtifactKind.POLICY and entry.artifact_id == "evidence":
                doc = json.loads(entry.content)
                doc["policy"]["required_evidence_types"] = ["model_response"]
                doc["policy"]["allowed_evidence_types"].append("model_response")
                entry = replace(entry, content=canonical_bytes(doc))
            artifacts.append(entry)
        return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))

    monkeypatch.setattr(source_fixture, "parking_source", tool_evidence_source)
    fixture = scenario(tmp_path)
    stage = fixture.stages[0]
    assert fixture.runtime is not None
    (tool,) = fixture.runtime.tool_executions
    assert isinstance(tool.attempt_result, ReturnedToolAttempt)
    (original,) = stage.evidence
    asset = original.model_copy(
        update={
            "evidence_id": "selected-tool-output",
            "evidence_type": EvidenceType.MODEL_RESPONSE,
            "mime_type": "application/json",
            "spatial": NonSpatialEvidence(kind="non_spatial"),
            "producer_id": tool.tool_ref.artifact_id,
            "producer_version": tool.tool_ref.version,
        }
    )
    sibling = asset.model_copy(update={"evidence_id": "same-type-unrelated"})
    content = b"{}"
    digest = sha256(content).hexdigest()
    smaller = asset.model_copy(
        update={
            "evidence_id": "zzz-small-tool-output",
            "byte_size": len(content),
            "sha256": digest,
            "stage": asset.stage.model_copy(update={"verified_sha256": digest}),
        }
    )
    linked_ref, unrelated_ref, smaller_ref = (
        EvidenceRef(record_kind=RuntimeRecordKind.EVIDENCE, record_id=row.evidence_id)
        for row in (asset, sibling, smaller)
    )
    facts = tuple(
        fact.model_copy(
            update={"evidence_refs": (linked_ref, unrelated_ref, smaller_ref)}
        )
        if isinstance(fact.source, ToolFactSource)
        else fact
        for fact in fixture.runtime.facts
    )
    pins = {
        fact.fact_id: artifact(RuntimeRecordKind.FACT, fact.fact_id, fact).pin
        for fact in facts
    }
    inputs = stage.draft.inputs
    inputs = inputs.model_copy(
        update={
            "envelope": inputs.envelope.model_copy(
                update={
                    "upstream_artifacts": tuple(
                        pins[pin.ref.record_id]
                        if pin.ref.record_kind is RuntimeRecordKind.FACT
                        else pin
                        for pin in inputs.envelope.upstream_artifacts
                    ),
                }
            ),
        }
    )
    (state,) = tool.attempt_result.result.output_states
    result = tool.attempt_result.result.model_copy(
        update={
            "evidence_refs": (linked_ref, unrelated_ref, smaller_ref),
            "output_states": (
                state.model_copy(update={"evidence_refs": (linked_ref, smaller_ref)}),
            ),
        }
    )
    tool = tool.model_copy(
        update={
            "evidence_refs": (linked_ref, unrelated_ref, smaller_ref),
            "attempt_result": tool.attempt_result.model_copy(update={"result": result}),
        }
    )
    records = fixture.runtime.model_copy(
        update={"facts": facts, "tool_executions": (tool,)}
    )
    fixture = replace(fixture, runtime=records)
    path = stage.draft.inputs.mandatory_rule_paths[0]
    complete = builder(
        fixture, inputs=inputs, evidence=(original, asset, sibling)
    ).build(path)
    assert isinstance(complete.guard, CompleteRequiredEvidenceGuard)
    assert complete.evidence == (asset, original)
    blocked = builder(fixture, inputs=inputs, evidence=(original, sibling)).build(path)
    assert isinstance(blocked.guard, BlockedRequiredEvidenceGuard)
    assert blocked.guard.reasons == (RequiredEvidenceReason.EVIDENCE_UNAVAILABLE,)
    assert blocked.evidence == (original,)
    budgeted = builder(
        fixture,
        inputs=inputs,
        evidence=(original, asset, smaller),
        reader=lambda row, limit: content if row == smaller else RGB_IMAGE,
        max_evidence_bytes=len(RGB_IMAGE) + len(content),
    ).build(path)
    assert isinstance(budgeted.guard, CompleteRequiredEvidenceGuard)
    assert budgeted.evidence == (smaller, original)


def test_evidence_outside_read_budget_remains_unavailable(runtime):
    stage = runtime.stages[0]

    def unexpected_read(asset, limit):
        pytest.fail("read beyond evidence byte budget")

    result = builder(
        runtime, reader=unexpected_read, max_evidence_bytes=len(RGB_IMAGE) - 1
    ).build(stage.draft.inputs.mandatory_rule_paths[0])
    assert isinstance(result.guard, BlockedRequiredEvidenceGuard)
    assert result.guard.reasons == (RequiredEvidenceReason.EVIDENCE_UNAVAILABLE,)


def test_original_requires_an_exact_n06_metadata_pin(runtime):
    (asset,) = runtime.stages[0].evidence
    with pytest.raises(ValueError, match="requires a metadata digest pin"):
        builder(runtime, pin_rows=False)
    inputs = runtime.stages[0].draft.inputs
    original_pin = artifact(RuntimeRecordKind.EVIDENCE, asset.evidence_id, asset).pin
    inputs = inputs.model_copy(
        update={
            "envelope": inputs.envelope.model_copy(
                update={
                    "upstream_artifacts": (
                        *inputs.envelope.upstream_artifacts,
                        original_pin,
                    ),
                }
            )
        }
    )
    swapped = asset.model_copy(update={"producer_id": "other-producer"})
    with pytest.raises(
        ValueError, match="differs from the actual runtime record digest"
    ):
        builder(runtime, inputs=inputs, evidence=(swapped,), pin_rows=False)


def test_failed_read_cache_does_not_charge_later_rules(runtime):
    (asset,) = runtime.stages[0].evidence
    good = asset.model_copy(update={"evidence_id": "second-valid"})
    bad = asset.model_copy(update={"evidence_id": "first-invalid"})
    seen = []

    def read(row, limit):
        seen.append(row.evidence_id)
        return RGB_IMAGE if row == good else None

    guarded = builder(
        runtime, evidence=(bad, good), reader=read, max_evidence_bytes=len(RGB_IMAGE)
    )
    path = runtime.stages[0].draft.inputs.mandatory_rule_paths[0]
    blocked = guarded.build(path)
    assert isinstance(blocked.guard, BlockedRequiredEvidenceGuard)
    complete = guarded.build(path)
    assert isinstance(complete.guard, CompleteRequiredEvidenceGuard)
    assert complete.evidence == (good,)
    assert seen == [bad.evidence_id, good.evidence_id]


def test_geometry_masks_keep_selected_child_and_named_input_role(runtime, tmp_path):
    from jimu_runtime.bundle.semantic_inputs import require_definition
    from jimu_runtime.domain.definition_enums import EvidenceType
    from jimu_runtime.domain.policies import EvidencePolicyArtifact
    from jimu_runtime.domain.runtime.tool_executions import ReturnedToolAttempt
    from jimu_runtime.inspection.rules._guard_sources import EvidenceSources
    from tests.fixtures.wp05.geometry import STEPS, GeometryFixture

    fixture = GeometryFixture(tmp_path)

    async def run():
        masks = await fixture.masks()
        fields, session = fixture.geometry(masks)
        return masks, await fixture.call(STEPS[2], inputs=fields, session=session)

    masks, geometry = asyncio.run(run())
    (relation,) = geometry.facts
    assert isinstance(relation, KnownFact)
    assert runtime.runtime is not None
    child_context = tuple(
        (item.proof.completion.result.facts[0], item.proof.completion.selection)
        for item in masks
    )
    assert all(head is not None for _, head in child_context)
    children = tuple(item.proof.completion.result.execution for item in masks)
    catalog = fixture.resolved.catalogue
    policy = require_definition(
        catalog,
        fixture.tools[STEPS[2]].evidence_policy_ref,
        EvidencePolicyArtifact,
    ).policy
    assets = tuple(
        row
        for row in geometry.evidence
        if row.evidence_type is not EvidenceType.TOOL_OUTPUT
    )
    metadata = {
        EvidenceRef(
            record_kind=RuntimeRecordKind.EVIDENCE, record_id=row.evidence_id
        ): row
        for row in assets
    }
    original = fixture.original.asset
    content = {**fixture.sidecars, original.evidence_id: fixture.original.content}

    def check(execution, selected=child_context):
        # Actual selected children and physical result rows from the signed scene;
        # full finite authority and coverage are exercised above.
        sources = EvidenceSources(
            catalogue=catalog,
            task=fixture.task,
            media=fixture.media,
            obj=fixture.object,
            records=runtime.runtime.model_copy(
                update={"tool_executions": (*children, execution)}
            ),
            selected=selected,
            evidence=metadata,
            read_evidence=lambda row, limit: content.get(row.evidence_id),
            max_evidence_bytes=sum(map(len, content.values())),
        )
        return sources.required(policy, (relation,))

    shown, missing = check(geometry.execution)
    assert not missing
    assert set(row.evidence_id for row in shown) == {row.evidence_id for row in assets}
    (geometry_row,) = tuple(
        row for row in shown if row.evidence_type is EvidenceType.GEOMETRY
    )
    mask_rows = tuple(row for row in shown if row.evidence_type is EvidenceType.MASK)
    assert len(mask_rows) == 2
    assert all(row.retention != geometry_row.retention for row in mask_rows)
    without_second_parent, missing = check(geometry.execution, child_context[:1])
    assert missing
    assert all(
        row.evidence_type is not EvidenceType.MASK for row in without_second_parent
    )
    returned = geometry.execution.attempt_result
    assert isinstance(returned, ReturnedToolAttempt)
    names = {"wheel_mask": "tactile_paving_mask", "tactile_paving_mask": "wheel_mask"}
    swapped_result = returned.result.model_copy(
        update={
            "evidence": tuple(
                slot.model_copy(update={"name": names.get(slot.name, slot.name)})
                for slot in returned.result.evidence
            ),
        }
    )
    swapped = geometry.execution.model_copy(
        update={
            "attempt_result": returned.model_copy(update={"result": swapped_result}),
        }
    )
    shown, missing = check(swapped)
    assert missing
    assert all(row.evidence_type is not EvidenceType.MASK for row in shown)
