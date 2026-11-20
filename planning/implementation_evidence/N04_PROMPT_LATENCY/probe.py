"""Time one forward request for each prompt configuration on the same original image."""

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator

OUT = Path(__file__).resolve().parent
HIST = OUT.parent / "N04_ORIGINAL_MULTI_REGION"
hist = json.loads((HIST / "measured/goods-input.json").read_bytes())
parser = argparse.ArgumentParser()
parser.add_argument("--evidence-code", action="store_true")
args = parser.parse_args()
schema = hist["response_schema"]
original = hist["prompt"]
fields = json.loads(original.split("\n", 1)[1])
boxes = {t["id"]: t["bbox_2d"] for t in fields["targets"]}
zone = fields["reference_regions"]["pedestrian_zone"]["bbox_2d"]
compact = (
    "One original photo. Boxes use [left,top,right,bottom], 0..999, origin top-left. "
    "For each carton ID answer sidewalk_encroachment independently. "
    "If its floor contact is hidden and you cannot visually trace its support to the floor, "
    "answer insufficient_visibility. Otherwise classify the visible contact footprint: "
    "overlaps zone = occupies_sidewalk; clearly disjoint = does_not_occupy_sidewalk. "
    "Box overlap alone cannot establish contact. Return schema JSON only.\n"
    + json.dumps({"zone": zone, "cartons": boxes}, separators=(",", ":"))
)
image = (OUT.parent / "LIVE_THREE_TASKS_20261119/images/goods.png").read_bytes()
assert hashlib.sha256(image).hexdigest() == hist["image_sha256"]
image_url = "data:image/png;base64," + base64.b64encode(image).decode()
client = build_opener(ProxyHandler({}))
reference = json.loads((HIST / "blind_reference.json").read_bytes())["goods"]
variants = [
    ("original_default", original, None),
    ("compact_default", compact, None),
    ("compact_direct", compact, False),
]
codes = {
    "contact_in_zone": "occupies_sidewalk",
    "contact_outside_zone": "does_not_occupy_sidewalk",
    "contact_unseen": "insufficient_visibility",
}
if args.evidence_code:
    task = (
        "One original photo. Coordinates are xyxy, top-left origin, normalized 0..999. "
        "For each carton, locate its physical footprint on the ground. If stacked, follow "
        "each visible supporting surface all the way down to the ground. If any link or "
        "the footprint cannot be seen, output contact_unseen and stop for this carton. "
        "If visible, output contact_in_zone for any footprint overlap, or contact_outside_zone "
        "for a footprint visibly wholly outside. Judge each carton separately. "
        "A box coordinate locates a carton, not its physical footprint. Return JSON only."
        + "\n"
        + json.dumps({"zone": zone, "cartons": boxes}, separators=(",", ":"))
    )
    schema = {
        "type": "object",
        "properties": {key: {"type": "string", "enum": list(codes)} for key in boxes},
        "required": list(boxes),
        "additionalProperties": False,
    }
    variants = [("evidence_code_direct", task, False)]
    (OUT / "evidence_code.txt").write_text(task + "\n")
else:
    (OUT / "compact.txt").write_text(compact + "\n")
for label, text, thinking in variants:
    path = OUT / (label + ".json")
    if path.exists():
        raise FileExistsError(path)
    body = {
        "model": "Qwen3_5-9B",
        "stream": False,
        "seed": 42,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "region_observation",
                "strict": True,
                "schema": schema,
            },
        },
    }
    if thinking is False:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    wire = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    req = Request(
        "http://192.168.32.6:8029/v1/chat/completions",
        data=wire,
        headers={"Content-Type": "application/json"},
    )
    t = time.perf_counter()
    with client.open(req, timeout=180) as resp:
        raw = resp.read()
        elapsed = time.perf_counter() - t
    a = json.loads(raw)
    ch = a["choices"][0]
    assert ch["finish_reason"] == "stop"
    ans = json.loads(ch["message"]["content"])
    Draft202012Validator(schema).validate(ans)
    raw_answers = ans
    if args.evidence_code:
        ans = {key: {"sidewalk_encroachment": codes[code]} for key, code in ans.items()}
    scores = {
        key: row["sidewalk_encroachment"] == reference[key]["value"]
        for key, row in ans.items()
    }
    result = {
        "variant": label,
        "seconds": elapsed,
        "usage": a.get("usage"),
        "model": a["model"],
        "request_id": a["id"],
        "finish": ch["finish_reason"],
        "seed": 42,
        "request_sha256": hashlib.sha256(wire).hexdigest(),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "answers": ans,
        "raw_answers": raw_answers,
        "matches_blind_reference": scores,
        "correct": sum(scores.values()),
    }
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(label, round(elapsed, 2), result["correct"], a.get("usage"), flush=True)
