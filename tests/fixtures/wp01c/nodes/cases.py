"""Deterministic data fixtures for the six node contracts."""

import json
from pathlib import Path

from pydantic import TypeAdapter

FIXTURES = Path(__file__).resolve().parents[2]
VERSION = "1.0.0"
PIN = "a" * 64
AT = "2026-11-17T02:31:00Z"
ISSUE = {
    "code": "invalid-value",
    "path": ["value"],
    "message": "Invalid value",
    "related_refs": [],
}


def load(relative):
    return json.loads((FIXTURES / relative).read_text())


def root(**fields):
    return {"schema_version": VERSION, **fields}


def ref(kind, record_id):
    return {"record_kind": kind, "record_id": record_id}


def content(kind, artifact_id):
    return {
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "version": VERSION,
        "digest": PIN,
    }


def parse(model, value):
    return TypeAdapter(model).validate_json(json.dumps(value))


def envelope(node, status=None, artifacts=()):
    identity = {
        "node_id": node,
        "node_run_id": node.lower(),
        "attempt_id": "attempt-1",
        "trace": {"trace_id": "trace-1", "span_id": None, "parent_span_id": None},
    }
    if status is None:
        return root(
            **identity,
            execution_id="execution-1",
            task_id="task-1",
            release_id="release-1",
            bundle_digest="1" * 64,
            upstream_artifacts=[],
            node_config_ref=content("policy", "workflow"),
        )
    out = root(
        **identity,
        status=status,
        output_artifacts=list(artifacts),
        started_at=AT,
        finished_at=AT,
        duration_ms=0,
    )
    if status != "succeeded":
        out["error"] = {
            "code": "invalid-context",
            "error_class": "validation",
            "retryable": False,
            "message": "Required context failed validation",
            "validation_issues": [ISSUE],
        }
    return out


