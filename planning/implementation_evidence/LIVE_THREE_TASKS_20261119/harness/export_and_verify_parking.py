"""Independently verify and export parking runtime receipts and selected sidecars."""
import base64
import io
import json
import shutil
import sqlite3
import sys
from hashlib import sha256
from pathlib import Path

from PIL import Image, ImageChops

from jimu_runtime.domain.runtime.media import BBoxXYXY
from jimu_runtime.inspection.geometry._cpu import classify, measure
from jimu_runtime.inspection.media.decoder import render_rgb_outline_png

folder, output, original = map(Path,sys.argv[1:])
output.mkdir(exist_ok=False)
(output/"sidecars").mkdir()


def digest(raw):
    return sha256(raw).hexdigest()


def save_json(path, body):
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def get_bitmap(src):
    with Image.open(io.BytesIO(src)) as image:
        return image.copy()


result = json.loads((folder/"result.json").read_text())
api = json.loads((folder/"api.json").read_text())
assert [entry["status"] for entry in api] == [202,200,200,200]
assert result["dispositions"][-1] == "finalized"
assert {item["node_id"] for item in result["nodes"]} == {f"N0{index}" for index in range(1,7)}
original_bytes = original.read_bytes()
media_sha = digest(original_bytes)
assert media_sha == result["media"]["sha256"]
grounding = next(item for item in result["node_outputs"] if item["kind"]=="grounding")
observations = {item["inputs"]["object_record"]["object_id"]: item
                for item in result["node_outputs"] if item["kind"]=="observation"}
tools = {item["inputs"]["object_record"]["object_id"]: item
         for item in result["node_outputs"] if item["kind"]=="tool"}
decisions = {item["draft"]["inputs"]["object_record"]["object_id"]: item
             for item in result["node_outputs"] if item["kind"]=="decision"}
bridge = [json.loads(path.read_text()) for path in (folder/"wire").glob("*.json")]
bridge_receipts = {doc["bridge_request_id"]:doc for doc in bridge}
recorded_model_calls = [
    item for record in result["node_outputs"]
    for item in (record.get("invocations") or ([record["invocation"]] if record.get("invocation") else []))
]
assert len(recorded_model_calls) == len(bridge)
for invoked in recorded_model_calls:
    physical = bridge_receipts[invoked["provider_request_id"]]
    assert physical["runtime_request"]["sha256"] == invoked["request_digest"]
    assert invoked["outcome"]["status"] == "succeeded"
    assert physical["runtime_http"] == 200

segmentation_sidecars = {}
for path in (folder/"tool-wire").glob("*.audit.json"):
    audit = json.loads(path.read_text())
    wire = path.with_name(path.name.replace(".audit.json",".wevis.json"))
    raw = wire.read_bytes()
    upstream = json.loads(raw)
    assert digest(raw) == audit["upstream_response_sha256"]
    assert audit["source_sha256"] == media_sha
    assert upstream["model"] == "WeVisOne-VL-4B-NF4"
    assert upstream["model_forward_calls"] == audit["model_forward_calls"] > 0
    crop = audit["crop_xyxy"]
    full = get_bitmap(original_bytes)
    if crop:
        target = full.crop(crop)
        wire_image = io.BytesIO()
        target.save(wire_image, format="PNG")
        assert digest(wire_image.getvalue()) == upstream["image_sha256"]
    else:
        target = full
        assert media_sha == upstream["image_sha256"]
    combined = Image.new("L",target.size)
    for item in upstream["masks"]:
        mask = get_bitmap(base64.b64decode(item["png_base64"],validate=True))
        combined = ImageChops.lighter(combined,mask)
    aligned = Image.new("L",full.size)
    aligned.paste(combined, crop[:2] if crop else (0,0))
    assert aligned.histogram()[255] == audit["aligned_foreground"]
    segmentation_sidecars[audit["aligned_mask_sha256"]] = aligned

all_objects = []
stored_by_digest = {}
for item in (folder/"media/media-v1").iterdir():
    if item.is_file():
        stored_by_digest[digest(item.read_bytes())] = item

