"""Prove all 347 WP-03 public schemas and their index have byte-identical pins."""

import json
import sys
from pathlib import Path


def main(previous: Path, current: Path) -> None:
    prefix = "schemas/generated/"
    old = {
        name: sha
        for name, sha in json.loads(previous.read_bytes()).items()
        if name.startswith(prefix)
    }
    new = {
        name: sha
        for name, sha in json.loads(current.read_bytes()).items()
        if name.startswith(prefix)
    }
    mismatches = {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(
            name for name in new.keys() & old.keys() if old[name] != new[name]
        ),
    }
    assert len(old) == 348 and prefix + "index.json" in old
    print(
        json.dumps(
            {
                "previous_schemas": len(old) - 1,
                "current_schemas": len(new) - 1,
                **mismatches,
            },
            sort_keys=True,
        )
    )
    assert old == new


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