def cases():
    task = load("wp01c/intake/accepted-task-valid.json")
    media = load("wp01c/intake/media-asset-valid.json")
    snapshot = load("wp01c/intake/context-snapshot-valid.json")
    gateway = load("wp01c/invocations/contracts.json")
    model = gateway["models"]["observation"]
    discovery = gateway["models"]["discovery"]
    selection = gateway["selections"]["all"]
    prof = gateway["profile"]["definition"]
    profile = model["profile_refs"][0]
    questions = model["contract_snapshot"]["effective_question_refs"]
    selected = ref("question_selection_record", selection["selection_id"])
    frame = {
        "coordinate_system_id": "original",
        "frame": "original_image",
        "dimensions": media["dimensions"],
    }
    original = media["media_ref"]
    selection_data = {
        "profile_ref": profile,
        "effective_question_refs": questions,
        "question_selection_ref": selected,
    }
    gate_ref = ref("profile_eligibility_evaluation", "gate-1")
    snapshot_ref = ref("context_snapshot", snapshot["context_snapshot_id"])
    object_ref = model["object_ref"]
    tool = gateway["tools"]["grounding"]
    tool_ref = ref("tool_execution", tool["tool_execution_id"])
    model_ref = ref("model_invocation", discovery["model_invocation_id"])
    observation_ref = ref("model_invocation", model["model_invocation_id"])
    grounding_ref = ref("grounding_record", "grounding-1")
    grounding_provenance = {
        "kind": "tool",
        "tool_ref": tool["tool_ref"],
        "tool_execution_ref": tool_ref,
        "actual_implementation_version": tool["actual_implementation_version"],
        "nested_model_invocation_refs": [],
    }
    object_record = root(
        **selection_data,
        object_id=object_ref["record_id"],
        execution_id=model["execution_id"],
        entity_ref=prof["entity_ref"],
        grounding_record_ref=grounding_ref,
        source_media_ref=original,
        coordinate_system=frame,
        bbox={"xyxy": [1.0, 2.0, 8.0, 9.0]},
        grounding_producer=grounding_provenance,
        visibility="visible",
        localization_status="localized",
        evidence_refs=[],
    )
    grounding_record = root(
        **selection_data,
        grounding_record_id=grounding_ref["record_id"],
        execution_id=model["execution_id"],
        node_run_id="n03",
        attempt_id=tool["attempt_ref"]["record_id"],
        grounding_binding=prof["grounding_binding"],
        source_media_ref=original,
        coordinate_system=frame,
        transforms=[],
        outcome="objects",
        object_refs=[object_ref],
        evidence_refs=[],
        producer=grounding_provenance,
        validation_issues=[],
    )
    result = {
        **selection_data,
        "outcome": "objects",
        "grounding_record_ref": grounding_ref,
        "object_refs": [object_ref],
        "validation_issues": [],
    }
    hit = {**selection_data, "model_invocation_ref": model_ref}
    context_input = root(
        envelope=envelope("N01"),
        accepted_task=task,
        media_asset=media,
        **{
            field: snapshot[field]
            for field in (
                "device_ref",
                "camera_ref",
                "region_ref",
                "context_schema_ref",
                "context_provenance_policy_ref",
            )
        },
    )
    context_output = root(
        envelope=envelope("N01", "succeeded", [{"ref": snapshot_ref, "digest": PIN}]),
        disposition="usable",
        validation_report={"disposition": "usable", "optional_issues": []},
        context_snapshot_ref=snapshot_ref,
        context_snapshot_digest=PIN,
    )
    context_failure = root(
        envelope=envelope("N01", "failed"),
        disposition="required_context_failure",
        validation_report={
            "disposition": "required_context_failure",
            "required_issues": [ISSUE],
            "optional_issues": [],
        },
    )
    discovery_snapshot = selection["context_snapshot_ref"]
    discovery_input = root(
        envelope=envelope("N02"),
        original_media_ref=original,
        context_snapshot_ref=discovery_snapshot,
        context_snapshot_digest=selection["context_snapshot_digest"],
        profile_eligibility_evaluation_ref=gate_ref,
        eligible_profiles=[profile],
        bindings=[
            {
                "profile_ref": profile,
                "discovery_binding_ref": prof["discovery_binding_ref"],
                "prompt_template_ref": content("prompt_template", "discovery"),
                "question_selection_policy_ref": prof["question_selection_policy_ref"],
            },
        ],
    )
    discovery_output = root(
        envelope=envelope("N02", "succeeded"),
        execution_id=model["execution_id"],
        outcome="hits",
        profile_hits=[hit],
        diagnostics=[],
        model_invocation_refs=[model_ref],
    )
    skipped_discovery = root(
        outcome="not_evaluated",
        node_id="N02",
        status="skipped",
        reason_code="no_eligible_profiles",
        node_run_id="n02",
        execution_id=model["execution_id"],
        profile_eligibility_evaluation_ref=gate_ref,
    )
    discovery_diagnostic = root(
        diagnostic_id="discovery-invalid-1",
        execution_id=model["execution_id"],
        model_invocation_ref=model_ref,
        raw_response_ref=ref("evidence", "rejected-profile-proposal"),
        validation_issues=[ISSUE],
        reason_code="invalid_profile_proposal",
    )
    grounding_input = root(
        envelope=envelope("N03"),
        original_media_ref=original,
        original_coordinate_system=frame,
        profiles=[{"hit": hit, "grounding_binding": prof["grounding_binding"]}],
    )
    grounding_output = root(
        envelope=envelope("N03", "succeeded"),
        profile_results=[result],
    )
    image_view = {"roi": object_record["bbox"], "transforms": []}
    question_results = []
    joint_questions = []
    facts = load("wp01b/a1/contracts.json")
    batch_ref = ref("observation_batch", "batch-1")
    for i, (question, fact_status) in enumerate(
        zip(questions, ("known", "error"), strict=True)
    ):
        key = "parked" if i == 0 else "vehicle-kind"
        fact_schema = content("fact_schema", key)
        fact = facts[fact_status] | {
            "fact_id": f"fact-{i}",
            "object_id": object_ref["record_id"],
            "profile_ref": profile,
            "producer_id": question["artifact_id"],
            "fact_key": key,
            "fact_schema_ref": fact_schema,
            "source": facts[fact_status]["source"]
            | {
                "question_ref": question,
                "observation_batch_ref": batch_ref,
                "model_invocation_ref": observation_ref,
            },
        }
        fact_identity = {
            "question_ref": question,
            "primary_fact_key": key,
            "primary_fact_schema_ref": fact_schema,
        }
        joint_questions.append(
            fact_identity
            | {
                "prompt_template_ref": content("prompt_template", key),
                "context_view": [],
            }
        )
        validation = (
            {"status": "validated"}
            if fact_status == "known"
            else {"status": "invalid", "issues": [ISSUE]}
        )
        question_results.append(
            fact_identity | {"validation": validation, "primary_fact": fact}
        )
    observation_input = root(
        envelope=envelope("N04"),
        object_record=object_record,
        context_snapshot_ref=discovery_snapshot,
        image_view=image_view,
        observation_binding_ref=model["configured_provider"]["binding_ref"],
        compiled_prompt_evidence_ref=model["evidence_refs"][0],
        contract_snapshot=model["contract_snapshot"],
        joint_questions=joint_questions,
    )
    batch = root(
        **selection_data,
        observation_batch_id=batch_ref["record_id"],
        execution_id=model["execution_id"],
        object_ref=object_ref,
        node_run_ref=model["node_run_ref"],
        attempt_ref=model["attempt_ref"],
        model_invocation_ref=observation_ref,
        outcome="partial",
        envelope_validation="trusted",
        association_validation="trusted",
        arrival_qualification="on_time",
        question_results=question_results,
        validation_issues=[],
    )
    plan_ref = prof["tool_plan_ref"]
    tool_input = root(
        envelope=envelope("N05"),
        object_record=object_record,
        image_view=image_view,
        context_snapshot_ref=discovery_snapshot,
        tool_plan_ref=plan_ref,
        validated_observation_fact_refs=[],
    )
    tool_output = root(
        envelope=envelope("N05", "succeeded"),
        tool_fact_refs=[],
        evidence_refs=[],
        summary={
            "tool_plan_ref": plan_ref,
            "object_ref": object_ref,
            "status": "succeeded",
            "steps": [
                {
                    "disposition": "skipped",
                    "step_id": gateway["tool_plan"]["definition"]["steps"][0][
                        "step_id"
                    ],
                    "reason_code": "declared-skip",
                    "validation_issues": [],
                }
            ],
            "validation_issues": [],
        },
    )
    decision_input = root(
        envelope=envelope("N06"),
        object_record=object_record,
        context_snapshot_ref=discovery_snapshot,
        profile_eligibility_evaluation_ref=gate_ref,
        fact_refs=[],
        authority_selection_refs=[],
        mandatory_rule_paths=[
            {
                "rule_set_ref": content("rule_set", "parking"),
                "rule_ref": content("rule", "bike-lane"),
            }
        ],
    )
    decision_output = root(
        envelope=envelope("N06", "succeeded"),
        decision_ref=ref("decision", "decision-1"),
        execution_coverage_ref=ref("execution_coverage", "coverage-1"),
        mandatory_rule_coverage={
            "scan": "all_mandatory",
            "obligations": [
                {
                    "obligation_id": "mandatory-1",
                    "profile_ref": profile,
                    "target": {
                        "scope": "rule",
                        "object_ref": object_ref,
                        **decision_input["mandatory_rule_paths"][0],
                    },
                    "outcome": {
                        "disposition": "dependency_not_evaluated",
                        "reason": "required_evidence_blocked",
                        "blocker_node_run_ref": ref("node_run", "n05"),
                    },
                }
            ],
        },
    )
    gate = root(
        evaluation_id="gate-1",
        execution_id=model["execution_id"],
        release_id="release-1",
        bundle_digest="1" * 64,
        context_snapshot_ref=discovery_snapshot,
        workflow_version=VERSION,
        evaluated_at=AT,
        configured_profile_refs=[profile],
        eligible_profiles=[profile],
        excluded_profiles=[],
        unresolved_profiles=[],
        applicability_traces=[ref("applicability_trace", "profile-trace-1")],
    )
    return {
        "input_envelope": envelope("N01"),
        "failed_envelope": envelope("N01", "failed"),
        "cancelled_envelope": envelope("N01", "cancelled"),
        "output_envelope": envelope("N01", "succeeded"),
        "context_input": context_input,
        "context_output": context_output,
        "context_failure": context_failure,
        "snapshot": snapshot,
        "discovery_input": discovery_input,
        "discovery_output": discovery_output,
        "discovery_diagnostic": discovery_diagnostic,
        "skipped_discovery": skipped_discovery,
        "eligibility": gate,
        "grounding_input": grounding_input,
        "grounding_output": grounding_output,
        "grounding_record": grounding_record,
        "object_record": object_record,
        "observation_input": observation_input,
        "batch": batch,
        "observation_invocation": model,
        "discovery_invocation": discovery,
        "selection": selection,
        "grounding_tool": tool,
        "step_tool": gateway["tools"]["step"],
        "profile": prof,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "tool_plan": gateway["tool_plan"]["definition"],
        "decision_input": decision_input,
        "decision_output": decision_output,
    }


