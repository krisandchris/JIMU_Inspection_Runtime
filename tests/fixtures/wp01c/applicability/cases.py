"""Precomputed synthetic applicability records with fixture identity and no evaluation."""

import json
from copy import deepcopy
from pathlib import Path

PATHS = json.loads(Path(__file__).with_name("paths.json").read_text())
VERSION = "1.0.0"
TIME = "2026-11-18T01:00:00Z"
LEVELS = ("profile", "rule_set", "rule")


def runtime_ref(kind, name):
    return {"record_kind": kind, "record_id": name}


def content_ref(kind, name, digest="a"):
    return {
        "artifact_kind": kind,
        "artifact_id": name,
        "version": VERSION,
        "digest": digest * 64,
    }


def metadata_ref(field="region_type"):
    return {
        "source": "region_metadata",
        "schema_ref": {
            **content_ref("context_schema", "region"),
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        },
        "field_path": [field],
    }


def wire_case(name="H05", *, object_scope=False, sibling=False):
    case = next(item for item in PATHS if item["id"] == name)
    truths = case["truths"]
    targets = [content_ref(level, f"fixture-{level}") for level in LEVELS]
    policies = [content_ref("policy", f"{level}-policy", "b") for level in LEVELS]
    eligibility_ref = runtime_ref("profile_eligibility_evaluation", "profile-gate")
    eval_refs = [
        eligibility_ref,
        runtime_ref("rule_set_applicability_evaluation", "rule-set-rev1"),
        runtime_ref("rule_applicability_evaluation", "rule-rev1"),
    ]
    trace_refs = [
        runtime_ref("applicability_trace", f"{level}-trace") for level in LEVELS
    ]
    attempt_ref = runtime_ref("attempt", "n06-attempt1")
    n06_ref = runtime_ref("node_run", "n06-run")
    object_ref = runtime_ref("object", "object-a")
    pins = {
        "execution_id": "exec-1",
        "bundle_digest": "c" * 64,
        "release_id": "release-1",
        "context_snapshot_ref": runtime_ref("context_snapshot", "snapshot-1"),
    }
    snapshot = "d" * 64
    selectors = [metadata_ref(f"{level}_region_type") for level in LEVELS]
    levels, traces, wrappers, obligations = [], [], [], []
    decisive = 0
    for i, (level, truth) in enumerate(zip(LEVELS, truths, strict=True)):
        prefix = {"level": level, "policy_ref": policies[i], "target_ref": targets[i]}
        if truth is not None:
            decisive = i
            prefix.update(
                evaluation_status="evaluated",
                evaluation_owner="pre_n02_gate" if i == 0 else "n06",
                source_evaluation_ref=eval_refs[i],
                truth=truth,
                declared_input_refs=[selectors[i]],
                reason_code={
                    "true": "included_by_policy",
                    "false": "excluded_by_policy",
                    "unknown": "metadata_unknown",
                }[truth],
                reason_evidence_refs=[policies[i], selectors[i]]
                if truth == "false"
                else [],
                validation_issues=[
                    {
                        "code": "metadata.missing",
                        "path": selectors[i]["field_path"],
                        "message": "The declared metadata field was unavailable.",
                        "related_refs": [policies[i]],
                    }
                ]
                if truth == "unknown"
                else [],
            )
        else:
            prefix.update(
                evaluation_status="not_evaluated",
                reason_code=(
                    "ancestor_unresolved"
                    if truths[decisive] == "unknown"
                    else "ancestor_excluded"
                ),
                decisive_ancestor_ref=eval_refs[decisive],
            )
        levels.append(prefix)
        effective = truths[decisive]
        traces.append(
            {
                **pins,
                "schema_version": VERSION,
                "applicability_trace_id": trace_refs[i]["record_id"],
                "target_ref": targets[i],
                "profile_ref": targets[0],
                "context_snapshot_digest": snapshot,
                "levels": levels[:],
                "effective_truth": effective,
                "disposition": {
                    "true": "included",
                    "false": "excluded",
                    "unknown": "unresolved",
                }[effective],
                "decisive_level": LEVELS[decisive],
                "created_at": TIME,
            }
        )
        if i:
            wrapper = {
                **pins,
                "schema_version": VERSION,
                "evaluation_id": eval_refs[i]["record_id"],
                "profile_eligibility_evaluation_ref": eligibility_ref,
                "profile_ref": targets[0],
                "rule_set_ref": targets[1],
                "applicability_trace_ref": trace_refs[i],
                "recorded_at": TIME,
            }
            if i == 2:
                wrapper.update(
                    rule_set_applicability_evaluation_ref=eval_refs[1],
                    rule_ref=targets[2],
                )
            if truth is None:
                wrapper["decisive_ancestor_ref"] = eval_refs[decisive]
            else:
                wrapper.update(node_run_ref=n06_ref, attempt_ref=attempt_ref)
                if object_scope:
                    wrapper["object_ref"] = object_ref
            wrappers.append(wrapper)

        if effective == "unknown":
            obligation = {
                "schema_version": VERSION,
                "obligation_id": "missing-region-metadata",
                "execution_id": pins["execution_id"],
                "profile_ref": targets[0],
                "scope": level,
                "target_ref": targets[i],
                "applicability_trace_ref": trace_refs[i],
                "reason_code": prefix["reason_code"],
                "diagnostic_refs": [
                    {
                        "trace_ref": trace_refs[decisive],
                        "level": LEVELS[decisive],
                        "issue_index": 0,
                    }
                ],
            }
            if i == 2:
                obligation["rule_set_ref"] = targets[1]
            if truth is None:
                obligation["decisive_ancestor_ref"] = eval_refs[decisive]
            elif i and object_scope:
                obligation["object_ref"] = object_ref
            obligations.append(obligation)

    scope = {
        **pins,
        "context_snapshot_digest": snapshot,
        "workflow_version": VERSION,
        "context_node_run": {
            "node_run_ref": runtime_ref("node_run", "n01-run"),
            "execution_id": pins["execution_id"],
            "node_id": "N01",
            "status": "succeeded",
        },
        "configured_profile_refs": [targets[0]],
        "target_bindings": [
            {
                "target_ref": target,
                "policy_ref": policy,
                "policy": {
                    "schema_version": VERSION,
                    "policy_id": policy["artifact_id"],
                    "version": VERSION,
                    "policy": {
                        "policy_kind": "applicability",
                        "input_domain": "rule_applicability",
                        "context_allowlist": [selectors[i]],
                        "identity_constraints": [],
                        "condition_ast": {
                            "kind": "comparison",
                            "operator": "eq",
                            "left": {"kind": "context_ref", "selector": selectors[i]},
                            "right": {
                                "kind": "literal",
                                "constant": {
                                    "type_definition": {"kind": "string"},
                                    "value": {
                                        "kind": "string",
                                        "value": (
                                            "different"
                                            if truths[i] == "false"
                                            else "street"
                                        ),
                                    },
                                },
                            },
                        },
                        "on_unknown": "block",
                    },
                },
                "child_refs": targets[i + 1 : i + 2],
            }
            for i, (target, policy) in enumerate(zip(targets, policies, strict=True))
        ],
    }
    records = {
        "profile_eligibility": {
            **pins,
            "schema_version": VERSION,
            "evaluation_id": eligibility_ref["record_id"],
            "workflow_version": VERSION,
            "evaluated_at": TIME,
            "configured_profile_refs": [targets[0]],
            "eligible_profiles": [targets[0]] if truths[0] == "true" else [],
            "excluded_profiles": [targets[0]] if truths[0] == "false" else [],
            "unresolved_profiles": [targets[0]] if truths[0] == "unknown" else [],
            "applicability_traces": trace_refs[:1],
        },
        "traces": traces,
        "rule_set_evaluations": wrappers[:1],
        "rule_evaluations": wrappers[1:],
        "unresolved_obligations": obligations,
        "node_runs": [],
        "attempts": [],
        "objects": [],
    }
    if truths[1] is not None:
        records["node_runs"].append(
            {
                "node_run_ref": n06_ref,
                "execution_id": pins["execution_id"],
                "node_id": "N06",
                "status": "succeeded",
            }
        )
        entry = {
            "attempt": {
                "attempt_ref": attempt_ref,
                "execution_id": pins["execution_id"],
                "owner": {"owner_kind": "node", "node_run_ref": n06_ref},
                "attempt_number": 1,
                "status": "succeeded",
            }
        }
        if object_scope:
            entry["object_ref"] = object_ref
            records["objects"].append(
                {
                    "object_ref": object_ref,
                    "execution_id": pins["execution_id"],
                    "profile_ref": targets[0],
                }
            )
        records["attempts"].append(entry)
    if sibling:
        _add_sibling(scope, records)
    return json.loads(json.dumps(scope)), json.loads(json.dumps(records))


