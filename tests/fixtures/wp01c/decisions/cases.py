"""Synthetic selected record sets used to check static Decision/Coverage consistency."""

import json
from copy import deepcopy
from pathlib import Path

from fixtures.wp01c.applicability.cases import (
    TIME,
    VERSION,
    content_ref,
    runtime_ref,
    wire_case,
)

_FIXTURES = Path(__file__).parents[1]
_RULE = json.loads((_FIXTURES / "rule_declarations/rule-valid.json").read_text())
_RULE_SET = json.loads(
    (_FIXTURES / "rule_declarations/rule-set-valid.json").read_text()
)
_OBSERVATION = json.loads((_FIXTURES / "invocations/contracts.json").read_text())[
    "models"
]["observation"]


def profile_rules(scope, *, needs_fact):
    _, rule_set, rule = scope["target_bindings"]
    definition = deepcopy(_RULE)
    required = definition["required_fact_selectors"]
    if needs_fact:
        condition = {"kind": "fact_ref", "selector": required[0]}
    else:
        required = []
        condition = {
            "kind": "literal",
            "constant": {
                "type_definition": {"kind": "bool"},
                "value": {"kind": "bool", "value": False},
            },
        }
    definition.update(
        rule_id=rule["target_ref"]["artifact_id"],
        applicability_policy_ref=rule["policy_ref"],
        required_fact_selectors=required,
        optional_fact_selectors=[],
        condition_ast=condition,
    )
    group = {
        **deepcopy(_RULE_SET),
        "rule_set_id": rule_set["target_ref"]["artifact_id"],
        "applicability_policy_ref": rule_set["policy_ref"],
        "rule_refs": [rule["target_ref"]],
        "candidate_binding_ref": _OBSERVATION["configured_provider"]["binding_ref"],
    }
    rule_policy = {
        "policy_kind": "rule",
        "mandatory": True,
        "missing": "unknown",
        "unknown": "unknown",
        "error": "unknown",
        "not_applicable": "unknown",
        "not_evaluated": "unknown",
        "required_guard": "block_unavailable",
        "scan": "all_mandatory",
        "true_result": "violation",
        "false_result": "no_violation",
        "unknown_result": "uncertain",
    }
    evidence_policy = {
        "policy_kind": "evidence",
        "required_evidence_types": [],
        "allowed_evidence_types": [],
        "on_missing": "block_result",
        "original_media_required": False,
        "retention_class": {
            "vocabulary_ref": content_ref("vocabulary", "retention"),
            "member": "fixture",
        },
        "retention_duration_seconds": 100,
        "access_scope": "task",
    }
    return {
        "profile_ref": scope["configured_profile_refs"][0],
        "rule_sets": [
            {
                "rule_set_ref": rule_set["target_ref"],
                "definition": group,
                "rules": [
                    {
                        "rule_ref": rule["target_ref"],
                        "definition": definition,
                        "rule_policy": {
                            "schema_version": VERSION,
                            "version": VERSION,
                            "policy_id": definition["rule_policy_ref"]["artifact_id"],
                            "policy": rule_policy,
                        },
                        "evidence_policy": {
                            "schema_version": VERSION,
                            "version": VERSION,
                            "policy_id": definition["evidence_policy_ref"][
                                "artifact_id"
                            ],
                            "policy": evidence_policy,
                        },
                    },
                ],
            },
        ],
    }


def invocation(node_id, purpose, object_ref=None):
    inv = deepcopy(_OBSERVATION)
    inv.pop("contract_snapshot")
    inv.pop("object_ref")
    inv.update(
        execution_id="exec-1",
        model_invocation_id=f"{node_id.lower()}-invocation",
        node_id=node_id,
        node_run_ref=runtime_ref("node_run", f"{node_id.lower()}-run"),
        attempt_ref=runtime_ref("attempt", f"{node_id.lower()}-attempt1"),
        profile_refs=[content_ref("profile", "fixture-profile")],
        purposes=[purpose],
    )
    if object_ref:
        inv["object_ref"] = object_ref
    return inv


