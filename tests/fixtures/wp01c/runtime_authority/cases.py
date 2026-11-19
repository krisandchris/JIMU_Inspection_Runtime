"""Build actual C roots separately from the B X3 claims they will corroborate."""

import json

from fixtures.wp01b.x3.integration import data, fixture, parse, registry
from fixtures.wp01c.nodes.cases import cases, content, load, ref, root

from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.validation.runtime_authority import (
    RuntimeAuthorityRecords,
    validate_runtime_authority,
)

START = "2026-11-17T11:59:58Z"
END = "2026-11-17T12:00:00Z"
CONTEXT_DIGEST = "d" * 64
CONFIG = content("policy", "workflow")
TRACE = {"trace_id": "trace-1"}


def _node(claim, attempt, output):
    return root(
        node_run_id=claim["node_run_ref"]["record_id"],
        execution_id=claim["execution_id"],
        node_id=claim["node_id"],
        trace=TRACE,
        config_ref=CONFIG,
        upstream_inputs=[],
        input_digest="e" * 64,
        status=claim["status"],
        created_at=START,
        started_at=START,
        ended_at=END,
        attempt_history=[
            {
                "attempt_ref": attempt["attempt_ref"],
                "attempt_number": attempt["attempt_number"],
            }
        ],
        output_refs=[output],
    )


def _attempt(claim):
    return root(
        attempt_id=claim["attempt_ref"]["record_id"],
        execution_id=claim["execution_id"],
        owner=claim["owner"],
        attempt_number=claim["attempt_number"],
        status=claim["status"],
        created_at=START,
        started_at=START,
        ended_at=END,
    )


def _invocation(template, result_id, node, attempt, binding, profiles, **changes):
    return template | {
        "model_invocation_id": result_id,
        "node_run_ref": node,
        "attempt_ref": attempt,
        "profile_refs": profiles,
        "started_at": START,
        "completed_at": END,
        "configured_provider": template["configured_provider"]
        | {"binding_ref": binding},
        **changes,
    }


def _model_pin(invocation):
    configured = invocation["configured_provider"]
    versions = invocation["actual_provider"]
    return {
        "binding_ref": configured["binding_ref"],
        "provider_policy_ref": configured["provider_policy_ref"],
        "fallback_policy_ref": invocation["route_trace"]["fallback_policy_ref"],
        "primary_deployment_id": configured["primary_deployment_id"],
        "deployments": [
            {
                "deployment_id": configured["primary_deployment_id"],
                "provider_id": configured["provider_id"],
                "provider_kind": configured["provider_kind"],
                "logical_model_id": configured["logical_model_id"],
                "actual_version_constraints": {
                    "allowed_deployment_versions": [versions["deployment_version"]],
                    "allowed_model_versions": [versions["model_version"]],
                },
                "required_features": ["rgb_image"],
            }
        ],
    }


