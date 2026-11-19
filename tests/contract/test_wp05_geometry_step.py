"""N05 ledger witnesses select the exact durable ancestor output group."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import AttemptStatus
from jimu_runtime.domain.nodes.tool_execution import ToolStepHistoryEntry
from jimu_runtime.tools.step_attempts import (
    StepResult,
    _ancestor_pin,
    _check_ancestor_history,
    _check_result,
    _StepInput,
)
from tests.fixtures.wp05.geometry import STEPS, GeometryFixture


def test_step_ledger_requires_pinned_selected_mask_inputs_and_exact_links(tmp_path):
    fixture = GeometryFixture(tmp_path)

    async def run():
        masks = await fixture.masks()
        request, session = fixture.geometry(masks)
        result = await fixture.call(STEPS[2], inputs=request, session=session)
        assert result.execution.status == "succeeded"
        return masks, result

    masks, result = asyncio.run(run())
    plan = require_definition(
        fixture.resolved.catalogue,
        fixture.profile.tool_plan_ref,
        ToolPlanDefinition,
    )
    step = next(item for item in plan.steps if item.step_id == STEPS[2])
    site, node, attempt = fixture.calls[STEPS[2]]
    pins = tuple(
        _ancestor_pin(
            step, mask.proof.input_path, mask.proof.completion, mask.proof.asset
        )
        for mask in masks
    )
    bound = _StepInput(
        inputs=site.inputs,
        step=step,
        invocation_input_digest=sha256(fixture.requests[STEPS[2]][0]).hexdigest(),
        ancestor_inputs=pins,
    )
    legacy = bound.model_copy(update={"ancestor_inputs": ()})
    read_back = _StepInput.model_validate_json(legacy.stable_json())
    assert "ancestor_inputs" not in legacy.model_dump(mode="json")
    assert read_back.bound_digest() == legacy.bound_digest()
    assert not read_back.matches_reuse(bound)
    evidence = []
    for name, mask in zip(STEPS[:2], masks, strict=True):
        _, ancestor_node, ancestor_attempt = fixture.calls[name]
        completion = mask.proof.completion
        evidence.append(
            ToolStepHistoryEntry(
                attempt,
                ancestor_node,
                ancestor_attempt.model_copy(update={"status": AttemptStatus.SUCCEEDED}),
                completion.result.execution,
                completion.result.facts,
                completion.selection,
                completion.result.evidence,
                completion.result.invocations,
            )
        )

    _check_ancestor_history(bound, tuple(evidence))
    with pytest.raises(ValueError, match="explicitly selected durable child"):
        _check_ancestor_history(
            bound, (replace(evidence[0], selection=None), evidence[1])
        )
    with pytest.raises(ValueError, match="exact selected mask"):
        _ancestor_pin(
            step,
            masks[0].proof.input_path,
            masks[1].proof.completion,
            masks[1].proof.asset,
        )
    with pytest.raises(
        ValueError, match="requires an explicitly selected durable child"
    ):
        alias = evidence[0].selection.model_copy(
            update={"selection_id": "selection.other"}
        )
        _check_ancestor_history(
            bound, (replace(evidence[0], selection=alias), evidence[1])
        )

    tool = result.execution.model_copy(
        update={
            "authority_observation": result.execution.authority_observation.model_copy(
                update={"arrival_qualification": "on_time"}
            )
        }
    )
    checked = StepResult(execution=tool, facts=result.facts, evidence=result.evidence)
    kwargs = (node, attempt, "1.0.0", fixture.task.idempotency_scope)
    _check_result(bound, checked, *kwargs)
    injected = tool.model_copy(
        update={"input_refs": (*tool.input_refs, tool.output_refs[0])}
    )
    with pytest.raises(ValueError, match="immutable scope"):
        _check_result(
            bound, checked.model_copy(update={"execution": injected}), *kwargs
        )


def test_ancestor_uses_declared_output_name_and_evidence_slot_for_alias(tmp_path):
    fixture = GeometryFixture(tmp_path)
    wheel, _ = asyncio.run(fixture.masks())
    proof = wheel.proof
    result = proof.completion.result
    (fact,) = result.facts
    alias = "wheel_pixels_observation"
    definition = fixture.tools[STEPS[0]]
    (output,) = definition.fact_outputs
    aliased = definition.model_copy(
        update={
            "fact_outputs": (output.model_copy(update={"output_name": alias}),),
        }
    )
    tool = result.execution
    reply = tool.attempt_result
    returned = reply.result
    (state,) = returned.output_states
    normalized = returned.model_copy(
        update={
            "output_states": (state.model_copy(update={"output_name": alias}),),
        }
    )
    fact = fact.model_copy(
        update={"source": fact.source.model_copy(update={"output_name": alias})}
    )
    authority = tool.authority_observation
    (mapped,) = authority.mapped_outputs
    changed = proof.completion.model_copy(
        update={
            "result": result.model_copy(
                update={
                    "execution": tool.model_copy(
                        update={
                            "authority_observation": authority.model_copy(
                                update={
                                    "mapped_outputs": (
                                        mapped.model_copy(
                                            update={"output_name": alias}
                                        ),
                                    ),
                                },
                            ),
                            "attempt_result": reply.model_copy(
                                update={"result": normalized}
                            ),
                        },
                    ),
                    "facts": (fact,),
                },
            ),
        },
    )
    changed = type(changed).model_validate_json(changed.stable_json())
    plan = require_definition(
        fixture.resolved.catalogue, fixture.profile.tool_plan_ref, ToolPlanDefinition
    )
    step = next(item for item in plan.steps if item.step_id == STEPS[2])
    pin = _ancestor_pin(
        step, proof.input_path, changed, proof.asset, definition=aliased
    )
    assert pin.output_name == alias
    assert pin.evidence_slot == "wheel_mask"
    assert (
        _ancestor_pin(step, proof.input_path, changed, proof.asset, archived=pin) == pin
    )
    with pytest.raises(ValueError, match="unique selected Fact"):
        _ancestor_pin(
            step, proof.input_path, changed, proof.asset, definition=definition
        )