def decision_case():
    from fixtures.wp01c.applicability.cases import wire_case

    wire = cases()
    inputs, output = wire["decision_input"], wire["decision_output"]
    _, records = wire_case("H05", object_scope=True)
    gate = records["profile_eligibility"]
    rule_set, rule = records["rule_set_evaluations"][0], records["rule_evaluations"][0]
    object_ref = rule["object_ref"]
    profile_ref = rule["profile_ref"]
    execution_pin = {
        name: gate[name] for name in ("execution_id", "release_id", "bundle_digest")
    }
    node_pin = {
        "node_run_id": rule["node_run_ref"]["record_id"],
        "attempt_id": rule["attempt_ref"]["record_id"],
    }
    gate_ref = ref("profile_eligibility_evaluation", gate["evaluation_id"])
    rule_set_evaluation_ref = ref(
        "rule_set_applicability_evaluation", rule_set["evaluation_id"]
    )
    rule_evaluation_ref = ref("rule_applicability_evaluation", rule["evaluation_id"])
    inputs["envelope"].update(execution_pin | node_pin)
    output["envelope"].update(node_pin)
    path = {"rule_set_ref": rule["rule_set_ref"], "rule_ref": rule["rule_ref"]}
    inputs.update(
        context_snapshot_ref=gate["context_snapshot_ref"],
        profile_eligibility_evaluation_ref=gate_ref,
        mandatory_rule_paths=[path],
    )
    inputs["object_record"].update(
        object_id=object_ref["record_id"],
        profile_ref=profile_ref,
        execution_id=gate["execution_id"],
    )
    decision_ref = output["decision_ref"]
    candidate_ref = ref("candidate_prediction", "advisory-failed")
    output["candidate_prediction_ref"] = candidate_ref
    rule_trace_ref = rule["applicability_trace_ref"]
    rule_set_trace_ref = rule_set["applicability_trace_ref"]
    profile_trace_ref = gate["applicability_traces"][0]
    selected_scope = [
        ("profile-obligation", {"scope": "profile"}, gate_ref, profile_trace_ref),
        (
            "rule-set-obligation",
            {
                "scope": "rule_set",
                "rule_set_ref": rule["rule_set_ref"],
                "object_ref": object_ref,
            },
            rule_set_evaluation_ref,
            rule_set_trace_ref,
        ),
        (
            "mandatory-1",
            {"scope": "rule", **path, "object_ref": object_ref},
            rule_evaluation_ref,
            rule_trace_ref,
        ),
    ]
    obligations = []
    for obligation_id, target, evaluation_ref, trace_ref in selected_scope:
        outcome = {
            "disposition": "included",
            "selected_applicability": {
                "evaluation_ref": evaluation_ref,
                "trace_ref": trace_ref,
            },
        }
        if target["scope"] == "rule":
            outcome.update(disposition="evaluated", decision_ref=decision_ref)
        obligations.append(
            {
                "obligation_id": obligation_id,
                "profile_ref": profile_ref,
                "target": target,
                "outcome": outcome,
            }
        )
    output["mandatory_rule_coverage"]["obligations"] = obligations
    coverage_ref = output["execution_coverage_ref"]
    obligation_ids = [entry["obligation_id"] for entry in obligations]
    coverage = root(
        **execution_pin,
        coverage_id=coverage_ref["record_id"],
        applicability_scope={
            "scope_status": "evaluated",
            "context_snapshot_ref": gate["context_snapshot_ref"],
            "profile_eligibility_evaluation_ref": gate_ref,
            "configured_profile_refs": [profile_ref],
        },
        discovery_outcome="hits",
        discovery_model_invocation_refs=[ref("model_invocation", "discovery-1")],
        hit_profile_refs=[profile_ref],
        profiles=[
            root(
                **execution_pin,
                profile_ref=profile_ref,
                grounding_outcome="objects",
                object_refs=[object_ref],
                object_count=1,
                obligation_ids=obligation_ids,
                coverage_status="complete",
                reasons=[],
                missing_evidence_refs=[],
            )
        ],
        mandatory_rule_coverage=output["mandatory_rule_coverage"],
        selected_applicability_evaluation_refs=[entry[2] for entry in selected_scope],
        applicability_trace_refs=[entry[3] for entry in selected_scope],
        unresolved_obligation_refs=[],
        object_count=1,
        coverage_status="complete",
        reasons=[],
        missing_evidence_refs=[],
        summary_result="no_violation",
        matches=[],
        created_at=AT,
    )
    decision_pins = {
        **execution_pin,
        "context_snapshot_ref": gate["context_snapshot_ref"],
        "scope": "object",
        "profile_ref": profile_ref,
        "object_ref": object_ref,
        "node_run_ref": rule["node_run_ref"],
        "attempt_ref": rule["attempt_ref"],
    }
    decision = root(
        **decision_pins,
        decision_id=decision_ref["record_id"],
        profile_eligibility_evaluation_ref=gate_ref,
        candidate_prediction_ref=candidate_ref,
        execution_coverage_ref=coverage_ref,
        rule_engine_version=VERSION,
        result="no_violation",
        rule_evaluations=[
            {
                "rule": path,
                "selected_rule_set_evaluation_ref": rule_set_evaluation_ref,
                "selected_rule_evaluation_ref": rule_evaluation_ref,
                "applicability_trace_ref": rule_trace_ref,
                "result": {
                    "outcome": "evaluated",
                    "predicate": {"truth": "false", "diagnostic_codes": []},
                    "guard": {
                        "outcome": "complete",
                        "required_fact_refs": [],
                        "required_evidence_refs": [],
                        "authoritative_result_selection_refs": [],
                        "fact_proofs": [],
                    },
                    "matched": False,
                },
                "fact_refs": [],
                "evidence_refs": [],
            }
        ],
        explanation={
            "applicable_rules": [path],
            "evaluated_rules": [path],
            "skipped_rules": [],
            "blocked_rules": [],
            "not_evaluated_rules": [],
            "applicability_trace_refs": [entry[3] for entry in selected_scope],
            "obligation_ids": obligation_ids,
            "fact_refs": [],
            "evidence_refs": [],
        },
        conflict_resolution={
            "mode": "collect_all",
            "conflict_policy_refs": [],
            "matched_rules": [],
            "matched_category_ids": [],
        },
        created_at=AT,
    )
    candidate = root(
        **decision_pins,
        prediction_id=candidate_ref["record_id"],
        status="failed",
        categories=[],
        model_invocation_ref=ref("model_invocation", "candidate-call"),
        advisory_only=True,
        failure_code="transport",
        created_at=AT,
    )
    return {
        "input": inputs,
        "output": output,
        "decision": decision,
        "coverage": coverage,
        "candidate": candidate,
        "gate": gate,
        "rule_set": rule_set,
        "rule": rule,
    }