for obj in grounding["objects"]:
    oid = obj["object_id"]
    observed = observations[oid]
    tool = tools[oid]
    decision = decisions[oid]
    assert observed["inputs"]["object_record"] == tool["inputs"]["object_record"] == decision["draft"]["inputs"]["object_record"] == obj
    assert observed["batch"]["outcome"] == "complete"
    assert observed["inputs"]["image_view"]["roi"] == obj["bbox"]
    assert tool["inputs"]["image_view"]["roi"] == obj["bbox"]

    marked = obj.get("marked_frame")
    if marked:
        marked_sha = marked["media_ref"]["sha256"]
        generated = render_rgb_outline_png(original_bytes, BBoxXYXY.model_validate_json(json.dumps(obj["bbox"])))
        stored_marked = stored_by_digest[marked_sha].read_bytes()
        assert stored_marked == generated
        assert tool["inputs"]["image_view"]["marked_media_ref"] == marked["media_ref"]
        assert observed["inputs"]["image_view"]["marked_media_ref"] == marked["media_ref"]
        assert bridge_receipts[observed["invocation"]["provider_request_id"]]["image"]["sha256"] == marked_sha
        (output/"sidecars"/f"marked-{oid.split('.')[-1][:10]}.png").write_bytes(stored_marked)
    else:
        assert bridge_receipts[observed["invocation"]["provider_request_id"]]["image"]["sha256"] == media_sha

    published = {}
    for evidence in tool["evidence"]:
        if evidence["evidence_type"] not in ("mask","geometry"):
            continue
        path = folder/"media"/evidence["storage"]["object_key"]
        content = path.read_bytes()
        assert len(content) == evidence["byte_size"]
        assert digest(content) == evidence["sha256"]
        assert evidence["scope"]["object_ref"]["record_id"] == oid
        stage = evidence["producer_id"]
        if evidence["evidence_type"] == "mask":
            aligned = get_bitmap(content)
            assert aligned.tobytes() == segmentation_sidecars[evidence["sha256"]].tobytes()
            ext = "png"
            published[stage] = aligned
        else:
            ext = "json"
            published[stage] = json.loads(content)
        (output/"sidecars"/f"{stage}-{oid.split('.')[-1][:10]}.{ext}").write_bytes(content)
    geometry = published["wheel_boundary_geometry"]
    geometry_metrics = measure(published["wheel_segmentation"],published["tactile_paving_segmentation"], geometry["parking_area"]["bbox"]["xyxy"])
    assert geometry_metrics == geometry["metrics"]
    assert classify(geometry_metrics) == (geometry["relation"],geometry["reason"])

    positive = decision["draft"]["decision"]
    assert len(positive["rule_evaluations"])==1
    checked_rule = positive["rule_evaluations"][0]
    all_objects.append({
        "object_id":oid,"bbox":obj["bbox"]["xyxy"],"members":obj.get("grounding_member_boxes"),
        "marked_frame":marked,"decision":positive["result"],"decision_id":positive["decision_id"],
        "rule_id":checked_rule["rule"]["rule_ref"]["artifact_id"],
        "rule_outcome":checked_rule["result"]["outcome"],
        "facts":[question["primary_fact"] for question in observed["batch"]["question_results"]],
        "geometry":geometry,
    })

for name in ("api.json","intake.json","result.json","wire","tool-wire"):
    src = folder/name
    if src.is_dir():
        shutil.copytree(src, output/name)
    else:
        shutil.copyfile(src,output/name)
log = folder.parent/(folder.name+".log")
shutil.copyfile(log,output/"run.log")
db = folder/"releases/bundle-store.sqlite3"
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as ledger:
    rows = ledger.execute("SELECT operation_id,sha256,raw FROM admissions JOIN archives ON stored_archive_sha256=sha256").fetchall()
    for op, expected, raw in rows:
        assert digest(raw)==expected
        if op == "admit.synthetic.scene.initial":
            (output/"signed-bundle.tar.gz").write_bytes(raw)
assert (output/"signed-bundle.tar.gz").is_file()

save_json(output/"verification.json",{
    "mode":"isolated_local_harness_using_live_models", "task_id":result["task_id"],
    "execution_id":result["execution_id"],"bundle_digest":result["bundle_digest"],
    "release_id":result["release_id"],"postgres_version":result["postgres_version"],
    "execution_status":result["execution"]["status"],
    "summary_result":result["final"]["coverage"]["summary_result"],
    "coverage_status":result["final"]["coverage"]["coverage_status"],
    "original_media_sha256":media_sha,"provider_calls":len(bridge),
    "verified_segmentation_calls":len(list((folder/"tool-wire").glob("*.audit.json"))),
    "objects":all_objects,
})
files = sorted(f for f in output.rglob("*") if f.is_file())
(output/"sha256sums.txt").write_text("".join(f"{digest(path.read_bytes())}  {path.relative_to(output)}\n" for path in files))
print(result["execution_id"], result["execution"]["status"], len(all_objects), len(bridge), sep=" ")
