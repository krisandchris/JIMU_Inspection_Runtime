"""Verify the primary product manifest and every entry at the current workspace."""

import hashlib
import json
import sys
from pathlib import Path
from subprocess import check_output
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[4]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(manifest_path: Path, expected_manifest_sha: str) -> int:
    observed_manifest_sha = sha(manifest_path)
    if observed_manifest_sha != expected_manifest_sha:
        print(f"manifest sha256 mismatch: {observed_manifest_sha}")
        return 1
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not entries:
        raise ValueError("empty primary product manifest")
    mismatches = []
    for name, expected in entries.items():
        path = ROOT / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(ROOT)
            or not path.is_file()
        ):
            mismatches.append(name)
            continue
        if sha(path) != expected:
            mismatches.append(name)
    print(
        json.dumps(
            {
                "manifest_sha256": observed_manifest_sha,
                "entries": len(entries),
                "mismatches": mismatches,
            },
            sort_keys=True,
        )
    )
    return int(bool(mismatches))


def diff_product(previous_path: Path, current_path: Path) -> int:
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    current = json.loads(current_path.read_text(encoding="utf-8"))
    added = sorted(current.keys() - previous.keys())
    changed = sorted(
        path
        for path in current.keys() & previous.keys()
        if current[path] != previous[path]
    )
    removed = sorted(previous.keys() - current.keys())
    print(
        json.dumps({"added": added, "changed": changed, "removed": removed}, indent=2)
    )
    return 0


def schema_baseline(previous_path: Path, current_path: Path) -> int:
    prefix = "schemas/generated/"
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    current = json.loads(current_path.read_text(encoding="utf-8"))
    names = {name.removeprefix(prefix) for name in previous if name.startswith(prefix)}
    if len(names) != 348 or "index.json" not in names:
        raise ValueError("WP-02 baseline must contain 347 schemas and index.json")
    observed = {path.name for path in (ROOT / prefix).iterdir()}
    frozen = {name.removeprefix(prefix) for name in current if name.startswith(prefix)}
    delta = {
        "baseline_schema_count": len(names) - 1,
        "current_schema_count": len(observed) - 1,
        "added": sorted(observed - names),
        "missing": sorted(names - observed),
        "changed": sorted(
            name
            for name in names & observed
            if sha(ROOT / prefix / name) != previous[f"{prefix}{name}"]
        ),
        "working_tree_vs_frozen_name_mismatch": sorted(observed ^ frozen),
    }
    print(json.dumps(delta, sort_keys=True))
    return int(bool(delta["missing"] or delta["working_tree_vs_frozen_name_mismatch"]))


def archive_snapshot(frozen: Path) -> int:
    manifest = json.loads((frozen / "product_sha256.json").read_bytes())
    zip_path = frozen / "product.zip"
    expected = (frozen / "product.zip.sha256").read_text().split()[0]
    observed = sha(zip_path)
    assert expected == observed
    with ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)) == len(manifest)
        assert set(manifest) == set(names)
        assert not [
            path
            for path, digest in manifest.items()
            if hashlib.sha256(archive.read(path)).hexdigest() != digest
        ]
    present = {
        item.decode()
        for item in check_output(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "pyproject.toml",
                "uv.lock",
                "schemas/generated",
                "scripts",
                "src",
                "tests",
            ],
            cwd=ROOT,
        ).split(b"\0")
        if item
    }
    assert set(manifest) == present, sorted(set(manifest) ^ present)
    print(
        json.dumps(
            {
                "archive_sha256": observed,
                "archive_entries": len(manifest),
                "workspace_inventory": len(present),
                "mismatches": [],
            }
        )
    )
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--archive":
        raise SystemExit(archive_snapshot(Path(sys.argv[2])))
    if sys.argv[1] == "--diff":
        raise SystemExit(diff_product(Path(sys.argv[2]), Path(sys.argv[3])))
    if sys.argv[1] == "--schema-baseline":
        raise SystemExit(schema_baseline(Path(sys.argv[2]), Path(sys.argv[3])))
    raise SystemExit(main(Path(sys.argv[1]), sys.argv[2]))
