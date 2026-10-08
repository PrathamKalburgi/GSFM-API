"""Security, resilience and logging checks (section 9: Security, Resilience, Database)."""

import io
import json
import logging
import os
import stat
import tempfile
import time
import zipfile

import pytest

from app.core.logging import JsonFormatter
from app.repositories import file_repository as repo
from tests.integration.test_upload_api import SQUARE, assert_error, row_counts, upload

SHAPEFILE_PARTS = ("shp", "shx", "dbf", "prj")


def zip_bytes(entries: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def shapefile_entries(stem="a") -> dict[str, bytes]:
    return {f"{stem}.{extension}": b"x" for extension in SHAPEFILE_PARTS}


@pytest.fixture
def one_kml(kml) -> bytes:
    return kml.document(
        kml.folder(
            "F",
            kml.placemark("a", kml.polygon(SQUARE)),
            kml.placemark("b", kml.point((77.6, 12.98))),
        )
    ).encode()


@pytest.fixture
def log_lines(client):
    """Parsed JSON log lines written after the app configured logging."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)


class TestArchiveAttacks:
    @pytest.mark.parametrize(
        "name",
        ["../evil.shp", "sub/../../evil.shp", "/etc/evil.shp", "C:/evil.shp", "..\\evil.shp"],
    )
    def test_path_traversal_names_are_rejected(self, client, engine, name):
        content = zip_bytes({**shapefile_entries(), name: b"x"})
        assert_error(upload(client, "a.zip", content), 400, "invalid_archive", engine)

    def test_traversal_names_never_write_outside_the_work_directory(self, client, tmp_path):
        sentinel = tmp_path / "evil.shp"
        upload(client, "a.zip", zip_bytes({**shapefile_entries(), f"../{sentinel.name}": b"x"}))
        assert not sentinel.exists()

    def test_zip_bomb_is_rejected_by_uncompressed_size(self, make_client, engine):
        client = make_client(max_uncompressed_bytes=100_000)
        bomb = zip_bytes({**shapefile_entries(), "a.shp": bytes(5_000_000)})
        assert len(bomb) < 20_000  # tiny on the wire, huge when expanded
        assert_error(upload(client, "a.zip", bomb), 413, "archive_limit_exceeded", engine)

    def test_entry_count_limit(self, make_client, engine):
        client = make_client(max_zip_entries=10)
        content = zip_bytes({f"file{i}.txt": b"x" for i in range(50)})
        assert_error(upload(client, "a.zip", content), 413, "archive_limit_exceeded", engine)

    def test_nested_archive_is_rejected(self, client, engine):
        content = zip_bytes({**shapefile_entries(), "inner.zip": zip_bytes({"x.txt": b"x"})})
        assert_error(upload(client, "a.zip", content), 400, "invalid_archive", engine)

    def test_symlink_entry_is_rejected(self, client, engine):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in shapefile_entries().items():
                archive.writestr(name, content)
            link = zipfile.ZipInfo("link.dbf")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "/etc/passwd")
        assert_error(upload(client, "a.zip", buffer.getvalue()), 400, "invalid_archive", engine)

    def test_macos_junk_does_not_cause_a_false_ambiguity(self, client, shapefile_zip, parcels_gdf):
        original = shapefile_zip(parcels_gdf).read_bytes()
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries.update({f"__MACOSX/._parcels.{ext}": b"junk" for ext in ("shp", "shx", "dbf")})
        entries["__MACOSX/"] = b""
        assert upload(client, "a.zip", zip_bytes(entries)).status_code == 201

    def test_shapefile_inside_a_subfolder_is_accepted(self, client, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf, prefix="export/2026/").read_bytes()
        assert upload(client, "a.zip", content).status_code == 201

    def test_entity_expansion_bomb_is_rejected_quickly(self, client, engine):
        entities = "".join(
            f'<!ENTITY {chr(98 + i)} "{(f"&{chr(97 + i)};") * 10}">' for i in range(8)
        )
        bomb = (
            f'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa">{entities}]>'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>'
            "<name>&i;</name></Folder></Document></kml>"
        )
        started = time.perf_counter()
        response = upload(client, "bomb.kml", bomb.encode())
        assert time.perf_counter() - started < 5
        assert response.status_code in (413, 422)
        assert response.status_code != 500
        assert row_counts(engine) == (0, 0)


class TestUploadSize:
    def test_oversized_chunked_upload_without_content_length(self, make_client, engine):
        client = make_client(max_upload_bytes=1000)
        boundary = "testboundary"

        def body():  # an iterator body is sent chunked, with no Content-Length header
            yield (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="a.kml"\r\n\r\n'
            ).encode()
            for _ in range(30):
                yield b"x" * 100_000
            yield f"\r\n--{boundary}--\r\n".encode()

        response = client.post(
            "/api/files/",
            content=body(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert_error(response, 413, "file_too_large", engine)

    def test_declared_oversize_is_refused_before_reading(self, make_client, engine):
        client = make_client(max_upload_bytes=1000)
        response = upload(client, "a.kml", b"<kml>" + bytes(3_000_000))
        assert_error(response, 413, "file_too_large", engine)


class TestFilenames:
    def test_traversal_in_the_client_filename_is_only_a_display_name(self, client, one_kml):
        body = upload(client, "../../etc/passwd.kml", one_kml).json()
        assert body["filename"] == "passwd.kml"

    def test_very_long_filenames_are_truncated(self, client, one_kml):
        body = upload(client, "a" * 600 + ".kml", one_kml).json()
        assert len(body["filename"]) == 255

    def test_empty_filename_is_not_a_file_part(self, client):
        # A multipart part without a filename is a plain form field, so `file` is missing.
        assert_error(upload(client, "", b"x"), 422, "validation_error")

    def test_name_without_an_extension_is_unsupported(self, client):
        assert_error(upload(client, "survey", b"x"), 415, "unsupported_format")


class TestTemporaryFiles:
    @pytest.fixture
    def scratch(self, tmp_path, monkeypatch):
        directory = tmp_path / "scratch"
        directory.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(directory))
        return directory

    def test_removed_after_success(self, client, scratch, shapefile_zip, parcels_gdf, one_kml):
        assert upload(client, "a.kml", one_kml).status_code == 201
        assert upload(client, "a.zip", shapefile_zip(parcels_gdf).read_bytes()).status_code == 201
        assert os.listdir(scratch) == []

    def test_removed_after_failures(self, client, scratch):
        upload(client, "a.zip", b"PK\x03\x04 corrupt archive")
        upload(client, "a.zip", zip_bytes({"readme.txt": b"no shapefile"}))
        upload(client, "a.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>')
        assert os.listdir(scratch) == []


class TestAtomicity:
    def test_failure_while_inserting_features_leaves_nothing(
        self, client, engine, one_kml, monkeypatch
    ):
        monkeypatch.setattr(repo, "INSERT_BATCH_SIZE", 1)
        real_insert, calls = repo.insert, 0

        def failing_insert(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # file row and the first feature are already in the transaction
                raise RuntimeError("simulated database failure")
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(repo, "insert", failing_insert)
        response = upload(client, "a.kml", one_kml)
        assert_error(response, 500, "internal_error", engine)
        assert "simulated" not in response.text

    def test_the_service_recovers_after_a_failed_upload(self, client, engine, one_kml, monkeypatch):
        monkeypatch.setattr(repo, "INSERT_BATCH_SIZE", 1)
        with monkeypatch.context() as patch:
            patch.setattr(repo, "insert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
            assert upload(client, "a.kml", one_kml).status_code == 500
        assert upload(client, "a.kml", one_kml).status_code == 201
        assert row_counts(engine) == (1, 2)


class TestResponsesStaySafe:
    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("a.zip", b"PK\x03\x04 broken"),
            ("a.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>'),
            ("a.zip", zip_bytes({"x.txt": b"x"})),
            ("a.txt", b"x"),
        ],
    )
    def test_errors_never_leak_paths_or_stack_traces(self, client, name, content):
        text = upload(client, name, content).text
        for leaked in (
            "Traceback",
            "/tmp",
            "/home",
            "site-packages",
            "pyogrio",
            ".py",
            "sqlalchemy",
        ):
            assert leaked not in text

    def test_connection_strings_never_appear_in_responses(self, make_client, one_kml, monkeypatch):
        secret = "postgresql://user:hunter2@db.example.supabase.co:5432/postgres"
        client = make_client(database_url=secret)
        monkeypatch.setattr(
            "app.api.routes.files.process_upload",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"cannot connect to {secret}")),
        )
        response = upload(client, "a.kml", one_kml)
        assert response.status_code == 500
        assert "hunter2" not in response.text and "supabase" not in response.text


class TestStructuredLogs:
    def test_one_json_line_per_request(self, client, log_lines):
        response = client.get("/health")
        lines = [line for line in log_lines() if line["logger"] == "app.access"]
        assert len(lines) == 1
        line = lines[0]
        assert line["request_id"] == response.headers["X-Request-ID"]
        assert (line["method"], line["path"], line["status"]) == ("GET", "/health", 200)
        assert isinstance(line["duration_ms"], float) and line["duration_ms"] >= 0
        assert {"timestamp", "level", "logger", "message"} <= set(line)

    def test_query_strings_are_not_logged(self, client, log_lines):
        client.get("/api/files/", params={"limit": 5})
        (line,) = [entry for entry in log_lines() if entry["logger"] == "app.access"]
        assert line["path"] == "/api/files/"
        assert "limit" not in json.dumps(line)

    def test_error_responses_are_logged_with_their_status(self, client, log_lines):
        client.get("/nope")
        (line,) = [entry for entry in log_lines() if entry["logger"] == "app.access"]
        assert line["status"] == 404

    def test_unexpected_errors_are_detailed_in_the_log_only(
        self, client, one_kml, log_lines, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("database exploded in /srv/app/secret.py")

        monkeypatch.setattr("app.api.routes.files.process_upload", explode)
        response = upload(client, "a.kml", one_kml)
        entries = log_lines()
        error_log = next(entry for entry in entries if entry["level"] == "ERROR")
        assert "database exploded" in error_log["exception"]
        assert error_log["request_id"] == response.headers["X-Request-ID"]
        assert "exploded" not in response.text and "secret.py" not in response.text
        access = next(entry for entry in entries if entry["logger"] == "app.access")
        assert access["status"] == 500
        assert access["request_id"] == response.headers["X-Request-ID"]

    def test_logs_are_valid_json_with_non_ascii_content(self, client, log_lines, one_kml):
        upload(client, "été.kml", one_kml)
        for line in log_lines():
            assert isinstance(line, dict)