def _add_sibling(scope, records):
    other_scope, other_records = wire_case()
    for index in (1, 2):
        binding = other_scope["target_bindings"][index]
        binding["target_ref"]["artifact_id"] += "-sibling"
        binding["policy_ref"]["artifact_id"] += "-sibling"
        binding["policy"]["policy_id"] += "-sibling"
        for child in binding["child_refs"]:
            child["artifact_id"] += "-sibling"
        scope["target_bindings"].append(binding)
        trace = other_records["traces"][index]
        trace["applicability_trace_id"] += "-sibling"
        trace["target_ref"]["artifact_id"] += "-sibling"
        for level in trace["levels"][1:]:
            level["target_ref"]["artifact_id"] += "-sibling"
            level["policy_ref"]["artifact_id"] += "-sibling"
            level["source_evaluation_ref"]["record_id"] += "-sibling"
        records["traces"].append(trace)
    scope["target_bindings"][0]["child_refs"].append(
        deepcopy(scope["target_bindings"][-2]["target_ref"])
    )
    rule_set, rule = (
        other_records["rule_set_evaluations"][0],
        other_records["rule_evaluations"][0],
    )
    for record in (rule_set, rule):
        record["evaluation_id"] += "-sibling"
        record["rule_set_ref"]["artifact_id"] += "-sibling"
        record["applicability_trace_ref"]["record_id"] += "-sibling"
    rule["rule_ref"]["artifact_id"] += "-sibling"
    rule["rule_set_applicability_evaluation_ref"]["record_id"] += "-sibling"
    records["rule_set_evaluations"].append(rule_set)
    records["rule_evaluations"].append(rule)
