"""Safe selection and extraction of a Shapefile from a ZIP archive.

Nothing is extracted wholesale. Only the Shapefile members are copied, under generated
names (`data.shp`, `data.dbf`, ...), so names inside the archive never reach the filesystem.
"""

import codecs
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.errors import AppError, ErrorCode

REQUIRED_EXTENSIONS = {".shp", ".shx", ".dbf"}
SHAPEFILE_EXTENSIONS = REQUIRED_EXTENSIONS | {".prj", ".cpg"}
NESTED_ARCHIVE_EXTENSIONS = {".zip", ".kmz", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar"}
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 200
    max_uncompressed_bytes: int = 200 * 1024 * 1024


def extract_shapefile(zip_path: Path, dest: Path, limits: ArchiveLimits) -> tuple[Path, str | None]:
    """Copy the single Shapefile in the ZIP to `dest`. Returns (.shp path, encoding or None)."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = _select_members(archive, limits)
            dest.mkdir(parents=True, exist_ok=True)
            _copy_members(archive, members, dest, limits)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as exc:
        raise AppError(
            ErrorCode.invalid_archive, "The ZIP archive is corrupt or unreadable."
        ) from exc
    return dest / "data.shp", _cpg_encoding(dest / "data.cpg")


def _select_members(archive: zipfile.ZipFile, limits: ArchiveLimits) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise AppError(ErrorCode.archive_limit_exceeded, "The ZIP archive has too many entries.")
    if sum(info.file_size for info in infos) > limits.max_uncompressed_bytes:
        raise AppError(
            ErrorCode.archive_limit_exceeded, "The ZIP archive is too large when uncompressed."
        )

    groups: dict[tuple[str, str], dict[str, zipfile.ZipInfo]] = {}
    for info in infos:
        name = info.filename.replace("\\", "/")
        _reject_unsafe(info, name)
        path = PurePosixPath(name)
        if info.is_dir() or _is_macos_junk(path):
            continue
        extension = path.suffix.lower()
        if extension in NESTED_ARCHIVE_EXTENSIONS:
            raise AppError(ErrorCode.invalid_archive, "Nested archives are not allowed.")
        if extension in SHAPEFILE_EXTENSIONS:
            group = groups.setdefault((str(path.parent).lower(), path.stem.lower()), {})
            if extension in group:
                raise AppError(ErrorCode.invalid_archive, "The ZIP contains duplicate entries.")
            group[extension] = info

    complete = [group for group in groups.values() if REQUIRED_EXTENSIONS <= group.keys()]
    if not complete:
        raise AppError(
            ErrorCode.missing_shapefile_component,
            "The ZIP must contain a Shapefile with .shp, .shx and .dbf files.",
        )
    if len(complete) > 1:
        raise AppError(
            ErrorCode.ambiguous_archive, "The ZIP contains more than one Shapefile; upload one."
        )
    return complete[0]


def _reject_unsafe(info: zipfile.ZipInfo, name: str) -> None:
    parts = PurePosixPath(name).parts
    traversal = name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts
    if traversal or info.flag_bits & 0x1 or stat.S_ISLNK(info.external_attr >> 16):
        raise AppError(
            ErrorCode.invalid_archive,
            "The ZIP contains unsafe entries (path traversal, symlinks or encryption).",
        )


def _is_macos_junk(path: PurePosixPath) -> bool:
    return "__MACOSX" in path.parts or path.name.startswith("._")


def _copy_members(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    dest: Path,
    limits: ArchiveLimits,
) -> None:
    # Declared sizes can lie, so the cap is enforced on the bytes actually read.
    total = 0
    for extension, info in members.items():
        with archive.open(info) as source, open(dest / f"data{extension}", "wb") as target:
            while chunk := source.read(CHUNK_SIZE):
                total += len(chunk)
                if total > limits.max_uncompressed_bytes:
                    raise AppError(
                        ErrorCode.archive_limit_exceeded,
                        "The ZIP archive is too large when uncompressed.",
                    )
                target.write(chunk)


def _cpg_encoding(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_bytes()[:64].decode("ascii", errors="ignore").strip()
    if not text:
        return None
    if text.isdigit():
        text = f"cp{text}"
    try:
        return codecs.lookup(text).name
    except LookupError:
        return None
