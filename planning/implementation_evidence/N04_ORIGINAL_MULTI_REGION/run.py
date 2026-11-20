"""Single-dispatch comparison using the retained images, grounding and signed questions."""

import argparse
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zipfile import ZipFile

from jsonschema import Draft202012Validator
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
HISTORY = ROOT / "planning/implementation_evidence"
CASES = {
    "parking": "parking",
    "mask": "mask/synthetic-roster",
    "goods": "goods/registered-policy",
}
BASE = "http://192.168.32.6:8029/v1"
HTTP = build_opener(ProxyHandler({}))


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def box(coords, width, height):
    x0, y0, x1, y1 = coords
    assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
    v = [
        min(999, round(n * 1000 / d))
        for n, d in zip(coords, (width, height, width, height), strict=True)
    ]
    assert v[0] < v[2] and v[1] < v[3]
    return v


def run(case, source, output):
    directory = HISTORY / "LIVE_THREE_TASKS_FIX" / source
    image = HISTORY / "LIVE_THREE_TASKS_20261119/images" / (case + ".png")
    image_bytes = image.read_bytes()
    digest = hashlib.sha256(image_bytes).hexdigest()
    with Image.open(image) as im:
        width, height = im.size
    historical = json.loads((directory / "result.json").read_bytes())
    assert digest == historical["media"]["sha256"]
    objects = next(
        x["objects"] for x in historical["node_outputs"] if x["kind"] == "grounding"
    )
    mapping, targets, questions, schemas = {}, [], {}, {}
    with ZipFile(directory / "signed-bundle.zip") as z:
        contract = json.loads(
            z.read("prompts/response_contracts/object_observation-result.schema.json")
        )
        for index, obj in enumerate(objects, start=1):
            alias = f"r{index}"
            keys = [q["artifact_id"] for q in obj["effective_question_refs"]]
            mapping[alias] = {
                "object_id": obj["object_id"],
                "xyxy_px": obj["bbox"]["xyxy"],
            }
            target = {
                "id": alias,
                "subject": obj["entity_ref"]["artifact_id"],
                "bbox_2d": box(obj["bbox"]["xyxy"], width, height),
                "questions": keys,
            }
            if case == "parking":
                target["member_boxes"] = [
                    {"member_index": i, "bbox_2d": box(m["xyxy"], width, height)}
                    for i, m in enumerate(obj["grounding_member_boxes"], 1)
                ]
            targets.append(target)
            for k in keys:
                q = json.loads(z.read(f"prompts/templates/{k}.json"))["template_text"]
                q = q.replace("highlighted", "specified").replace(
                    "the red outline", "the supplied bounding box"
                )
                if k == "sidewalk_encroachment":
                    literal = "x=445..1320 and y=410..980, in original 1536 by 1024 pixel coordinates (top-left origin)"
                    assert literal in q
                    q = q.replace(
                        literal, "the pedestrian_zone specified in reference_regions"
                    )
                questions[k] = {"prompt": q}
                if "enum" in contract["properties"][k]:
                    questions[k]["answers"] = contract["properties"][k]["enum"]
            schemas[alias] = {
                "type": "object",
                "additionalProperties": False,
                "required": keys,
                "properties": {k: contract["properties"][k] for k in keys},
            }
    payload = {"questions": questions, "targets": targets}
    if case == "goods":
        payload["reference_regions"] = {
            "pedestrian_zone": {"bbox_2d": box((445, 410, 1320, 980), width, height)}
        }
    prompt = (
        "Observe the one supplied original image. Each bbox_2d is [left,top,right,bottom], integer thousandths of full image width and height, clamped to 0..999, origin top left. "
        "Use each target's coordinates to identify that specific subject. Evaluate only its listed questions, using surrounding visual context where needed. "
        "Answer by the provided target IDs and question IDs. If unclear use the uncertainty option for that question. Do not decide violations. Return the JSON object only.\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(mapping),
        "properties": schemas,
    }
    frame = {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(image_bytes).decode()
        },
    }
    body = {
        "model": "Qwen3_5-9B",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}, frame]}
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "region_observation",
                "strict": True,
                "schema": schema,
            },
        },
    }
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    save(
        output / (case + "-input.json"),
        {
            "source_case": source,
            "image_sha256": digest,
            "image_size": [width, height],
            "id_map": mapping,
            "prompt": prompt,
            "response_schema": schema,
            "request_bytes": len(data),
            "request_sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    request = Request(
        BASE + "/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    log = {"start_utc": datetime.now(UTC).isoformat(), "request_count": 1}
    start = time.perf_counter()
    try:
        with HTTP.open(request, timeout=180) as r:
            raw = r.read()
            log.update(http=r.status)
        log["seconds"] = time.perf_counter() - start
        a = json.loads(raw)
        choice = a["choices"][0]
        log.update(
            id=a["id"],
            model=a["model"],
            usage=a.get("usage"),
            finish=choice["finish_reason"],
            raw_content=choice["message"]["content"],
            response_sha256=hashlib.sha256(raw).hexdigest(),
        )
        answers = json.loads(log["raw_content"])
        Draft202012Validator(schema).validate(answers)
        if case == "parking":
            members = answers["r1"]["parking_state"]["members"]
            assert sorted(m["member_index"] for m in members) == list(
                range(1, 1 + len(targets[0]["member_boxes"]))
            )
            assert all(
                m["state"] in ("parked", "unparked", "unknown")
                and 0 < len(m["visual_evidence"].strip()) <= 160
                for m in members
            )
        assert log["finish"] == "stop" and log["model"] == body["model"]
        log.update(answers=answers, validated=True)
    except Exception as err:
        log.setdefault("seconds", time.perf_counter() - start)
        log.update(validated=False, error=str(err))
        if isinstance(err, HTTPError):
            log.update(http=err.code, response=err.read(8192).decode(errors="replace"))
    save(output / (case + "-response.json"), log)
    print(case, log["seconds"], log["validated"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    dest = parser.parse_args().output
    dest.mkdir(parents=True, exist_ok=False)
    with HTTP.open(BASE + "/models", timeout=5) as models:
        save(dest / "models.json", json.load(models))
    for case, source in CASES.items():
        run(case, source, dest)
