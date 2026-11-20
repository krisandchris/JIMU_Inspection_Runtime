"""Verify persisted, API, and provider evidence from an isolated live carton case."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from examples.live_inspection.scenes.trusted_scope import (
    CAMERA,
    DEVICE,
    SCOPE_EVIDENCE_SHA256,
    SCOPE_IMAGE_SHA256,
    SIDEWALK_REGION,
    SIDEWALK_SCOPE_FIELD,
    scope_pins,
)

_IMAGE = (
    Path(__file__).resolve().parents[2]
    / "LIVE_THREE_TASKS_20261119"
    / "images"
    / "goods.png"
)
_PROFILE = "sidewalk_goods"
_QUESTION = "sidewalk_encroachment"
_RULE = "sidewalk_goods_encroachment"


def read_json(path: Path):
    return json.loads(path.read_bytes())


def verify(directory: Path) -> dict:
    result = read_json(directory / "result.json")
    api = read_json(directory / "api.json")
    intake = read_json(directory / "intake.json")
    task = intake["task"]
    ctx = task["context"]
    image = _IMAGE.read_bytes()
    image_sha = hashlib.sha256(image).hexdigest()
    assert ctx["device_id"] == DEVICE
    assert ctx["camera_id"] == CAMERA
    assert ctx["region_id"] == SIDEWALK_REGION
    trusted_image, trusted_evidence = scope_pins(SIDEWALK_REGION, SIDEWALK_SCOPE_FIELD)
    assert trusted_image == image_sha
    with ZipFile(directory / "signed-bundle.zip") as archive:
        assert archive.testzip() is None
        properties = json.loads(
            archive.read("schemas/context/sidewalk-goods.context.schema.json")
        )["properties"]
    expected_values = {}
    for field, digest in (
        (SCOPE_IMAGE_SHA256, trusted_image),
        (SCOPE_EVIDENCE_SHA256, trusted_evidence),
    ):
        schema = properties[field]
        if "enum" in schema:
            assert schema["enum"] == [digest]
            kind = "enum"
        else:
            kind = "string"
        expected_values[field] = {"kind": kind, "value": digest}
    has_policy = SIDEWALK_SCOPE_FIELD in ctx["metadata"]["value"]
    if has_policy:
        expected_values[SIDEWALK_SCOPE_FIELD] = {"kind": "bool", "value": True}
    assert ctx["metadata"] == {"kind": "record", "value": expected_values}
    assert result["media"] == task["media_ref"]
    assert result["media"]["sha256"] == image_sha
    assert result["release_id"] == task["release_id"]
    assert result["bundle_digest"] == task["bundle_digest"]
    assert result["task_id"] == task["task_id"]
    assert result["execution_id"] == intake["execution"]["execution_id"]

    post, execution, response, *traces = api
    assert [post["status"], execution["status"], response["status"]] == [202, 200, 200]
    assert post["body"]["task"] == task
    assert execution["body"] == result["execution"]
    assert response["body"] == result["final"]
    assert traces and all(trace["status"] == 200 for trace in traces)
    events = [event["number"] for trace in traces for event in trace["body"]["events"]]
    assert events == list(range(1, traces[-1]["body"]["through_event_number"] + 1))

    grouped = {}
    for output in result["node_outputs"]:
        grouped.setdefault(output["kind"], []).append(output)
    (context,) = grouped["context"]
    assert context["snapshot"]["media_ref"] == result["media"]
    assert context["snapshot"]["device_ref"]["record_id"] == DEVICE
    assert context["snapshot"]["camera_ref"]["record_id"] == CAMERA
    assert context["snapshot"]["region_ref"]["artifact_id"] == SIDEWALK_REGION
    snapshot_values = {
        row["field_path"][0]: row for row in context["snapshot"]["values"]
    }
    for key, value in expected_values.items():
        source = snapshot_values[key]
        assert source["value"] == value
        assert source["provenance"]["source"] == "trusted_task_metadata"
    assert (SIDEWALK_SCOPE_FIELD in snapshot_values) == has_policy
    (discovery,) = grouped["discovery"]
    (grounding,) = grouped["grounding"]
    (hit,) = discovery["output"]["profile_hits"]
    assert discovery["output"]["outcome"] == "hits"
    assert hit["profile_ref"]["artifact_id"] == _PROFILE
    assert [question["artifact_id"] for question in hit["effective_question_refs"]] == [
        _QUESTION
    ]

    audits = {
        path.stem: read_json(path)
        for path in sorted((directory / "provider").glob("live-*.json"))
    }
    matched_calls = set()

    def wire(invocation, capability: str) -> dict:
        request_id = invocation["provider_request_id"]
        record = audits[request_id]
        assert request_id not in matched_calls
        matched_calls.add(request_id)
        assert invocation["request_digest"] == record["runtime_request"]["sha256"]
        assert invocation["outcome"]["status"] == "succeeded"
        assert invocation["actual_provider"]["model_id"] == "jimu/live-adapter"
        assert (
            record["bridge_identity"]["wire_model_version"]
            == invocation["actual_provider"]["model_version"]
        )
        assert record["capability"] == f"{capability}-result"
        assert record["runtime_http"] == record["upstream"]["status"] == 200
        return record

    n02 = wire(discovery["invocations"][0], "profile_discovery")
    n03 = wire(grounding["invocations"][0], "object_grounding")
    assert n02["bridge_request_id"] != n03["bridge_request_id"]
    assert n02["projected_result"] == {"profile_hits": [_PROFILE]}
    assert n02["discovery_calls"][0]["provider"] == "wevis_detection"
    for record in (n02, n03):
        assert (
            record["image"]["sha256"]
            == record["upstream"]["request"]["sha256"]
            == image_sha
        )
        assert record["wevis"]["model"] == "WeVisOne-VL-4B-NF4"
        assert record["wevis"]["forward_calls"] > 0
    assert (
        n02["discovery_calls"][0]["wevis_query"]
        == n03["wevis_query"]
        == "cardboard boxes"
    )
    objects = grounding["objects"]
    assert objects
    assert all(obj["source_media_ref"] == result["media"] for obj in objects)
    assert all(
        obj["effective_question_refs"] == hit["effective_question_refs"]
        for obj in objects
    )
    assert sorted(obj["bbox"]["xyxy"] for obj in objects) == sorted(
        box["xyxy"] for box in n03["projected_result"]["boxes"]
    )
    objects_by_id = {obj["object_id"]: obj for obj in objects}
    object_ids = set(objects_by_id)

    observations = {}
    for output in grouped["observation"]:
        batch = output["batch"]
        object_id = batch["object_ref"]["record_id"]
        assert object_id not in observations
        (question,) = batch["question_results"]
        fact = question["primary_fact"]
        assert fact["object_id"] == object_id
        assert fact["fact_key"] == _QUESTION
        call = wire(output["invocation"], "object_observation")
        assert call["qwen"]["actual_model"] == "Qwen3_5-9B"
        obj = output["inputs"]["object_record"]
        assert output["inputs"]["image_view"]["roi"] == obj["bbox"]
        assert obj["marked_frame"]["media_ref"]["sha256"] == call["image"]["sha256"]
        original = output.get("original_image_evidence")
        if original is not None:
            assert original["evidence_type"] == "original_image"
            assert original["sha256"] == image_sha
            assert original["spatial"]["original_media_ref"] == task["media_ref"]
            assert original["scope"] == {
                "scope": "execution",
                "execution_id": result["execution_id"],
            }
            assert original["access_scope"]["task_id"] == task["task_id"]
            assert original["evidence_id"] in {
                ref["record_id"] for ref in fact["evidence_refs"]
            }
        answer = json.loads(call["qwen"]["unaltered_completion_content"])[_QUESTION]
        if answer == "insufficient_visibility":
            assert fact["status"] == "unknown"
        else:
            assert fact["status"] == "known" and fact["value"]["value"] == answer
        observations[object_id] = {
            "answer": answer,
            "source_bbox_xyxy": obj["bbox"]["xyxy"],
            "fact_id": fact["fact_id"],
            "status": fact["status"],
            "original_evidence_id": None
            if original is None
            else original["evidence_id"],
            "upstream_completion_id": call["qwen"]["upstream_completion_id"],
        }
    failed_observations = {}
    for node in result["nodes"]:
        if node["node_id"] != "N04" or node["status"] != "failed":
            continue
        (obj_id,) = (
            pin["ref"]["record_id"]
            for pin in node["upstream_inputs"]
            if pin["ref"]["record_kind"] == "object"
        )
        assert obj_id not in observations
        obj = objects_by_id[obj_id]
        marked_sha = obj["marked_frame"]["media_ref"]["sha256"]
        failures = {
            request_id: record["failure"]
            for request_id, record in audits.items()
            if (
                record["capability"] == "object_observation-result"
                and record["image"]["sha256"] == marked_sha
                and record["runtime_http"] >= 400
            )
        }
        assert failures
        matched_calls.update(failures)
        failed_observations[obj_id] = {
            "source_bbox_xyxy": obj["bbox"]["xyxy"],
            "reason": node["reason"]["code"],
            "attempts": len(node["attempt_history"]),
            "provider_failures": failures,
        }
    assert set(observations) | set(failed_observations) == object_ids

    tool_ids = set()
    for output in grouped["tool"]:
        summary = output["output"]["summary"]
        assert summary["status"] == "succeeded"
        assert summary["steps"] == output["executions"] == output["facts"] == []
        refs = output["output"]["envelope"]["output_artifacts"]
        assert [artifact["ref"]["record_kind"] for artifact in refs] == [
            "tool_plan_run_summary"
        ]
        tool_ids.add(summary["object_ref"]["record_id"])
    assert len(grouped["tool"]) == len(tool_ids) and tool_ids == object_ids

    final = result["final"]
    coverage = final["coverage"]
    final_decisions = {
        item["object_ref"]["record_id"]: item for item in final["decisions"]
    }
    assert len(final_decisions) == len(final["decisions"])
    assert set(final_decisions) == object_ids and coverage["object_count"] == len(
        objects
    )
    for output in grouped["decision"]:
        candidate_wire = wire(output["invocation"], "candidate_prediction")
        assert candidate_wire["qwen"]["actual_model"] == "Qwen3_5-9B"
        assert output["draft"]["candidate"]["advisory_only"] is True
        (rule_set_trace,) = (
            trace
            for trace in output["traces"]
            if trace["target_ref"]["artifact_kind"] == "rule_set"
        )
        assert rule_set_trace["effective_truth"] == (
            "true" if has_policy else "unknown"
        )
    assert matched_calls == set(audits)
    assert {
        output["draft"]["decision"]["object_ref"]["record_id"]
        for output in grouped["decision"]
    } == object_ids
    decision_details = {}
    for object_id, decision in final_decisions.items():
        if not has_policy:
            assert decision["rule_evaluations"] == []
            (rule,) = decision["explanation"]["not_evaluated_rules"]
            assert rule["rule_ref"]["artifact_id"] == _RULE
            decision_details[object_id] = {
                "decision_id": decision["decision_id"],
                "result": decision["result"],
                "rule_outcome": "not_evaluated",
                "guard_reasons": [],
            }
            continue
        (evaluation,) = decision["rule_evaluations"]
        assert evaluation["rule"]["rule_ref"]["artifact_id"] == _RULE
        decision_details[object_id] = {
            "decision_id": decision["decision_id"],
            "result": decision["result"],
            "rule_outcome": evaluation["result"]["outcome"],
            "guard_reasons": evaluation["result"].get("guard", {}).get("reasons", []),
        }
        if object_id in failed_observations:
            assert decision["result"] == "uncertain"
            assert (
                "required_fact_unavailable"
                in decision_details[object_id]["guard_reasons"]
            )
        if decision["result"] == "violation":
            assert has_policy
            assert observations[object_id]["answer"] == "occupies_sidewalk"
            assert evaluation["result"]["outcome"] == "evaluated"
            assert evaluation["result"]["predicate"]["truth"] == "true"
            assert observations[object_id]["original_evidence_id"] in {
                ref["record_id"]
                for ref in evaluation["result"]["guard"]["required_evidence_refs"]
            }
    counts = Counter(item["result"] for item in final_decisions.values())
    if counts["violation"]:
        assert coverage["summary_result"] == "violation"
    if not has_policy:
        assert coverage["summary_result"] == "uncertain"
        assert dict(counts) == {"uncertain": len(object_ids)}

    return {
        "execution_id": result["execution_id"],
        "task_id": result["task_id"],
        "bundle_digest": result["bundle_digest"],
        "release_id": result["release_id"],
        "image_sha256": image_sha,
        "trusted_placement_policy_present": has_policy,
        "scope_evidence_sha256": ctx["metadata"]["value"][
            "synthetic_scope_evidence_sha256"
        ]["value"],
        "postgres_version": result["postgres_version"],
        "execution_status": result["execution"]["status"],
        "node_terminal_statuses": dict(
            Counter(f"{item['node_id']}:{item['status']}" for item in result["nodes"])
        ),
        "trace_event_count": len(events),
        "source_detections": n02["discovery_calls"][0]["detections"],
        "model_calls": dict(Counter(row["capability"] for row in audits.values())),
        "grounded_objects": len(objects),
        "completed_empty_tool_plans": len(tool_ids),
        "facts": observations,
        "failed_observations": failed_observations,
        "decisions": decision_details,
        "decision_counts": dict(counts),
        "coverage": coverage["coverage_status"],
        "summary_result": coverage["summary_result"],
    }


def compare(policy: dict, control: dict) -> dict:
    assert policy["trusted_placement_policy_present"] is True
    assert control["trusted_placement_policy_present"] is False
    assert policy["summary_result"] == "violation"
    assert control["summary_result"] == "uncertain"
    assert policy["execution_id"] != control["execution_id"]
    for field in ("bundle_digest", "image_sha256", "scope_evidence_sha256"):
        assert policy[field] == control[field]

    def boxes(run: dict) -> set:
        return {
            tuple(row["source_bbox_xyxy"])
            for key in ("facts", "failed_observations")
            for row in run[key].values()
        }

    assert boxes(policy) == boxes(control)
    evaluated_positive_boxes = {
        tuple(policy["facts"][obj]["source_bbox_xyxy"])
        for obj, decision in policy["decisions"].items()
        if decision["result"] == "violation"
    }
    observed_control_boxes = {
        tuple(fact["source_bbox_xyxy"])
        for fact in control["facts"].values()
        if fact["answer"] == "occupies_sidewalk"
    }
    return {
        "bundle_digest": policy["bundle_digest"],
        "original_sha256": policy["image_sha256"],
        "scope_evidence_sha256": policy["scope_evidence_sha256"],
        "same_grounding_boxes": len(boxes(policy)),
        "with_policy_execution": policy["execution_id"],
        "without_policy_execution": control["execution_id"],
        "with_policy_decisions": policy["decision_counts"],
        "without_policy_decisions": control["decision_counts"],
        "occupied_boxes_with_violation_only_when_policy_present": sorted(
            evaluated_positive_boxes & observed_control_boxes
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--compare-to-no-policy", type=Path)
    args = parser.parse_args()
    proof = verify(args.evidence)
    if args.compare_to_no_policy is not None:
        proof = compare(proof, verify(args.compare_to_no_policy))
    print(json.dumps(proof, ensure_ascii=False, indent=2))
