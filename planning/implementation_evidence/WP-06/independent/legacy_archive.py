"""Verify the exact inherited WP-05 archive and all its scene members."""

import json
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile


def verify(directory: Path, expected_manifest: str) -> None:
    encoded = (directory / "product_sha256.json").read_bytes()
    manifest = sha256(encoded).hexdigest()
    if manifest != expected_manifest:
        raise ValueError("WP-05 product manifest digest mismatch")
    records = json.loads(encoded)
    archive = directory / "product.zip"
    archive_digest = sha256(archive.read_bytes()).hexdigest()
    seal = (directory / "product.zip.sha256").read_text().split()[0]
    if archive_digest != seal:
        raise ValueError("WP-05 archive digest mismatch")
    with ZipFile(archive) as product:
        names = product.namelist()
        if len(names) != len(set(names)) or set(names) != set(records):
            raise ValueError("WP-05 archive inventory differs from the pinned manifest")
        for name in names:
            if sha256(product.read(name)).hexdigest() != records[name]:
                raise ValueError(f"WP-05 archive member digest mismatch: {name}")
    print(
        json.dumps(
            {
                "manifest_sha256": manifest,
                "archive_sha256": archive_digest,
                "members": len(records),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    verify(Path(sys.argv[1]), sys.argv[2])
