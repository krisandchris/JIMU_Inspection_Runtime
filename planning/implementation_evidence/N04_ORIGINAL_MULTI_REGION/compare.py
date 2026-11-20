"""Compare historical model wire time and N04 observation values by source Object ID."""

import json
from pathlib import Path

D = Path(__file__).resolve().parent
H = D.parent / "LIVE_THREE_TASKS_FIX"
M = D / "measured"


def read(path):
    return json.loads(path.read_bytes())


def flat(question, val):
    if question == "parking_state":
        return {
            f"parking_state.{x['member_index']}": x["state"] for x in val["members"]
        }
    return {question: val}


def unwrap(val):
    if val["kind"] == "record":
        return {k: unwrap(v) for k, v in val["value"].items()}
    if val["kind"] == "list":
        return [unwrap(v) for v in val["value"]]
    return val["value"]


def compare(case, source):
    p = H / source
    result = read(p / "result.json")
    previous = {}
    for batch in result["node_outputs"]:
        if batch["kind"] != "observation":
            continue
        old = previous.setdefault(batch["batch"]["object_ref"]["record_id"], {})
        for q in batch["batch"]["question_results"]:
            fact = q["primary_fact"]
            if fact["status"] == "known":
                old.update(flat(q["primary_fact_key"], unwrap(fact["value"])))
            elif fact["status"] == "unknown":
                old[q["primary_fact_key"]] = "insufficient_visibility"
            else:
                raise ValueError(fact["status"])
    wires = [
        x
        for f in (p / ("wire" if case == "parking" else "provider")).glob("*.json")
        if (x := read(f))["capability"] == "object_observation-result"
    ]
    new = read(M / f"{case}-response.json")
    assert new["validated"]
    measured_input = read(M / f"{case}-input.json")
    comparisons = []
    for alias, vals in new["answers"].items():
        obj = measured_input["id_map"][alias]["object_id"]
        for q, value in vals.items():
            for key, v in flat(q, value).items():
                old = previous.get(obj, {}).get(key)
                comparisons.append(
                    {
                        "region": alias,
                        "question": key,
                        "old": old,
                        "new": v,
                        "exact": None if old is None else v == old,
                    }
                )
    t_old = sum(w["upstream"]["round_trip_s"] for w in wires)
    return {
        "old_http_calls": len(wires),
        "new_http_calls": new["request_count"],
        "old_wire_s": t_old,
        "new_wire_s": new["seconds"],
        "old_over_new": t_old / new["seconds"],
        "old_request_bytes": sum(w["upstream"]["request"]["bytes"] for w in wires),
        "new_request_bytes": measured_input["request_bytes"],
        "comparisons": comparisons,
        "matching": sum(x["exact"] is True for x in comparisons),
        "comparable": sum(x["exact"] is not None for x in comparisons),
    }


if __name__ == "__main__":
    results = {
        c: compare(c, s)
        for c, s in (
            ("parking", "parking"),
            ("mask", "mask/synthetic-roster"),
            ("goods", "goods/registered-policy"),
        )
    }
    (D / "comparison.json").write_text(json.dumps(results, indent=2) + "\n")