def documents(case="question"):
    claims = fixture(case)
    history = claims["history"]
    template = cases()
    catalogue = claims["producer_catalogues"][0]
    profile = catalogue["context"]["profiles"][0]
    definition = profile["definition"]
    profile_ref = profile["ref"]
    execution_id = claims["execution_pins"][0]["execution_id"]
    release_id = claims["execution_pins"][0]["release_id"]
    bundle_digest = claims["execution_pins"][0]["bundle_digest"]
    context_ref = ref("context_snapshot", "context-1")
    release = load("wp01c/releases/release.json")
    release["release_id"] = release_id
    release["approval_receipt"]["release_ref"]["record_id"] = release_id
    gate = template["eligibility"] | {
        "bundle_digest": bundle_digest,
        "configured_profile_refs": [profile_ref],
        "eligible_profiles": [profile_ref],
        "context_snapshot_ref": context_ref,
    }
    actual_node_claims = list(history["node_runs"])
    actual_attempt_claims = list(history["attempts"])
    objects = []
    selections = []
    invocations = []
    batches = []
    tool_executions = []
    events = []
    node_outputs = []
    if case != "system":
        discovery_node = ref("node_run", "discovery-node")
        discovery_attempt = ref("attempt", "discovery-attempt")
        selection_ref = ref("question_selection_record", "question_selection_record-1")
        discovery = _invocation(
            template["discovery_invocation"],
            "discovery-1",
            discovery_node,
            discovery_attempt,
            definition["discovery_binding_ref"],
            [profile_ref],
        )
        invocations.append(discovery)
        questions = definition["question_refs"]
        selections.append(
            template["selection"]
            | {
                "profile_ref": profile_ref,
                "selection_id": selection_ref["record_id"],
                "node_run_id": discovery_node["record_id"],
                "attempt_id": discovery_attempt["record_id"],
                "context_snapshot_ref": context_ref,
                "context_snapshot_digest": CONTEXT_DIGEST,
                "configured_question_refs": questions,
                "mandatory_question_refs": questions,
                "selected_optional_question_refs": [],
                "effective_question_refs": questions,
                "created_at": END,
                "policy_ref": definition["question_selection_policy_ref"],
            }
        )
        selection_descriptor = {
            "question_selection_ref": selection_ref,
            "execution_id": execution_id,
            "profile_ref": profile_ref,
            "effective_question_refs": questions,
            "eligibility_qualification": "validated",
        }
        history["question_selections"] = [selection_descriptor]
        actual_node_claims.append(
            {
                "node_run_ref": discovery_node,
                "execution_id": execution_id,
                "node_id": "N02",
                "status": "succeeded",
            }
        )
        actual_attempt_claims.append(
            {
                "attempt_ref": discovery_attempt,
                "execution_id": execution_id,
                "attempt_number": 1,
                "status": "succeeded",
                "owner": {"owner_kind": "node", "node_run_ref": discovery_node},
            }
        )
        object_ref = history["objects"][0]["object_ref"]
        objects.append(
            template["object_record"]
            | {
                "object_id": object_ref["record_id"],
                "profile_ref": profile_ref,
                "question_selection_ref": selection_ref,
                "effective_question_refs": questions,
            }
        )
        fact = history["candidates"]["entries"][0]["fact"]
        primary_node = actual_node_claims[0]["node_run_ref"]
        primary_attempt = actual_attempt_claims[0]["attempt_ref"]
        if case == "question":
            observation = history["observation_results"][0]
            observation_invocation = _invocation(
                template["observation_invocation"],
                observation["model_invocation_ref"]["record_id"],
                primary_node,
                primary_attempt,
                definition["observation_binding_ref"],
                [profile_ref],
                object_ref=object_ref,
                contract_snapshot=template["observation_invocation"][
                    "contract_snapshot"
                ]
                | {
                    "effective_question_refs": questions,
                },
            )
            invocations.append(observation_invocation)
            batches.append(
                root(
                    observation_batch_id=observation["result_ref"]["record_id"],
                    execution_id=execution_id,
                    object_ref=object_ref,
                    node_run_ref=primary_node,
                    attempt_ref=primary_attempt,
                    model_invocation_ref=observation["model_invocation_ref"],
                    outcome="complete",
                    envelope_validation="trusted",
                    association_validation="trusted",
                    arrival_qualification="on_time",
                    question_selection_ref=selection_ref,
                    profile_ref=profile_ref,
                    effective_question_refs=questions,
                    validation_issues=[],
                    question_results=[
                        {
                            "question_ref": questions[0],
                            "primary_fact": fact,
                            "primary_fact_key": fact["fact_key"],
                            "primary_fact_schema_ref": fact["fact_schema_ref"],
                            "validation": {"status": "validated"},
                        }
                    ],
                )
            )
            node_outputs.append(observation["result_ref"])
        else:
            tool = history["tool_results"][0]
            tool_template = load("wp01c/invocations/contracts.json")["tools"]["step"]
            tool_executions.append(
                tool_template
                | {
                    "tool_execution_id": tool["result_ref"]["record_id"],
                    "profile_ref": profile_ref,
                    "tool_ref": tool["tool_ref"],
                    "node_run_ref": primary_node,
                    "attempt_ref": primary_attempt,
                    "object_ref": object_ref,
                    "actual_implementation_version": tool[
                        "actual_implementation_version"
                    ],
                    "started_at": START,
                    "completed_at": END,
                    "generated_fact_refs": [tool["mapped_outputs"][0]["fact_ref"]],
                    "invocation_origin": {
                        "kind": "tool_plan_step",
                        "node_id": "N05",
                        "tool_plan_ref": tool["tool_plan_ref"],
                        "step_id": tool["step_id"],
                        "producer_declaration_ids": [fact["producer_declaration_id"]],
                    },
                    "attempt_result": {
                        "disposition": "returned",
                        "result": {
                            "status": "succeeded",
                            "diagnostics": [],
                            "evidence_refs": [],
                            "evidence": [],
                            "payload": {"kind": "bool", "value": False},
                            "output_states": [
                                {
                                    "fact_status": "known",
                                    "output_name": "relation",
                                    "evidence_refs": [],
                                }
                            ],
                        },
                    },
                    "authority_observation": {
                        key: tool[key]
                        for key in (
                            "envelope_validation",
                            "association_validation",
                            "arrival_qualification",
                            "mapped_outputs",
                        )
                    },
                }
            )
            node_outputs.append(tool["result_ref"])
        node_outputs.append(selection_ref)
        history["node_runs"] = actual_node_claims
        history["attempts"] = actual_attempt_claims
    else:
        system = history["system_results"][0]
        events.append(
            root(
                execution_event_id=system["result_ref"]["record_id"],
                execution_id=execution_id,
                event_number=1,
                trace=TRACE,
                recorded_at=END,
                payload={
                    "event_kind": "system_output",
                    "attempt_ref": system["attempt_ref"],
                    "requirement": system["requirement"],
                    "output_name": system["output_name"],
                    "actual_implementation_version": system[
                        "actual_implementation_version"
                    ],
                    "authority_output": {
                        key: system[key]
                        for key in (
                            "execution_id",
                            "scope",
                            "arrival_qualification",
                            "output",
                        )
                    },
                },
            )
        )

    facts = [entry["fact"] for entry in history["candidates"]["entries"]]
    model_pins = [_model_pin(item) for item in invocations]
    tool_pins = [
        {
            "tool_ref": item["tool_ref"],
            "implementation_version": item["actual_implementation_version"],
        }
        for item in tool_executions
    ]
    manifest = load("wp01c/releases/bundle_manifest.json")
    inventory = [
        CONFIG,
        profile_ref,
        definition["entity_ref"],
        definition["question_selection_policy_ref"],
        catalogue["registration"]["ref"],
        *(
            item["ref"]
            for dir_name in ("questions", "tools", "plans")
            for item in catalogue["context"][dir_name]
        ),
        *(item["fact_schema_ref"] for item in facts),
        *(
            model[key]
            for model in model_pins
            for key in ("binding_ref", "provider_policy_ref", "fallback_policy_ref")
        ),
    ]
    refs = {(ref_["artifact_kind"], ref_["artifact_id"]): ref_ for ref_ in inventory}
    manifest["artifacts"] = [
        {"content_ref": ref_, "path": f"records/item-{index}.json"}
        for index, ref_ in enumerate(refs.values())
    ]
    records = {
        "execution": root(
            execution_id=execution_id,
            task_id="task-1",
            trace=TRACE,
            pins={
                "release_id": release_id,
                "bundle_digest": bundle_digest,
                "runtime_version": "1.0.0",
                "workflow_version": "1.0.0",
                "model_pins": model_pins,
                "tool_pins": tool_pins,
                "input_digest": "e" * 64,
            },
            status="running",
            created_at=START,
            started_at=START,
            context_snapshot={
                "snapshot_ref": context_ref,
                "snapshot_digest": CONTEXT_DIGEST,
            },
            model_invocation_refs=[
                ref("model_invocation", item["model_invocation_id"])
                for item in invocations
            ],
        ),
        "release": release,
        "manifest": manifest,
        "eligibility": gate,
        "objects": objects,
        "node_runs": [
            _node(node, attempt, output)
            for node, attempt, output in zip(
                actual_node_claims, actual_attempt_claims, node_outputs, strict=False
            )
        ],
        "attempts": [_attempt(claim) for claim in actual_attempt_claims],
        "question_selections": selections,
        "observations": batches,
        "tool_executions": tool_executions,
        "model_invocations": invocations,
        "execution_events": events,
        "facts": facts,
        "authority_selections": history["prior_selections"]
        + history["proposed_selections"],
    }
    # Detach real record documents from claim dictionaries for substitution tests.
    return json.loads(json.dumps(claims)), json.loads(json.dumps(records))


def claim_records(case="question"):
    claims, records = documents(case)
    return parse(claims), RuntimeAuthorityRecords.model_validate_json(
        json.dumps(records)
    )


def check(claims, records):
    bounds = data()
    return validate_runtime_authority(
        claims,
        records,
        fact_schemas=registry(),
        parse_limits=CandidateParseLimits.model_validate(bounds["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(
            bounds["snapshot_limits"]
        ),
    )


def project(case="question"):
    return check(*claim_records(case))
