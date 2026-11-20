"""Cross-check the retained regional provider receipts against the six-node output."""

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT.parent / "N04_ORIGINAL_MULTI_REGION"


def load(path):
    return json.loads(path.read_bytes())


def verify(case):
    run = ROOT / case
    scene = Path(case).name.split("-")[0]
    result = load(run / "result.json")
    coverage = result["final"]["coverage"]
    assert result["execution"]["status"] == "completed"
    assert {node["node_id"] for node in result["nodes"]} == {
        "N01",
        "N02",
        "N03",
        "N04",
        "N05",
        "N06",
    }
    assert all(node["status"] == "succeeded" for node in result["nodes"])
    logs = [load(p) for p in (run / "provider").glob("live-*.json")]
    logical = [row for row in logs if row["capability"] == "object_observation-result"]
    (physical,) = (load(p) for p in (run / "provider/region-batches").glob("*.json"))
    original = ROOT.parent / "LIVE_THREE_TASKS_20261119" / "images" / f"{scene}.png"
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    assert digest == result["media"]["sha256"]
    batch = physical["physical_request"]["batch"]
    assert (digest, result["execution_id"], result["bundle_digest"]) == (
        batch["image_sha256"],
        batch["execution_id"],
        batch["bundle_digest"],
    )
    physical_id = physical["physical_request_id"]
    assert sum(row["physical_http_dispatches"] for row in logical) == 1
    assert (
        physical["physical_http_dispatches"] == 1 and physical["state"] == "completed"
    )
    assert all(
        row["physical_request_id"] == physical_id and row["image"]["sha256"] == digest
        for row in logical
    )
    objects = next(
        row["objects"] for row in result["node_outputs"] if row["kind"] == "grounding"
    )
    object_ids = {obj["object_id"] for obj in objects}
    observations = [
        row for row in result["node_outputs"] if row["kind"] == "observation"
    ]
    assert (
        len(object_ids) == len(logical) == len(observations) == coverage["object_count"]
    )
    assert (
        object_ids
        == {row["projection_object_id"] for row in logical}
        == set(batch["object_ids"])
    )
    assert object_ids == {
        row["object_ref"]["record_id"] for row in result["final"]["decisions"]
    }
    facts = 0
    for ob in observations:
        obj = ob["inputs"]["object_record"]
        assert ob["inputs"]["image_mode"] == "original_regions_v1"
        assert "marked_media_ref" not in ob["inputs"]["image_view"]
        assert ob["invocation"]["provider_request_id"] == physical_id
        questions = ob["batch"]["question_results"]
        assert {row["question_ref"]["artifact_id"] for row in questions} == {
            ref["artifact_id"] for ref in obj["effective_question_refs"]
        }
        assert all(
            row["primary_fact"]["object_id"] == obj["object_id"] for row in questions
        )
        facts += len(questions)
    physical_response = physical["physical_response"]
    summary = {
        "execution_id": result["execution_id"],
        "bundle_digest": result["bundle_digest"],
        "physical_request_id": physical_id,
        "region_count": len(objects),
        "logical_bridge_requests": len(logical),
        "physical_n04_requests": physical["physical_http_dispatches"],
        "upstream_seconds": physical_response["upstream"]["round_trip_s"],
        "upstream_usage": physical_response["qwen"]["token_usage"],
        "fact_count": facts,
        "result": coverage["summary_result"],
        "coverage": coverage["coverage_status"],
        "decisions": dict(
            Counter(row["result"] for row in result["final"]["decisions"])
        ),
    }
    if scene == "goods":
        historical = load(HIST / "measured/goods-input.json")["id_map"]
        reference = load(HIST / "blind_reference.json")["goods"]
        aliases = {tuple(obj["xyxy_px"]): alias for alias, obj in historical.items()}
        comparison = {}
        for obj in objects:
            alias = aliases[tuple(obj["bbox"]["xyxy"])]
            observed = json.loads(physical["projections"][obj["object_id"]])[
                "sidewalk_encroachment"
            ]
            comparison[alias] = {
                "expected": reference[alias]["value"],
                "observed": observed,
            }
        assert comparison.keys() == reference.keys()
        summary["goods_by_matched_source_box"] = comparison
    return summary


if __name__ == "__main__":
    cases = ("parking", "mask", "goods", "goods-default")
    reviewed = (*cases, "parking-one", "parking-two")
    print(
        json.dumps(
            {
                "initial": {case: verify(case) for case in cases},
                "reviewed": {case: verify(f"reviewed/{case}") for case in reviewed},
            },
            indent=2,
        )
    )
