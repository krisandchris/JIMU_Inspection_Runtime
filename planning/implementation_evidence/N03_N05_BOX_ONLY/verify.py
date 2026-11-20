"""Verify saved execution, provider and tool records against the historical source photos."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent


def review(archive, case):
    def load(name):
        return json.loads(archive.read(f"{case}/{name}"))

    scene = case.split("-")[0]
    source = ROOT.parent / "LIVE_THREE_TASKS_20261119" / "images" / f"{scene}.png"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result, receipt = load("result.json"), load("batch.json")
    assert result["media"]["sha256"] == digest
    assert result["marked_frame_asset_count"] == 0
    assert {node["node_id"] for node in result["nodes"]} == {
        "N01",
        "N02",
        "N03",
        "N04",
        "N05",
        "N06",
    }
    outputs = result["node_outputs"]
    objects = {
        obj["object_id"]: obj
        for row in outputs
        if row["kind"] == "grounding"
        for obj in row["objects"]
    }
    assert objects
    for obj in objects.values():
        assert obj["source_media_ref"] == result["media"]
        assert (
            "marked_frame" not in obj
            and obj["coordinate_system"]["frame"] == "original_image"
        )
        members = [
            box["xyxy"] for box in obj.get("grounding_member_boxes", [obj["bbox"]])
        ]
        assert members == sorted(members)
        assert [
            op(box[i] for box in members) for i, op in enumerate((min, min, max, max))
        ] == obj["bbox"]["xyxy"]
    physical = receipt["physical_request"]["batch"]
    assert (
        physical["execution_id"] == result["execution_id"]
        and set(physical["object_ids"]) == objects.keys()
    )
    assert (
        physical["image_sha256"] == digest
        and physical["bundle_digest"] == result["bundle_digest"]
    )
    assert receipt["state"] == "completed" and receipt["physical_http_dispatches"] == 1
    requests = [
        json.loads(archive.read(key))
        for key in archive.namelist()
        if key.startswith(f"{case}/observations/")
    ]
    assert {r["projection_object_id"] for r in requests} == objects.keys()
    assert sum(r["physical_http_dispatches"] for r in requests) == 1
    assert all(
        r["physical_request_id"] == receipt["physical_request_id"]
        and r["image"]["sha256"] == digest
        for r in requests
    )
    for kind in ("observation", "tool"):
        rows = [row for row in outputs if row["kind"] == kind]
        assert len(rows) == len(objects)
        assert {
            row["inputs"]["object_record"]["object_id"] for row in rows
        } == objects.keys()
        for row in rows:
            inputs = row["inputs"]
            obj = inputs["object_record"]
            assert obj == objects[obj["object_id"]]
            assert inputs["image_view"] == {"roi": obj["bbox"], "transforms": []}
    decisions = result["final"]["decisions"]
    assert {d["object_ref"]["record_id"] for d in decisions} == objects.keys()
    summary = {
        "execution_id": result["execution_id"],
        "status": result["execution"]["status"],
        "result": result["final"]["coverage"]["summary_result"],
        "groups": len(objects),
        "members_per_group": [
            len(obj.get("grounding_member_boxes", [obj["bbox"]]))
            for obj in objects.values()
        ],
        "n04_physical_requests": receipt["physical_http_dispatches"],
        "marked_frame_asset_count": result["marked_frame_asset_count"],
    }
    if scene == "parking":
        geometry = load("geometry.json")
        obj = objects[geometry["object_ref"]["record_id"]]
        assert geometry["original_sha256"] == digest
        assert [m["bbox"] for m in geometry["members"]] == [
            box["xyxy"] for box in obj["grounding_member_boxes"]
        ]
        summary["member_matches"] = [m["matches"] for m in geometry["members"]]
    if scene == "goods":
        history = ROOT.parent / "N04_ORIGINAL_MULTI_REGION"
        aliases = json.loads((history / "measured/goods-input.json").read_bytes())[
            "id_map"
        ]
        reference = json.loads((history / "blind_reference.json").read_bytes())["goods"]
        by_box = {
            tuple(r["xyxy_px"]): reference[k]["value"] for k, r in aliases.items()
        }
        assert len(by_box) == len(objects)
        summary["observations_matching_blind_reference"] = sum(
            json.loads(receipt["projections"][key])["sidewalk_encroachment"]
            == by_box[tuple(obj["bbox"]["xyxy"])]
            for key, obj in objects.items()
        )
    return summary


if __name__ == "__main__":
    with ZipFile(ROOT / "receipts.zip") as archive:
        print(
            json.dumps(
                {
                    case: review(archive, case)
                    for case in (
                        "parking",
                        "parking-final",
                        "parking-112",
                        "mask",
                        "goods",
                    )
                },
                indent=2,
            )
        )
