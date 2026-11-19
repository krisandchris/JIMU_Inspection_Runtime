"""Finite execution/node/Attempt proofs and exact definition pins."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.producers import ProducerCatalogue
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.schema_types import ToolResultSchemaDescriptor
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeRunDescriptor,
    ObjectDescriptor,
)
from jimu_runtime.domain.validation.definition_inputs import (
    ProfileRegistration,
    ToolPlanRegistration,
)
from jimu_runtime.domain.validation.invocations import (
    validate_model_invocation,
    validate_question_selection_record,
    validate_tool_execution,
)
from jimu_runtime.domain.validation.policies import (
    ModelBindingRegistration,
    PolicyRegistration,
)
from jimu_runtime.domain.validation.review_inputs import ReviewMetadata
from jimu_runtime.domain.validation.tool_plans import ToolRegistration

_FIXTURES = (
    Path(__file__).parents[1] / "fixtures" / "wp01c" / "invocations" / "contracts.json"
)


def parse(model, payload):
    return TypeAdapter(model).validate_json(json.dumps(payload))


@pytest.fixture
def audit():
    return json.loads(_FIXTURES.read_text())


def model_inputs(audit, model_case):
    return dict(
        node_run=parse(NodeRunDescriptor, audit["nodes"]["N04"]),
        attempt=parse(
            AttemptDescriptor,
            audit["attempts"]["N04"][int(model_case == "fallback")],
        ),
        binding=parse(ModelBindingRegistration, audit["model_binding"]),
        provider_policy=parse(
            PolicyRegistration,
            audit["local_provider"]
            if model_case == "local"
            else audit["policies"]["provider"],
        ),
        fallback_policy=parse(
            PolicyRegistration,
            audit["evaluated_fallback"]
            if model_case == "fallback"
            else audit["policies"]["fallback"],
        ),
        prior_invocations=(parse(ModelInvocation, audit["models"]["failure"]),),
        reviews=(parse(ReviewMetadata, audit["review"]),),
    )


@pytest.mark.parametrize(
    "case", ["observation", "fallback", "local", "unavailable", "undispatched"]
)
def test_actual_model_routes_resolve_exact_pins(audit, case):
    validate_model_invocation(
        parse(ModelInvocation, audit["models"][case]), **model_inputs(audit, case)
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_prior", "missing model invocation"),
        ("different_execution", "missing model invocation"),
        ("wrong_review", "does not cover exact transition"),
        ("expired_review", "does not cover exact transition"),
        ("missing_review", "requires its exact independent evaluation"),
        ("disabled", "exact evaluated transition"),
    ],
)
def test_model_fallback_requires_actual_prior_and_evaluated_transition(
    audit, mutation, message
):
    kwargs = model_inputs(audit, "fallback")
    if mutation == "missing_prior":
        kwargs["prior_invocations"] = ()
    if mutation == "different_execution":
        prior = dict(audit["models"]["failure"], execution_id="another")
        kwargs["prior_invocations"] = (parse(ModelInvocation, prior),)
    if mutation == "wrong_review":
        report = deepcopy(audit["review"])
        report["assessments"][0]["target"]["model_version"] = "2.0.0"
        kwargs["reviews"] = (parse(ReviewMetadata, report),)
    if mutation == "expired_review":
        report = dict(audit["review"], valid_until="2026-03-01T00:00:00Z")
        kwargs["reviews"] = (parse(ReviewMetadata, report),)
    if mutation == "missing_review":
        kwargs["reviews"] = ()
    if mutation == "disabled":
        kwargs["fallback_policy"] = parse(
            PolicyRegistration, audit["policies"]["fallback"]
        )
    with pytest.raises(ValueError, match=message):
        validate_model_invocation(
            parse(ModelInvocation, audit["models"]["fallback"]), **kwargs
        )


def selection_inputs(audit, key):
    record = parse(QuestionSelectionRecord, audit["selections"][key])
    invocations = (
        (audit["models"]["shared"],)
        if key in {"shared", "fallback_shared"}
        else (audit["models"]["discovery"], audit["models"]["additional"])
    )
    return record, dict(
        profile=parse(ProfileRegistration, audit["profile"]),
        policy=parse(PolicyRegistration, audit["selection_policies"][key]),
        context_snapshot_ref=parse(
            ContextSnapshotRef, audit["selections"][key]["context_snapshot_ref"]
        ),
        context_snapshot_digest=record.context_snapshot_digest,
        node_run=parse(NodeRunDescriptor, audit["nodes"]["N02"]),
        attempts=tuple(
            parse(AttemptDescriptor, attempt) for attempt in audit["attempts"]["N02"]
        ),
        invocations=tuple(
            parse(ModelInvocation, invocation) for invocation in invocations
        ),
        trusted_discovery_hit_invocation_id="discovery-1",
        additional_binding=parse(ModelBindingRegistration, audit["additional_binding"]),
        additional_resources=parse(PolicyRegistration, audit["policies"]["resources"]),
    )


@pytest.mark.parametrize(
    "key",
    ["all", "context", "shared", "additional", "fallback_context", "fallback_shared"],
)
def test_selection_modes_and_failed_proposals_resolve(audit, key):
    record, kwargs = selection_inputs(audit, key)
    validate_question_selection_record(record, **kwargs)


@pytest.mark.parametrize(
    "fault",
    [
        "context",
        "failed_discovery",
        "proposal_scope",
        "duplicate_attempt",
        "budget",
        "resource",
    ],
)
def test_selection_relations_fail_closed(audit, fault):
    record, kwargs = selection_inputs(audit, "additional")
    discovery, proposal = kwargs["invocations"]
    if fault == "context":
        kwargs["context_snapshot_digest"] = "f" * 64
    if fault == "failed_discovery":
        failed = dict(
            audit["models"]["discovery"], outcome=audit["models"]["failure"]["outcome"]
        )
        kwargs["invocations"] = (parse(ModelInvocation, failed), proposal)
    if fault == "proposal_scope":
        kwargs["invocations"] = (
            discovery,
            parse(
                ModelInvocation, dict(audit["models"]["additional"], profile_refs=[])
            ),
        )
    if fault == "duplicate_attempt":
        kwargs["attempts"] += (kwargs["attempts"][1],)
    if fault == "budget":
        retry = dict(audit["models"]["additional"], model_invocation_id="retry")
        kwargs["invocations"] += (parse(ModelInvocation, retry),)
    if fault == "resource":
        kwargs.pop("additional_resources")
    with pytest.raises(ValueError):
        validate_question_selection_record(record, **kwargs)


def tool_inputs(audit, key):
    node, attempt_index = ("N03", 0) if key == "grounding" else ("N05", 1)
    return dict(
        node_run=parse(NodeRunDescriptor, audit["nodes"][node]),
        attempt=parse(AttemptDescriptor, audit["attempts"][node][attempt_index]),
        profile=parse(ProfileRegistration, audit["profile"]),
        tool=parse(ToolRegistration, audit["tool"]),
        object_record=parse(ObjectDescriptor, audit["object"]),
        plan=parse(ToolPlanRegistration, audit["tool_plan"]),
        output_schema=parse(ToolResultSchemaDescriptor, audit["output_schema"]),
        producer_catalogue_ref=parse(
            ProducerCatalogueContentRef, audit["producer_catalogue_ref"]
        ),
        producer_catalogue=parse(ProducerCatalogue, audit["producer_catalogue"]),
    )


@pytest.mark.parametrize("key", ["grounding", "step", "failure", "undispatched"])
def test_same_tool_distinct_origin_and_attempt(audit, key):
    execution = parse(ToolExecution, audit["tools"][key])
    other = "step" if key == "grounding" else "grounding"
    validate_tool_execution(
        execution,
        **tool_inputs(audit, key),
        peer_executions=(parse(ToolExecution, audit["tools"][other]),),
    )


@pytest.mark.parametrize(
    "fault", ["object", "attempt", "duplicate", "plan", "schema", "implementation"]
)
def test_tool_rejects_mismatched_scoped_or_pinned_data(audit, fault):
    execution = audit["tools"]["step"]
    kwargs = tool_inputs(audit, "step")
    if fault == "object":
        kwargs["object_record"] = parse(
            ObjectDescriptor, dict(audit["object"], execution_id="another")
        )
    if fault == "attempt":
        kwargs["attempt"] = parse(AttemptDescriptor, audit["attempts"]["N03"][0])
    if fault == "duplicate":
        kwargs["peer_executions"] = (parse(ToolExecution, execution),)
    if fault == "plan":
        kwargs.pop("plan")
    if fault == "schema":
        kwargs.pop("output_schema")
    if fault == "implementation":
        execution["actual_implementation_version"] = "2.0.0"
    with pytest.raises(ValueError):
        validate_tool_execution(parse(ToolExecution, execution), **kwargs)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "non_known_with_value",
        "missing_state",
        "different_fact",
        "different_source",
    ],
)
def test_named_tool_fact_path_and_authority_evidence(audit, fault):
    from fixtures.wp01b.r1.support import registration

    from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
    from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistryBuilder
    from jimu_runtime.domain.runtime.facts import FactRecord

    schema, definition, _ = registration(
        {"kind": "enum", "members": ["parked", "moving"]}
    )
    definition = FactSchemaDefinition.model_validate(
        {
            **definition.model_dump(),
            "allowed_producer_kinds": ("tool",),
        }
    )
    registry = FactSchemaRegistryBuilder().register(schema, definition).freeze()
    ref = schema.model_dump(mode="json")
    raw = audit["tools"]["step"]
    origin = raw["invocation_origin"]
    tool = audit["tool"]
    fact_key = definition.semantic_key
    producer_id = "geometry-output"
    raw["generated_fact_refs"] = [{"record_kind": "fact", "record_id": "geometry-1"}]
    origin["producer_declaration_ids"] = [producer_id]
    tool["definition"]["fact_outputs"] = [
        {
            "output_name": "state",
            "fact_key": fact_key,
            "fact_schema_ref": ref,
            "scope": "object",
            "required": True,
            "value_path": ["state"],
        }
    ]
    audit["tool_plan"]["definition"]["steps"][0]["fact_output_mappings"] = [
        {"output_name": "state", "target_fact_key": fact_key}
    ]
    audit["output_schema"]["output_declarations"] = [
        {"output_name": "state", "value_path": ["state"], "required": True}
    ]
    audit["producer_catalogue"]["declarations"] = [
        {
            "declaration_id": producer_id,
            "scope": "object",
            "profile_ref": raw["profile_ref"],
            "producer_kind": "tool",
            "producer_id": "tool",
            "producer_version": "1.0.0",
            "producer": {
                "producer_kind": "tool",
                "tool_plan_ref": origin["tool_plan_ref"],
                "tool_ref": raw["tool_ref"],
                "step_id": "one",
            },
            "output_name": "state",
            "fact_key": fact_key,
            "fact_schema_ref": ref,
            "cardinality": "exactly_one",
            "activation_condition": {"mode": "always"},
        }
    ]
    output = raw["attempt_result"]["result"]
    output["payload"] = {
        "kind": "record",
        "value": {"state": {"kind": "enum", "value": "parked"}},
    }
    output["output_states"] = [
        {"output_name": "state", "fact_status": "known", "evidence_refs": []}
    ]
    fact = {
        "schema_version": "1.0.0",
        "fact_id": "geometry-1",
        "execution_id": raw["execution_id"],
        "scope": "object",
        "object_id": raw["object_ref"]["record_id"],
        "profile_ref": raw["profile_ref"],
        "fact_key": fact_key,
        "fact_schema_ref": ref,
        "producer_catalogue_ref": audit["producer_catalogue_ref"],
        "producer_declaration_id": producer_id,
        "producer_kind": "tool",
        "producer_id": "tool",
        "producer_version": "1.0.0",
        "attempt_id": raw["attempt_ref"]["record_id"],
        "source": {
            "source_kind": "tool",
            "tool_execution_ref": {
                "record_kind": "tool_execution",
                "record_id": raw["tool_execution_id"],
            },
            "tool_plan_ref": origin["tool_plan_ref"],
            "step_id": "one",
            "tool_ref": raw["tool_ref"],
            "output_name": "state",
        },
        "status": "known",
        "value": {"kind": "enum", "value": "parked"},
        "evidence_refs": [],
        "created_at": raw["completed_at"],
        "trace_id": raw["trace_id"],
    }
    if fault == "missing_state":
        output["output_states"] = []
    if fault == "non_known_with_value":
        output["output_states"][0].update(
            fact_status="unknown",
            reason={
                "reason_code": "occluded",
                "message": "Synthetic unknown",
                "diagnostic_refs": [],
            },
        )
    if fault == "different_fact":
        fact["value"]["value"] = "moving"
    if fault == "different_source":
        fact["source"]["tool_execution_ref"]["record_id"] = audit["tools"]["grounding"][
            "tool_execution_id"
        ]
    if fault == "none":
        raw["authority_observation"] = {
            "arrival_qualification": "on_time",
            "envelope_validation": "trusted",
            "association_validation": "trusted",
            "mapped_outputs": [
                {
                    "disposition": "emitted",
                    "output_name": "state",
                    "fact_status": "known",
                    "fact_ref": raw["generated_fact_refs"][0],
                }
            ],
        }
    execution = parse(ToolExecution, raw)
    kwargs = dict(
        **tool_inputs(audit, "step"),
        fact_schemas=registry,
        generated_facts=(parse(FactRecord, fact),),
    )
    if fault == "none":
        validate_tool_execution(execution, **kwargs)
    else:
        with pytest.raises(ValueError):
            validate_tool_execution(execution, **kwargs)
