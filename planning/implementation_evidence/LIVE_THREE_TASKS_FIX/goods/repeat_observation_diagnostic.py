"""Verify and optionally repeat one byte-identical failed provider request."""

import argparse
import hashlib
import json
from base64 import b64encode
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from examples.live_inspection.providers.live_wire_bridge import (
    _QWEN_MODEL,
    MODEL_ID,
    ProfileRoute,
    _json,
    start_bridge,
)


def summary(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def reconstruct(run: Path, original: dict) -> bytes:
    pins = {name: original[name] for name in ("image", "prompt", "response_schema")}
    inputs = {}
    for path in (run / "media" / "media-v1").iterdir():
        size = path.stat().st_size
        candidates = [name for name, pin in pins.items() if pin["bytes"] == size]
        if not candidates or not path.is_file():
            continue
        content = path.read_bytes()
        actual = summary(content)
        for name in candidates:
            if actual == pins[name]:
                inputs[name] = content
    encoded = b64encode(inputs["image"]).decode("ascii")
    value = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": inputs["prompt"].decode()},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{original['image_media_type']};base64,{encoded}",
                        },
                    },
                ],
            }
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "jimu_response",
                "strict": True,
                "schema": json.loads(inputs["response_schema"]),
            },
        },
    }
    request = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if summary(request) != original["runtime_request"]:
        raise ValueError(
            "The original Runtime request cannot be reproduced byte for byte"
        )
    return request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("request_id")
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if not args.request_id.startswith("live-") or not args.request_id[5:].isalnum():
        raise ValueError("Supply one recorded bridge request identity")
    original = json.loads(
        (args.run / "provider" / f"{args.request_id}.json").read_bytes()
    )
    if (
        original["capability"] != "object_observation-result"
        or original["failure"] != "qwen_completion_incomplete"
    ):
        raise ValueError("This diagnostic requires a recorded incomplete observation")
    data = reconstruct(args.run, original)
    upstream = summary(_json({**json.loads(data), "model": _QWEN_MODEL}))
    if upstream != original["upstream"]["request"]:
        raise ValueError("The original Qwen request cannot be reproduced byte for byte")
    if args.verify_only:
        print(
            json.dumps({"runtime_request": summary(data), "upstream_request": upstream})
        )
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with start_bridge(
        args.output / "provider",
        profile_routes={"sidewalk_goods": ProfileRoute("cardboard boxes")},
    ) as server:
        request = Request(
            server.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(request, timeout=95)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            request_id = response.headers["x-request-id"]
    audit = json.loads((args.output / "provider" / f"{request_id}.json").read_bytes())
    assert audit["runtime_request"] == original["runtime_request"]
    assert audit["upstream"]["request"] == original["upstream"]["request"]
    assert request_id != original["bridge_request_id"]
    diagnostic = {
        "kind": "separate_provider_diagnostic",
        "original_bridge_request_id": original["bridge_request_id"],
        "diagnostic_bridge_request_id": request_id,
        "runtime_request": audit["runtime_request"],
        "upstream_request": audit["upstream"]["request"],
        "bridge_http": status,
        "qwen": audit.get("qwen"),
        "bridge_failure": audit.get("failure"),
    }
    body = json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n"
    (args.output / "diagnostic.json").write_text(body, encoding="utf-8")
    print(body, end="")


if __name__ == "__main__":
    main()
