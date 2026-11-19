"""Check the exact product and scene bytes before building an isolated wheel."""

import argparse
import json
import subprocess
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SAMPLE = "examples/nonmotor_vehicle_illegal_parking"
CORE = ("src", "tests", "scripts", "schemas/generated", "pyproject.toml", "uv.lock")


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def inventory(root: Path, *, include_examples: str = SAMPLE) -> set[str]:
    argv = [
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *CORE,
        include_examples,
    ]
    return {
        item.decode()
        for item in subprocess.check_output(argv, cwd=root).split(b"\0")
        if item
    }


def file_bytes(root: Path, name: str) -> bytes:
    path = root / name
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(root)
        or not path.is_file()
    ):
        raise ValueError(f"unexpected staged source path {name}")
    return path.read_bytes()


def snapshot(root: Path, output_dir: Path) -> None:
    output_dir.mkdir()
    first_set = inventory(root)
    stage = output_dir / "product"
    records = {}
    archive = output_dir / "product.zip"
    with ZipFile(archive, mode="x", compression=ZIP_DEFLATED) as zipped:
        for name in sorted(first_set):
            content = file_bytes(root, name)
            records[name] = digest(content)
            copied = stage / name
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(content)
            zipped.writestr(name, content)
    if first_set != inventory(root):
        raise ValueError(
            "working tree added or removed in-scope product files during staging"
        )
    for name, expected in records.items():
        assert digest(file_bytes(root, name)) == expected, (
            f"source changed during staging: {name}"
        )
    encoded = (json.dumps(records, sort_keys=True, indent=2) + "\n").encode()
    (output_dir / "product_sha256.json").write_bytes(encoded)
    print(
        json.dumps(
            {
                "source": str(stage),
                "files": len(records),
                "manifest_sha256": digest(encoded),
                "archive_sha256": digest(archive.read_bytes()),
            },
            sort_keys=True,
        )
    )


def verify(root: Path, manifest: Path, expected: str) -> None:
    encoded = manifest.read_bytes()
    if digest(encoded) != expected:
        raise ValueError("freeze manifest digest differs from the supplied digest")
    recorded = json.loads(encoded)
    sample_scope = (
        SAMPLE
        if any(
            name.startswith(f"{SAMPLE}/")
            and not name.startswith(f"{SAMPLE}/compilable/")
            for name in recorded
        )
        else f"{SAMPLE}/compilable"
    )
    in_scope = inventory(root, include_examples=sample_scope)
    assert in_scope == set(recorded), sorted(in_scope ^ set(recorded))
    for name, declared in recorded.items():
        assert digest(file_bytes(root, name)) == declared, (
            f"freeze source differs: {name}"
        )
    archive = manifest.with_name("product.zip")
    archive_sha = None
    if archive.is_file():
        archive_sha = digest(archive.read_bytes())
        seal = manifest.with_name("product.zip.sha256")
        if seal.is_file():
            assert archive_sha == seal.read_text().split()[0]
        with ZipFile(archive) as zipped:
            names = zipped.namelist()
            assert len(names) == len(set(names)) and set(names) == in_scope
            for name in names:
                assert digest(zipped.read(name)) == recorded[name], (
                    f"archive source differs: {name}"
                )
    print(
        json.dumps(
            {
                "manifest_sha256": expected,
                "archive_sha256": archive_sha,
                "product_files": len(recorded),
                "scene_files": sum(name.startswith(sample_scope) for name in recorded),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stage", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("digest", nargs="?")
    args = parser.parse_args()
    if args.mode == "stage":
        snapshot(args.root.resolve(), args.destination.resolve())
    else:
        assert args.digest
        verify(args.root.resolve(), args.destination.resolve(), args.digest)


if __name__ == "__main__":
    main()