def supplied_case(
    mode="match",
    *,
    hierarchy="H05",
    discovery="hits",
    grounding="objects",
    needs_fact=False,
):
    object_scope = discovery in ("hits", "partial") and grounding in (
        "objects",
        "partial",
    )
    scope, records = wire_case(hierarchy, object_scope=object_scope)
    context = scope["context_snapshot_ref"]
    pins = {
        "schema_version": VERSION,
        **{key: scope[key] for key in ("execution_id", "release_id", "bundle_digest")},
    }
    configured = scope["configured_profile_refs"]
    profile = configured[0]
    configured_rules = profile_rules(scope, needs_fact=needs_fact)
    rule_set = configured_rules["rule_sets"][0]
    rule_data = rule_set["rules"][0]
    path = {"rule_set_ref": rule_set["rule_set_ref"], "rule_ref": rule_data["rule_ref"]}
    decision_ref = runtime_ref("decision", "decision-a")
    traces = records["traces"]
    gate = records["profile_eligibility"]
    gate_ref = runtime_ref("profile_eligibility_evaluation", gate["evaluation_id"])
    coverage_ref = runtime_ref("execution_coverage", "coverage-1")
    node = runtime_ref("node_run", "n06-run")
    attempt = runtime_ref("attempt", "n06-attempt1")
    obj = runtime_ref("object", "object-a")
    dependency = None
    invocations = []
    extra_nodes = []
    reasons = []
    profile_reasons = []
    gate_truth = traces[0]["effective_truth"]
    if gate_truth != "true":
        discovery = grounding = "not_evaluated"
        object_scope = False
        reasons.append("no_eligible_profiles")
        if gate_truth == "unknown":
            profile_reasons.append("applicability_unresolved")
    if discovery != "hits":
        reasons.append(f"discovery_{discovery}")
    if discovery != "not_evaluated":
        invocations.append(invocation("N02", "profile_discovery"))
    if discovery in ("no_hits", "failed"):
        grounding = "not_evaluated"
        dependency = ("N02", f"discovery_{discovery}")
    elif grounding in ("no_target", "failed"):
        dependency = ("N03", f"grounding_{grounding}")
    if grounding != "objects":
        profile_reasons.append(f"grounding_{grounding}")
    if dependency:
        node_id, reason = dependency
        blocker = runtime_ref("node_run", f"{node_id.lower()}-run")
        extra_nodes.append(
            {
                "node_run_ref": blocker,
                "execution_id": scope["execution_id"],
                "node_id": node_id,
                "status": "failed" if reason.endswith("failed") else "succeeded",
            }
        )
        records["traces"] = traces[:1]
        for field in (
            "rule_set_evaluations",
            "rule_evaluations",
            "node_runs",
            "attempts",
            "objects",
        ):
            records[field] = []

    if not object_scope and gate_truth != "true":
        records["node_runs"] = records["attempts"] = records["objects"] = []

    eval_refs = [
        gate_ref,
        runtime_ref("rule_set_applicability_evaluation", "rule-set-rev1"),
        runtime_ref("rule_applicability_evaluation", "rule-rev1"),
    ]
    trace_refs = [
        runtime_ref("applicability_trace", trace["applicability_trace_id"])
        for trace in records["traces"]
    ]
    obligatory_ids = [
        "missing-region-metadata"
        if trace["effective_truth"] == "unknown"
        and trace["levels"][-1]["evaluation_status"] == "evaluated"
        else f"obligation-{i}"
        for i, trace in enumerate(traces)
    ]
    ledger = []
    for i, trace in enumerate(traces):
        level = trace["levels"][-1]
        if dependency and i:
            outcome = {
                "disposition": "dependency_not_evaluated",
                "reason": reason,
                "blocker_node_run_ref": blocker,
            }
        else:
            outcome = {
                "selected_applicability": {
                    "evaluation_ref": eval_refs[i],
                    "trace_ref": trace_refs[i],
                }
            }
            if level["evaluation_status"] == "not_evaluated":
                ancestor = eval_refs.index(level["decisive_ancestor_ref"])
                outcome.update(
                    disposition="not_evaluated",
                    decisive_ancestor_ref=eval_refs[ancestor],
                    ancestor_obligation_id=obligatory_ids[ancestor],
                )
            elif level["truth"] == "unknown":
                outcome.update(
                    disposition="unresolved",
                    unresolved_obligation_ref=runtime_ref(
                        "unresolved_obligation", obligatory_ids[i]
                    ),
                )
            elif level["truth"] == "false":
                outcome["disposition"] = "excluded"
            elif i < 2:
                outcome["disposition"] = "included"
            else:
                outcome.update(
                    disposition="blocked" if mode == "blocked" else "evaluated",
                    decision_ref=decision_ref,
                )
        target = {"scope": level["level"]}
        if i:
            target["rule_set_ref"] = path["rule_set_ref"]
            if object_scope:
                target["object_ref"] = obj
        if i == 2:
            target["rule_ref"] = path["rule_ref"]
        ledger.append(
            {
                "obligation_id": obligatory_ids[i],
                "profile_ref": profile,
                "target": target,
                "outcome": outcome,
            }
        )

    status = (
        "not_applicable"
        if gate_truth == "false"
        else "none"
        if not object_scope
        else "partial"
        if mode in ("blocked", "unknown")
        or hierarchy in ("H03", "H04")
        or grounding == "partial"
        else "complete"
    )
    result = (
        "violation"
        if object_scope and mode == "match" and hierarchy == "H05"
        else "not_applicable"
        if status == "not_applicable"
        or (status == "complete" and hierarchy.startswith("H07"))
        else "no_violation"
        if status == "complete"
        else "uncertain"
    )
    summary_status = (
        "partial" if discovery == "partial" and status == "complete" else status
    )
    summary_result = (
        "uncertain" if summary_status == "partial" and result != "violation" else result
    )
    matches = (
        [
            {
                "object_ref": obj,
                "profile_ref": profile,
                "decision_ref": decision_ref,
                "rule": path,
            }
        ]
        if result == "violation"
        else []
    )
    unresolved_refs = [
        entry["outcome"]["unresolved_obligation_ref"]
        for entry in ledger
        if entry["outcome"]["disposition"] == "unresolved"
    ]
    coverage = {
        **pins,
        "coverage_id": coverage_ref["record_id"],
        "applicability_scope": {
            "scope_status": "evaluated",
            "profile_eligibility_evaluation_ref": gate_ref,
            "context_snapshot_ref": context,
            "configured_profile_refs": configured,
        },
        "discovery_outcome": discovery,
        "hit_profile_refs": configured if discovery in ("hits", "partial") else [],
        "profiles": [
            {
                **pins,
                "profile_ref": profile,
                "grounding_outcome": grounding,
                "object_refs": [obj] if object_scope else [],
                "object_count": int(object_scope),
                "obligation_ids": obligatory_ids,
                "coverage_status": status,
                "reasons": profile_reasons,
                "missing_evidence_refs": [],
            }
        ],
        "mandatory_rule_coverage": {"scan": "all_mandatory", "obligations": ledger},
        "selected_applicability_evaluation_refs": eval_refs[: len(trace_refs)],
        "applicability_trace_refs": trace_refs,
        "unresolved_obligation_refs": unresolved_refs,
        "object_count": int(object_scope),
        "coverage_status": summary_status,
        "summary_result": summary_result,
        "reasons": reasons,
        "matches": matches,
        "missing_evidence_refs": [],
        "created_at": TIME,
    }
    coverage["discovery_model_invocation_refs"] = [
        runtime_ref("model_invocation", inv["model_invocation_id"])
        for inv in invocations
    ]
    finite = {
        "profile_rule_scope": [configured_rules],
        "applicability": {"scope": scope, "records": records},
        "objects": records["objects"],
        "node_runs": [*records["node_runs"], *extra_nodes],
        "attempts": [item["attempt"] for item in records["attempts"]],
        "decisions": [],
        "predictions": [],
        "model_invocations": invocations,
        "facts": [],
        "authoritative_selections": [],
        "evidence": [],
    }
    if not object_scope:
        return json.loads(json.dumps(coverage)), json.loads(json.dumps(finite))

    guard = {
        "outcome": "complete",
        "required_fact_refs": [],
        "required_evidence_refs": [],
        "authoritative_result_selection_refs": [],
        "fact_proofs": [],
    }
    if needs_fact and mode != "blocked":
        add_fact_proof(finite, guard, rule_data, pins, profile, obj)
    if mode == "blocked":
        guard.update(outcome="blocked", reasons=["required_fact_unavailable"])
        evaluation = {
            "outcome": "blocked",
            "guard": guard,
            "predicate": {
                "truth": "false",
                "diagnostic_codes": ["required-fact-unavailable"],
            },
        }
    else:
        evaluation = {
            "outcome": "evaluated",
            "guard": guard,
            "matched": mode == "match",
            "predicate": {
                "truth": {"match": "true", "false": "false", "unknown": "unknown"}[
                    mode
                ],
                "diagnostic_codes": ["optional-fact-unavailable"]
                if mode == "unknown"
                else [],
            },
        }
    if hierarchy == "H07-rule":
        evaluation = {"outcome": "skipped", "reason": "applicability_excluded"}
    if hierarchy in ("H03", "H04", "H07-rule-set"):
        evaluation = {"outcome": "not_evaluated"}
    candidate_invocation = invocation("N06", "candidate_prediction", obj)
    invocations.append(candidate_invocation)
    candidate = {
        **pins,
        "scope": "object",
        "profile_ref": profile,
        "object_ref": obj,
        "context_snapshot_ref": context,
        "node_run_ref": node,
        "attempt_ref": attempt,
        "prediction_id": "prediction-1",
        "status": "succeeded",
        "categories": [],
        "model_invocation_ref": runtime_ref(
            "model_invocation", candidate_invocation["model_invocation_id"]
        ),
        "advisory_only": True,
        "created_at": TIME,
    }
    outcome = evaluation["outcome"]
    fact_refs = (
        guard["required_fact_refs"] if outcome in ("evaluated", "blocked") else []
    )
    decision = {
        **pins,
        "scope": "object",
        "profile_ref": profile,
        "object_ref": obj,
        "context_snapshot_ref": context,
        "node_run_ref": node,
        "attempt_ref": attempt,
        "decision_id": decision_ref["record_id"],
        "rule_engine_version": VERSION,
        "profile_eligibility_evaluation_ref": gate_ref,
        "execution_coverage_ref": coverage_ref,
        "candidate_prediction_ref": runtime_ref(
            "candidate_prediction", candidate["prediction_id"]
        ),
        "result": result,
        "rule_evaluations": [
            {
                "rule": path,
                "result": evaluation,
                "selected_rule_set_evaluation_ref": eval_refs[1],
                "selected_rule_evaluation_ref": eval_refs[2],
                "applicability_trace_ref": trace_refs[2],
                "fact_refs": fact_refs,
                "evidence_refs": [],
            }
        ]
        if outcome != "not_evaluated"
        else [],
        "explanation": {
            "applicable_rules": [path]
            if traces[2]["effective_truth"] == "true"
            else [],
            **{
                f"{name}_rules": [path] if outcome == name else []
                for name in ("evaluated", "skipped", "blocked", "not_evaluated")
            },
            "obligation_ids": obligatory_ids,
            "applicability_trace_refs": trace_refs,
            "fact_refs": fact_refs,
            "evidence_refs": [],
        },
        "conflict_resolution": {
            "mode": "collect_all",
            "conflict_policy_refs": [rule_set["definition"]["conflict_policy_ref"]],
            "matched_rules": [path] if matches else [],
            "matched_category_ids": [rule_data["definition"]["violation_category"]]
            if matches
            else [],
        },
        "created_at": TIME,
    }
    finite["decisions"].append(decision)
    finite["predictions"].append(candidate)
    return json.loads(json.dumps(coverage)), json.loads(json.dumps(finite))


