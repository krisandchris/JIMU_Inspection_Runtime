"""Cross-check one retained live Task against its API, original image and N06 output."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from zipfile import ZipFile


def read(path: Path) -> dict | list:
    return json.loads(path.read_bytes())


def check(run: Path, image: Path, expected: str, rule: str | None) -> dict:
    result = read(run / "result.json")
    api = read(run / "api.json")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    final = result["final"]
    coverage = final["coverage"]
    execution = result["execution_id"]
    assert result["media"]["sha256"] == digest
    assert result["execution"]["status"] in {"completed", "partial_failed"}
    assert coverage["execution_id"] == final["execution_id"] == execution
    assert coverage["bundle_digest"] == result["bundle_digest"]
    assert coverage["summary_result"] == expected
    assert len(api) >= 4 and api[0]["method"] == "POST" and api[0]["status"] == 202
    assert api[0]["body"]["execution"]["execution_id"] == execution
    assert all(reply["method"] == "GET" and reply["status"] == 200 for reply in api[1:])
    assert [r["body"] for r in api if r["path"].endswith("/result")] == [final]
    trace = [r["body"] for r in api if r["path"].endswith("/trace")]
    events = [event["number"] for page in trace for event in page["events"]]
    assert all(p["execution_id"] == execution for p in trace)
    assert events == list(range(1, trace[-1]["through_event_number"] + 1))
    assert trace[-1]["next_after_event_number"] is None
    groups = Counter(node["node_id"] for node in result["nodes"])
    assert set(groups) == {"N01", "N02", "N03", "N04", "N05", "N06"}
    ids = [d["object_ref"]["record_id"] for d in final["decisions"]]
    assert len(set(ids)) == len(ids) == coverage["object_count"]
    assert all(groups[node] == len(ids) for node in ("N04", "N05", "N06"))
    matches = coverage["matches"]
    for match in matches:
        assert match["object_ref"]["record_id"] in ids
        decision = next(
            d
            for d in final["decisions"]
            if d["decision_id"] == match["decision_ref"]["record_id"]
        )
        assert (
            decision["object_ref"] == match["object_ref"]
            and decision["result"] == "violation"
        )
    rule_ids = sorted({match["rule"]["rule_ref"]["artifact_id"] for match in matches})
    assert rule_ids == ([rule] if rule else [])
    with ZipFile(run / "signed-bundle.zip") as bundle:
        assert bundle.testzip() is None
    return {
        "task_id": result["task_id"],
        "execution_id": execution,
        "original_sha256": digest,
        "bundle_digest": result["bundle_digest"],
        "execution_status": result["execution"]["status"],
        "node_counts": dict(sorted(groups.items())),
        "node_statuses": dict(Counter(n["status"] for n in result["nodes"])),
        "coverage": coverage["coverage_status"],
        "result": expected,
        "objects": len(ids),
        "decisions": dict(Counter(d["result"] for d in final["decisions"])),
        "rule_ids": rule_ids,
        "api_status": [r["status"] for r in api],
        "trace_events": len(events),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("expected", choices=("violation", "uncertain", "no_violation"))
    parser.add_argument("--rule")
    options = parser.parse_args()
    print(
        json.dumps(
            check(options.run, options.image, options.expected, options.rule), indent=2
        )
    )
