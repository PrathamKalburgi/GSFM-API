import stat
import zipfile
from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.archive import ArchiveLimits, extract_shapefile

LIMITS = ArchiveLimits()
SHAPEFILE_PARTS = ("shp", "shx", "dbf", "prj")


def build_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def shapefile_entries(stem="a", folder="") -> dict[str, bytes]:
    return {f"{folder}{stem}.{ext}": b"x" for ext in SHAPEFILE_PARTS}


def code_of(zip_path, tmp_path, limits=LIMITS) -> ErrorCode:
    with pytest.raises(AppError) as error:
        extract_shapefile(zip_path, tmp_path / "out", limits)
    return error.value.code


def test_valid_shapefile_is_copied_under_generated_names(shapefile_zip, parcels_gdf, tmp_path):
    zip_path = shapefile_zip(parcels_gdf)
    shp, encoding = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert shp == tmp_path / "out" / "data.shp"
    assert {p.name for p in (tmp_path / "out").iterdir()} >= {
        "data.shp",
        "data.shx",
        "data.dbf",
        "data.prj",
    }
    assert encoding == "utf-8"  # pyogrio writes a .cpg with UTF-8


def test_shapefile_inside_a_subfolder(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", shapefile_entries(folder="deep/folder/"))
    shp, _ = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert shp.exists()


def test_macos_junk_entries_are_ignored(tmp_path):
    entries = shapefile_entries()
    entries.update({"__MACOSX/._a.shp": b"junk", "__MACOSX/._a.dbf": b"junk", "._a.shx": b"junk"})
    entries["__MACOSX/"] = b""
    zip_path = build_zip(tmp_path / "z.zip", entries)
    shp, _ = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert shp.exists()


def test_extensions_match_case_insensitively(tmp_path):
    entries = {f"A.{ext.upper()}": b"x" for ext in SHAPEFILE_PARTS}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    shp, _ = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert shp.exists()


def test_prj_and_cpg_are_optional(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"a.shp": b"x", "a.shx": b"x", "a.dbf": b"x"})
    shp, encoding = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert shp.exists()
    assert encoding is None
    assert not (tmp_path / "out" / "data.prj").exists()


@pytest.mark.parametrize("missing", ["shp", "shx", "dbf"])
def test_missing_required_component(tmp_path, missing):
    entries = {k: v for k, v in shapefile_entries().items() if not k.endswith(missing)}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path) == ErrorCode.missing_shapefile_component


def test_zip_without_any_shapefile(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"readme.txt": b"hello"})
    assert code_of(zip_path, tmp_path) == ErrorCode.missing_shapefile_component


def test_two_shapefiles_are_ambiguous(tmp_path):
    entries = {**shapefile_entries("a"), **shapefile_entries("b")}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path) == ErrorCode.ambiguous_archive


def test_same_stem_in_two_folders_is_ambiguous(tmp_path):
    entries = {**shapefile_entries("a", "one/"), **shapefile_entries("a", "two/")}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path) == ErrorCode.ambiguous_archive


def test_malformed_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 this is not really a zip")
    assert code_of(bad, tmp_path) == ErrorCode.invalid_archive


@pytest.mark.parametrize(
    "name", ["../evil.shp", "a/../../evil.shp", "/abs/evil.shp", "C:/evil.shp", "..\\evil.shp"]
)
def test_traversal_names_are_rejected(tmp_path, name):
    entries = shapefile_entries()
    entries[name] = b"x"
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path) == ErrorCode.invalid_archive
    assert not (tmp_path / "evil.shp").exists()


def test_symlink_entries_are_rejected(tmp_path):
    zip_path = tmp_path / "z.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, content in shapefile_entries().items():
            archive.writestr(name, content)
        link = zipfile.ZipInfo("link.shp")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    assert code_of(zip_path, tmp_path) == ErrorCode.invalid_archive


def test_encrypted_entries_are_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"a.shp": b"x"})
    data = bytearray(zip_path.read_bytes())
    # zipfile cannot write encrypted entries, so set the "encrypted" bit (bit 0 of the
    # general-purpose flags) by hand in the local header and in the central directory.
    data[data.index(b"PK\x03\x04") + 6] |= 0x1
    data[data.index(b"PK\x01\x02") + 8] |= 0x1
    zip_path.write_bytes(bytes(data))
    assert code_of(zip_path, tmp_path) == ErrorCode.invalid_archive


@pytest.mark.parametrize("nested", ["inner.zip", "inner.KMZ", "data.tar.gz", "x.7z"])
def test_nested_archives_are_rejected(tmp_path, nested):
    entries = {**shapefile_entries(), nested: b"PK"}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path) == ErrorCode.invalid_archive


def test_too_many_entries(tmp_path):
    entries = {f"file{i}.txt": b"x" for i in range(10)}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert code_of(zip_path, tmp_path, ArchiveLimits(max_entries=5)) == (
        ErrorCode.archive_limit_exceeded
    )


def test_declared_uncompressed_size_over_limit(tmp_path):
    entries = {**shapefile_entries(), "a.shp": b"\0" * 1_000_000}  # zip-bomb style: compresses tiny
    zip_path = build_zip(tmp_path / "z.zip", entries)
    assert zip_path.stat().st_size < 10_000
    limits = ArchiveLimits(max_uncompressed_bytes=100_000)
    assert code_of(zip_path, tmp_path, limits) == ErrorCode.archive_limit_exceeded


def test_size_cap_is_enforced_while_copying_even_if_declared_size_is_small(tmp_path):
    """Declared sizes can lie, so the running total of bytes actually read must be capped."""
    entries = {**shapefile_entries(), "a.shp": b"\0" * 1_000_000}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    limits = ArchiveLimits(max_uncompressed_bytes=100_000)
    # Bypass the directory-size pre-check by applying only the copy step.
    from app.services import archive as archive_module

    with zipfile.ZipFile(zip_path) as zf:
        members = {
            ".shp": zf.getinfo("a.shp"),
            ".shx": zf.getinfo("a.shx"),
            ".dbf": zf.getinfo("a.dbf"),
        }
        (tmp_path / "out2").mkdir()
        with pytest.raises(AppError) as error:
            archive_module._copy_members(zf, members, tmp_path / "out2", limits)
    assert error.value.code == ErrorCode.archive_limit_exceeded


@pytest.mark.parametrize(
    ("cpg", "expected"),
    [(b"UTF-8", "utf-8"), (b"ISO-8859-1", "iso8859-1"), (b"1252", "cp1252"), (b"nonsense", None)],
)
def test_cpg_encoding(tmp_path, cpg, expected):
    entries = {**shapefile_entries(), "a.cpg": cpg}
    zip_path = build_zip(tmp_path / "z.zip", entries)
    _, encoding = extract_shapefile(zip_path, tmp_path / "out", LIMITS)
    assert encoding == expected