def add_fact_proof(finite, guard, rule, pins, profile, obj):
    selector = rule["definition"]["required_fact_selectors"][0]
    fact_ref = runtime_ref("fact", "fact-a")
    select_ref = runtime_ref("authoritative_result_selection", "selection-a")
    batch_ref = runtime_ref("observation_batch", "batch-a")
    common = {
        "schema_version": VERSION,
        "execution_id": pins["execution_id"],
        "scope": "object",
        "object_id": obj["record_id"],
        "profile_ref": profile,
    }
    finite["facts"].append(
        {
            **common,
            "fact_id": fact_ref["record_id"],
            "fact_schema_ref": selector["fact_schema_ref"],
            "fact_key": selector["fact_key"],
            "producer_catalogue_ref": content_ref("fact_producer_catalog", "catalogue"),
            "producer_declaration_id": "question-a",
            "producer_kind": "vlm_question",
            "producer_id": selector["producer_selector"]["question_ref"]["artifact_id"],
            "producer_version": VERSION,
            "attempt_id": "n04-attempt1",
            "source": {
                "source_kind": "vlm_question",
                "question_ref": selector["producer_selector"]["question_ref"],
                "observation_batch_ref": batch_ref,
                "model_invocation_ref": runtime_ref(
                    "model_invocation", "n04-invocation"
                ),
            },
            "evidence_refs": [],
            "status": "known",
            "value": {"kind": "bool", "value": True},
            "created_at": TIME,
            "trace_id": "trace",
        }
    )
    finite["authoritative_selections"].append(
        {
            **common,
            "selection_group": "observation_batch",
            "selection_id": select_ref["record_id"],
            "selected_attempt_id": "n04-attempt1",
            "selected_result_ref": batch_ref,
            "selected_fact_refs": [fact_ref],
            "reason": "validated_success",
            "authority_revision": 1,
            "actor_ref": {
                "actor_kind": "service",
                "actor_id": "fixture",
                "identity_registry_id": "fixture",
            },
            "created_at": TIME,
        }
    )
    guard.update(
        required_fact_refs=[fact_ref],
        authoritative_result_selection_refs=[select_ref],
        fact_proofs=[
            {
                "selector": selector,
                "fact_ref": fact_ref,
                "authority_selection_ref": select_ref,
            }
        ],
    )


