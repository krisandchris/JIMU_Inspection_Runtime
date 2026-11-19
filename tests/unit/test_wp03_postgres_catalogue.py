from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import URL

from jimu_runtime.workflow.postgres import (
    MigrationError,
    _read_catalogue,
    create_postgres_engine,
)


def test_versions_use_exact_utf8_bytes_in_filename_order(tmp_path: Path) -> None:
    first = b"SELECT 'present';\r\n"
    second = "SELECT '车';\n".encode()
    (tmp_path / "002_second.sql").write_bytes(second)
    (tmp_path / "001_first.sql").write_bytes(first)
    (tmp_path / "README.md").write_text("comment", encoding="utf-8")
    migrations = _read_catalogue(tmp_path)
    assert tuple(item.version for item in migrations) == (1, 2)
    assert tuple(item.script_sha256 for item in migrations) == (
        sha256(first).hexdigest(),
        sha256(second).hexdigest(),
    )


@pytest.mark.parametrize(
    ("name", "contents", "code"),
    [
        ("000_zero.sql", b"SELECT 1;", "noncontiguous_migrations"),
        ("002_gap.sql", b"SELECT 1;", "noncontiguous_migrations"),
        ("001_Uppercase.sql", b"SELECT 1;", "invalid_migration_name"),
        ("001_encoding.sql", b"SELECT '\xff';", "invalid_migration_encoding"),
        ("001_empty.sql", b"\n", "invalid_migration_script"),
        (
            "001_delimiter.sql",
            b"SELECT '$jimu_migration$';",
            "invalid_migration_script",
        ),
        ("001_null.sql", b"SELECT '\x00';", "invalid_migration_script"),
    ],
)
def test_catalogue_rejects_invalid_migration(
    tmp_path: Path, name: str, contents: bytes, code: str
) -> None:
    (tmp_path / name).write_bytes(contents)
    with pytest.raises(MigrationError) as rejected:
        _read_catalogue(tmp_path)
    assert rejected.value.code == code


def test_duplicate_and_empty_catalogues_reject(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="migration_catalogue_empty"):
        _read_catalogue(tmp_path)
    for name in ("001_alpha.sql", "001_beta.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="noncontiguous_migrations"):
        _read_catalogue(tmp_path)


def test_workflow_engine_requires_explicit_psycopg_dialect() -> None:
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        create_postgres_engine(URL.create("postgresql"))