def source_case(discovery="hits", *, two_groups=False, no_decision=False):
    from fixtures.wp01c.nodes.cases import cases, envelope

    coverage, finite = supplied_case(discovery=discovery)
    peers = cases()
    scope = finite["applicability"]["scope"]
    app_records = finite["applicability"]["records"]
    gate = app_records["profile_eligibility"]
    subject = gate["eligible_profiles"][0]
    base_inv = finite["model_invocations"][0]
    input_record = peers["discovery_input"]
    input_record.update(
        context_snapshot_ref=scope["context_snapshot_ref"],
        context_snapshot_digest=scope["context_snapshot_digest"],
        profile_eligibility_evaluation_ref=coverage["applicability_scope"][
            "profile_eligibility_evaluation_ref"
        ],
        eligible_profiles=[subject],
    )
    input_record["envelope"].update(
        execution_id=scope["execution_id"],
        release_id=scope["release_id"],
        bundle_digest=scope["bundle_digest"],
        node_run_id="n02-run",
        attempt_id="n02-attempt1",
    )
    input_record["bindings"][0].update(
        profile_ref=subject,
        discovery_binding_ref=base_inv["configured_provider"]["binding_ref"],
    )
    output_record = peers["discovery_output"]
    output_record.update(
        execution_id=scope["execution_id"],
        outcome=discovery,
        model_invocation_refs=coverage["discovery_model_invocation_refs"],
    )
    output_record["envelope"].update(node_run_id="n02-run", attempt_id="n02-attempt1")
    hit_template = deepcopy(output_record["profile_hits"][0])
    output_record["profile_hits"] = []
    selection_template = peers["selection"]
    selections = []
    groundings = []
    objects = []
    node_inputs = {}

    def add_hit(profile, invocation_ref, selection_id):
        choice = deepcopy(selection_template)
        choice.update(
            execution_id=scope["execution_id"],
            node_run_id="n02-run",
            attempt_id="n02-attempt1",
            profile_ref=profile,
            selection_id=selection_id,
            discovery_model_invocation_ref=invocation_ref,
            context_snapshot_ref=scope["context_snapshot_ref"],
            context_snapshot_digest=scope["context_snapshot_digest"],
        )
        hits = deepcopy(hit_template)
        hits.update(
            profile_ref=profile,
            model_invocation_ref=invocation_ref,
            question_selection_ref=runtime_ref(
                "question_selection_record", selection_id
            ),
        )
        selections.append(choice)
        output_record["profile_hits"].append(hits)
        return hits

    if discovery != "no_hits":
        main = add_hit(subject, output_record["model_invocation_refs"][0], "chosen-a")
        grounding = peers["grounding_record"]
        obj = peers["object_record"]
        for item in (grounding, obj):
            item.update(
                execution_id=scope["execution_id"],
                profile_ref=subject,
                question_selection_ref=main["question_selection_ref"],
            )
        obj["object_id"] = coverage["profiles"][0]["object_refs"][0]["record_id"]
        grounding.update(
            node_run_id="n03-run",
            attempt_id="n03-attempt1",
            object_refs=coverage["profiles"][0]["object_refs"],
        )
        groundings.append(grounding)
        objects.append(obj)
        node_inputs["N06"] = [
            {"ref": coverage["profiles"][0]["object_refs"][0], "digest": "b" * 64}
        ]
    if two_groups:
        other = content_ref("profile", "fixture-profile-b")
        trace_ref = runtime_ref("applicability_trace", "profile-trace-b")
        for items in (
            scope["configured_profile_refs"],
            gate["configured_profile_refs"],
            gate["eligible_profiles"],
            input_record["eligible_profiles"],
            coverage["applicability_scope"]["configured_profile_refs"],
        ):
            if other not in items:
                items.append(other)
        binding = deepcopy(scope["target_bindings"][0])
        binding.update(target_ref=other, child_refs=[])
        scope["target_bindings"].append(binding)
        trace = deepcopy(app_records["traces"][0])
        trace.update(
            target_ref=other,
            profile_ref=other,
            applicability_trace_id=trace_ref["record_id"],
        )
        trace["levels"][0]["target_ref"] = other
        app_records["traces"].append(trace)
        gate["applicability_traces"].append(trace_ref)
        coverage["applicability_trace_refs"].append(trace_ref)
        coverage["profiles"].append(
            {
                **coverage["profiles"][0],
                "profile_ref": other,
                "grounding_outcome": "not_evaluated",
                "object_refs": [],
                "object_count": 0,
                "coverage_status": "none",
                "obligation_ids": ["obligation-profile-b"],
                "reasons": ["grounding_not_evaluated"],
            }
        )
        finite["profile_rule_scope"].append({"profile_ref": other, "rule_sets": []})
        coverage["mandatory_rule_coverage"]["obligations"].append(
            {
                "obligation_id": "obligation-profile-b",
                "profile_ref": other,
                "target": {"scope": "profile"},
                "outcome": {
                    "disposition": "included",
                    "selected_applicability": {
                        "evaluation_ref": coverage["applicability_scope"][
                            "profile_eligibility_evaluation_ref"
                        ],
                        "trace_ref": trace_ref,
                    },
                },
            }
        )
        inv = deepcopy(base_inv)
        inv.update(model_invocation_id="n02-invocation-b", profile_refs=[other])
        inv["configured_provider"]["binding_ref"] = content_ref(
            "model_binding", "second-binding"
        )
        if discovery == "partial":
            inv["outcome"] = {
                "status": "failed",
                "error": {
                    "error_class": "internal",
                    "code": "unavailable",
                    "diagnostic_refs": [],
                },
            }
        invocation_ref = runtime_ref("model_invocation", inv["model_invocation_id"])
        coverage["discovery_model_invocation_refs"].append(invocation_ref)
        if invocation_ref not in output_record["model_invocation_refs"]:
            output_record["model_invocation_refs"].append(invocation_ref)
        finite["model_invocations"].append(inv)
        input_record["bindings"].append(
            {
                **input_record["bindings"][0],
                "profile_ref": other,
                "discovery_binding_ref": inv["configured_provider"]["binding_ref"],
            }
        )
        if discovery == "hits":
            second = add_hit(other, invocation_ref, "chosen-b")
            coverage["hit_profile_refs"].append(other)
            coverage["coverage_status"] = "partial"
            # The second Profile has a real hit but its grounding was not reached.
            assert second["profile_ref"] == other

    blockers = []
    if no_decision:
        coverage.update(
            summary_result="uncertain", matches=[], coverage_status="partial"
        )
        coverage["profiles"][0]["coverage_status"] = "partial"
        coverage["selected_applicability_evaluation_refs"] = coverage[
            "selected_applicability_evaluation_refs"
        ][:1]
        coverage["applicability_trace_refs"] = coverage["applicability_trace_refs"][:1]
        for entry in coverage["mandatory_rule_coverage"]["obligations"][1:]:
            entry["outcome"] = {
                "disposition": "dependency_not_evaluated",
                "reason": "decision_not_evaluated",
                "blocker_node_run_ref": runtime_ref("node_run", "n06-run"),
            }
        finite["decisions"] = finite["predictions"] = []
        finite["node_runs"][0]["status"] = "blocked"
        finite["attempts"] = []
        finite["model_invocations"] = [
            inv for inv in finite["model_invocations"] if inv["node_id"] == "N02"
        ]
        app_records["node_runs"] = app_records["attempts"] = []
        app_records["traces"] = app_records["traces"][:1]
        app_records["rule_set_evaluations"] = app_records["rule_evaluations"] = []
        blockers.append(
            {
                "object_ref": coverage["profiles"][0]["object_refs"][0],
                "node_run_ref": runtime_ref("node_run", "n06-run"),
            }
        )

    source_nodes = {"N01": ("succeeded", [scope["context_snapshot_ref"]])}
    source_nodes["N02"] = ("succeeded", coverage["discovery_model_invocation_refs"])
    if groundings:
        source_nodes["N03"] = (
            "succeeded",
            [runtime_ref("grounding_record", groundings[0]["grounding_record_id"])],
        )
        source_nodes["N06"] = (
            ("blocked", [])
            if no_decision
            else (
                "succeeded",
                [runtime_ref("decision", finite["decisions"][0]["decision_id"])],
            )
        )
    nodes, attempts = [], []
    for node_id, (status, refs) in source_nodes.items():
        name = f"{node_id.lower()}-run"
        attempt_ref = runtime_ref("attempt", f"{node_id.lower()}-attempt1")
        ran = status == "succeeded"
        nodes.append(
            {
                "schema_version": VERSION,
                "node_run_id": name,
                "execution_id": scope["execution_id"],
                "node_id": node_id,
                "trace": envelope(node_id)["trace"],
                "config_ref": content_ref("policy", "workflow"),
                "upstream_inputs": node_inputs.get(node_id, []),
                "input_digest": "a" * 64,
                "status": status,
                "created_at": TIME,
                "started_at": TIME if ran else None,
                "ended_at": TIME,
                "reason": None if ran else {"code": "dependency"},
                "attempt_history": [{"attempt_ref": attempt_ref, "attempt_number": 1}]
                if ran
                else [],
                "output_refs": refs,
            }
        )
        if ran:
            attempts.append(
                {
                    "schema_version": VERSION,
                    "attempt_id": attempt_ref["record_id"],
                    "execution_id": scope["execution_id"],
                    "owner": {
                        "owner_kind": "node",
                        "node_run_ref": runtime_ref("node_run", name),
                    },
                    "attempt_number": 1,
                    "status": "succeeded",
                    "created_at": TIME,
                    "started_at": TIME,
                    "ended_at": TIME,
                }
            )
    context_output = deepcopy(peers["context_output"])
    context_output.update(
        context_snapshot_ref=scope["context_snapshot_ref"],
        context_snapshot_digest=scope["context_snapshot_digest"],
    )
    context_output["envelope"].update(
        node_run_id="n01-run",
        attempt_id="n01-attempt1",
        output_artifacts=[
            {
                "ref": scope["context_snapshot_ref"],
                "digest": scope["context_snapshot_digest"],
            }
        ],
    )
    snapshot = deepcopy(peers["snapshot"])
    snapshot.update(
        context_snapshot_id=scope["context_snapshot_ref"]["record_id"],
        execution_id=scope["execution_id"],
        release_id=scope["release_id"],
        bundle_digest=scope["bundle_digest"],
    )
    execution_fixture = json.loads(
        (_FIXTURES / "executions/contracts.json").read_text()
    )["execution"]
    execution_fixture.update(
        execution_id=scope["execution_id"],
        task_id=snapshot["task_id"],
        status="running",
        started_at=execution_fixture["created_at"],
        context_snapshot={
            "snapshot_ref": scope["context_snapshot_ref"],
            "snapshot_digest": scope["context_snapshot_digest"],
        },
    )
    execution_fixture["pins"].update(
        release_id=scope["release_id"],
        bundle_digest=scope["bundle_digest"],
    )
    return json.loads(
        json.dumps(
            [
                coverage,
                finite,
                {
                    "execution": execution_fixture,
                    "context_output": context_output,
                    "context_snapshot": snapshot,
                    "discovery_input": input_record,
                    "discovery_output": output_record,
                    "question_selections": selections,
                    "grounding_records": groundings,
                    "objects": objects,
                    "node_runs": nodes,
                    "attempts": attempts,
                    "decision_blockers": blockers,
                },
            ]
        )
    )


def two_rule_case(*, second_outcome="match"):
    coverage, records = supplied_case()
    first = records["profile_rule_scope"][0]["rule_sets"][0]
    decision = records["decisions"][0]
    selected = records["applicability"]
    original_binding = selected["scope"]["target_bindings"][-1]
    target = deepcopy(original_binding["target_ref"])
    target["artifact_id"] = "fixture-rule-second"
    bind = deepcopy(original_binding)
    bind["target_ref"] = target
    selected["scope"]["target_bindings"].append(bind)
    selected["scope"]["target_bindings"][1]["child_refs"].append(target)
    new_rule = deepcopy(first["rules"][0])
    new_rule["rule_ref"] = target
    new_rule["definition"]["rule_id"] = target["artifact_id"]
    first["rules"].append(new_rule)
    first["definition"]["rule_refs"].append(target)

    current = selected["records"]
    trace = deepcopy(current["traces"][-1])
    evaluation = deepcopy(current["rule_evaluations"][0])
    trace_ref = runtime_ref("applicability_trace", "rule-trace-second")
    eval_ref = runtime_ref("rule_applicability_evaluation", "rule-rev-second")
    trace.update(applicability_trace_id=trace_ref["record_id"], target_ref=target)
    trace["levels"][-1].update(target_ref=target, source_evaluation_ref=eval_ref)
    evaluation.update(
        evaluation_id=eval_ref["record_id"],
        rule_ref=target,
        applicability_trace_ref=trace_ref,
    )
    current["traces"].append(trace)
    current["rule_evaluations"].append(evaluation)

    ledger = coverage["mandatory_rule_coverage"]["obligations"]
    obligation = deepcopy(ledger[-1])
    obligation.update(obligation_id="extra-rule-obligation")
    obligation["target"]["rule_ref"] = target
    obligation["outcome"]["selected_applicability"] = {
        "evaluation_ref": eval_ref,
        "trace_ref": trace_ref,
    }
    ledger.append(obligation)
    coverage["profiles"][0]["obligation_ids"].append(obligation["obligation_id"])
    coverage["applicability_trace_refs"].append(trace_ref)
    coverage["selected_applicability_evaluation_refs"].append(eval_ref)
    explanation = decision["explanation"]
    explanation["obligation_ids"].append(obligation["obligation_id"])
    explanation["applicability_trace_refs"].append(trace_ref)
    item = deepcopy(decision["rule_evaluations"][0])
    item["rule"]["rule_ref"] = target
    item.update(
        selected_rule_evaluation_ref=eval_ref, applicability_trace_ref=trace_ref
    )
    decision["rule_evaluations"].append(item)
    explanation["applicable_rules"].append(item["rule"])
    if second_outcome == "match":
        explanation["evaluated_rules"].append(item["rule"])
        decision["conflict_resolution"]["matched_rules"].append(item["rule"])
        coverage["matches"].append({**coverage["matches"][0], "rule": item["rule"]})
    else:
        coverage["coverage_status"] = coverage["profiles"][0]["coverage_status"] = (
            "partial"
        )
        new_definition = new_rule["definition"]
        required = deepcopy(_RULE["required_fact_selectors"][:1])
        new_definition.update(
            required_fact_selectors=required,
            condition_ast={"kind": "fact_ref", "selector": required[0]},
        )
        obligation["outcome"]["disposition"] = "blocked"
        explanation["blocked_rules"].append(item["rule"])
        item["result"] = {
            "outcome": "blocked",
            "guard": {
                **item["result"]["guard"],
                "outcome": "blocked",
                "reasons": ["required_fact_unavailable"],
            },
        }
    return json.loads(json.dumps(coverage)), json.loads(json.dumps(records))


def optional_fact_case():
    coverage, records = supplied_case(needs_fact=True)
    rule = records["profile_rule_scope"][0]["rule_sets"][0]["rules"][0]["definition"]
    rule["optional_fact_selectors"] = rule["required_fact_selectors"]
    rule["required_fact_selectors"] = []
    evaluation = records["decisions"][0]["rule_evaluations"][0]
    guard = evaluation["result"]["guard"]
    evaluation["optional_fact_proofs"] = guard["fact_proofs"]
    for field in (
        "fact_proofs",
        "required_fact_refs",
        "authoritative_result_selection_refs",
    ):
        guard[field] = []
    return coverage, records
