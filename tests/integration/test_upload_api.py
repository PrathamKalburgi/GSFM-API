import uuid
import zipfile

import pytest
from pyproj import Geod
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FeatureResult, UploadedFile
from app.repositories import file_repository as repo

GEOD = Geod(ellps="WGS84")
SQUARE = [(77.59, 12.97), (77.5904, 12.97), (77.5904, 12.9704), (77.59, 12.9704), (77.59, 12.97)]
SCHEMA = '<Schema name="parcel" id="parcel"><SimpleField name="owner" type="string"/></Schema>'


def upload(client, name, content, crs=None, headers=None):
    data = {"crs": crs} if crs is not None else None
    return client.post(
        "/api/files/",
        files={"file": (name, content, "application/octet-stream")},
        data=data,
        headers=headers,
    )


def row_counts(engine) -> tuple[int, int]:
    with Session(engine) as fresh:
        return (
            fresh.scalar(select(func.count()).select_from(UploadedFile)),
            fresh.scalar(select(func.count()).select_from(FeatureResult)),
        )


def assert_error(response, status, code, engine=None):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    for unsafe in ("Traceback", "/tmp/", "/home/", 'File "'):
        assert unsafe not in response.text
    if engine is not None:
        assert row_counts(engine) == (0, 0)  # a failed upload leaves nothing behind


@pytest.fixture
def parcels_kml(kml):
    return kml.write(
        kml.document(
            kml.folder(
                "Parcels",
                kml.placemark("North", kml.polygon(SQUARE), pid="p1"),
                kml.placemark("Gate", kml.point((77.6, 12.98)), pid="g1"),
                kml.placemark("Road", kml.line([(77.59, 12.97), (77.60, 12.98)]), pid="r1"),
            )
        ),
        "survey.kml",
    )


class TestSuccessfulUploads:
    def test_kml(self, client, engine, parcels_kml):
        response = upload(client, "survey.kml", parcels_kml.read_bytes())

        assert response.status_code == 201
        body = response.json()
        assert set(body) == {
            "id", "filename", "format", "feature_count", "crs",
            "measurement_crs", "status", "warnings", "created_at",
        }  # fmt: skip
        uuid.UUID(body["id"])
        assert body["filename"] == "survey.kml"
        assert body["format"] == "KML"
        assert body["feature_count"] == 3
        assert body["crs"] == "EPSG:4326"
        assert body["measurement_crs"] == "EPSG:32643"
        assert body["status"] == "COMPLETED"
        assert body["warnings"] == []
        assert body["created_at"].endswith("Z")
        assert response.headers["X-Request-ID"]
        assert row_counts(engine) == (1, 3)

        with Session(engine) as fresh:
            features, _ = repo.list_features(fresh, uuid.UUID(body["id"]), 10, 0)
        polygon, point, line = features
        assert polygon.source_feature_id == "p1"
        assert polygon.geometry_type == "Polygon"
        assert polygon.geometry_json["type"] == "Polygon"
        assert polygon.properties_json == {"Name": "North"}
        assert polygon.measurement_status == "MEASURED"
        assert 1900 < polygon.area_m2 < 1950  # about 1922 m2, not a number in degrees
        assert point.measurement_status == "NOT_REQUIRED"
        assert point.area_m2 is None and point.length_m is None
        assert line.length_m == pytest.approx(
            GEOD.line_length([77.59, 77.60], [12.97, 12.98]), rel=0.005
        )

    def test_shapefile_zip(self, client, engine, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf).read_bytes()
        response = upload(client, "C:\\Users\\me\\survey.zip", content)

        assert response.status_code == 201
        body = response.json()
        assert body["filename"] == "survey.zip"  # path parts are stripped
        assert body["format"] == "SHAPEFILE_ZIP"
        assert body["feature_count"] == 2
        assert body["crs"] == "EPSG:4326"
        assert body["measurement_crs"] == "EPSG:32643"
        assert body["warnings"] == []
        with Session(engine) as fresh:
            features, _ = repo.list_features(fresh, uuid.UUID(body["id"]), 10, 0)
        assert [f.source_layer for f in features] == ["survey", "survey"]
        assert [f.source_feature_id for f in features] == ["0", "1"]
        assert features[1].properties_json == {"owner": None, "zone": 4}

    def test_projected_shapefile_is_remeasured_to_the_same_area(
        self, client, engine, shapefile_zip, parcels_gdf
    ):
        in_degrees = upload(client, "a.zip", shapefile_zip(parcels_gdf).read_bytes()).json()
        projected = parcels_gdf.to_crs(3857)  # Web Mercator would overstate areas by ~5.9 %
        in_mercator = upload(client, "b.zip", shapefile_zip(projected).read_bytes()).json()

        assert in_mercator["crs"] == "EPSG:3857"
        assert in_mercator["measurement_crs"] == in_degrees["measurement_crs"] == "EPSG:32643"
        with Session(engine) as fresh:
            first, _ = repo.list_features(fresh, uuid.UUID(in_degrees["id"]), 10, 0)
            second, _ = repo.list_features(fresh, uuid.UUID(in_mercator["id"]), 10, 0)
        for a, b in zip(first, second, strict=True):
            assert b.area_m2 == pytest.approx(a.area_m2, rel=1e-4)

    def test_unmeasurable_features_add_one_counted_warning(self, client, engine, kml):
        mixed = (
            "<MultiGeometry>"
            + kml.point((77.60, 12.98))
            + kml.line([(77.59, 12.97), (77.60, 12.98)])
            + "</MultiGeometry>"
        )
        content = kml.document(
            kml.folder(
                "F",
                kml.placemark("ok", kml.polygon(SQUARE)),
                kml.placemark("mixed", mixed),
                kml.placemark("nothing"),
            )
        ).encode()
        response = upload(client, "mixed.kml", content)

        assert response.status_code == 201
        assert response.json()["warnings"] == [
            "2 features could not be measured (UNSUPPORTED or INVALID_GEOMETRY); "
            "see per-feature status."
        ]
        assert row_counts(engine) == (1, 3)  # the bad features did not stop the good one

    def test_file_with_only_empty_geometries_has_no_measurement_crs(self, client, kml):
        content = kml.document(kml.folder("F", kml.placemark("a"), kml.placemark("b"))).encode()
        body = upload(client, "empty.kml", content).json()
        assert body["measurement_crs"] is None
        assert any("No feature has usable geometry" in w for w in body["warnings"])

    def test_wide_extent_adds_a_warning(self, client, kml):
        content = kml.document(
            kml.folder("F", kml.placemark("a", kml.point((77.0, 12.0))),
                       kml.placemark("b", kml.point((84.0, 12.5))))
        ).encode()  # fmt: skip
        body = upload(client, "wide.kml", content).json()
        assert any("less accurate" in w for w in body["warnings"])

    def test_southern_hemisphere_file(self, client, kml):
        content = kml.document(
            kml.folder("F", kml.placemark("a", kml.polygon(
                [(151.2, -33.9), (151.201, -33.9), (151.201, -33.899), (151.2, -33.9)])))
        ).encode()  # fmt: skip
        assert upload(client, "syd.kml", content).json()["measurement_crs"] == "EPSG:32756"

    def test_non_ascii_filename_is_kept_for_display(self, client, parcels_kml):
        body = upload(client, "parcelles-été.kml", parcels_kml.read_bytes()).json()
        assert body["filename"] == "parcelles-été.kml"


class TestCrsField:
    def test_override_is_used_when_prj_is_missing(self, client, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf, with_prj=False).read_bytes()
        response = upload(client, "a.zip", content, crs="EPSG:4326")
        assert response.status_code == 201
        body = response.json()
        assert body["crs"] == "EPSG:4326"
        assert body["measurement_crs"] == "EPSG:32643"
        assert any("supplied by the client" in w for w in body["warnings"])

    def test_override_works_for_projected_coordinates(self, client, shapefile_zip, parcels_gdf):
        projected = parcels_gdf.to_crs(32643)
        content = shapefile_zip(projected, with_prj=False).read_bytes()
        body = upload(client, "a.zip", content, crs="EPSG:32643").json()
        assert body["crs"] == "EPSG:32643"

    def test_override_is_ignored_with_a_warning_when_prj_exists(
        self, client, shapefile_zip, parcels_gdf
    ):
        content = shapefile_zip(parcels_gdf).read_bytes()
        body = upload(client, "a.zip", content, crs="EPSG:32643").json()
        assert body["crs"] == "EPSG:4326"
        assert any("ignored" in w for w in body["warnings"])

    def test_invalid_override_is_rejected(self, client, engine, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf, with_prj=False).read_bytes()
        response = upload(client, "a.zip", content, crs="banana")
        assert_error(response, 422, "unknown_crs", engine)

    def test_override_does_not_apply_to_kml(self, client, parcels_kml):
        body = upload(client, "a.kml", parcels_kml.read_bytes(), crs="EPSG:32643").json()
        assert body["crs"] == "EPSG:4326"
        assert any("ignored" in w for w in body["warnings"])


class TestErrors:
    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("notes.txt", b"hello"),
            ("data.geojson", b"{}"),
            ("fake.zip", b"this is not a zip"),
            ("survey.kmz", b"PK\x03\x04"),
            ("fake.kml", b'{"type": "FeatureCollection"}'),
            ("renamed.kml", b"PK\x03\x04" + bytes(50)),
        ],
    )
    def test_unsupported_format(self, client, engine, name, content):
        assert_error(upload(client, name, content), 415, "unsupported_format", engine)

    def test_kmz_message_explains_the_alternative(self, client):
        message = upload(client, "a.kmz", b"PK\x03\x04").json()["error"]["message"]
        assert "KMZ" in message and ".kml" in message

    def test_file_too_large_by_content_length(self, make_client, engine):
        client = make_client(max_upload_bytes=1000)
        response = upload(client, "big.kml", b"<kml>" + bytes(2 * 1024 * 1024))
        assert_error(response, 413, "file_too_large", engine)

    def test_file_too_large_by_exact_check(self, make_client, engine):
        client = make_client(max_upload_bytes=1000)  # under the multipart slack, over the limit
        response = upload(client, "big.kml", b"<kml>" + b" " * 2000)
        assert_error(response, 413, "file_too_large", engine)

    def test_file_at_the_limit_is_accepted_by_the_size_check(self, make_client, parcels_kml):
        content = parcels_kml.read_bytes()
        client = make_client(max_upload_bytes=len(content))
        assert upload(client, "survey.kml", content).status_code == 201

    def test_archive_limit_exceeded(self, make_client, engine, shapefile_zip, parcels_gdf):
        client = make_client(max_zip_entries=2)
        content = shapefile_zip(parcels_gdf).read_bytes()
        assert_error(upload(client, "a.zip", content), 413, "archive_limit_exceeded", engine)

    def test_too_many_features_in_a_shapefile(
        self, make_client, engine, shapefile_zip, parcels_gdf
    ):
        client = make_client(max_features=1)
        content = shapefile_zip(parcels_gdf).read_bytes()
        assert_error(upload(client, "a.zip", content), 413, "too_many_features", engine)

    def test_too_many_features_in_a_kml(self, make_client, engine, parcels_kml):
        client = make_client(max_features=2)
        response = upload(client, "a.kml", parcels_kml.read_bytes())
        assert_error(response, 413, "too_many_features", engine)

    def test_invalid_archive(self, client, engine):
        response = upload(client, "a.zip", b"PK\x03\x04 garbage, not a real archive")
        assert_error(response, 400, "invalid_archive", engine)

    def test_missing_shapefile_component(self, client, engine, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf, drop_extensions=(".dbf",)).read_bytes()
        assert_error(upload(client, "a.zip", content), 400, "missing_shapefile_component", engine)

    def test_ambiguous_archive(self, client, engine, shapefile_zip, parcels_gdf, tmp_path):
        merged = tmp_path / "two.zip"
        with zipfile.ZipFile(merged, "w") as target:
            for stem in ("one", "two"):
                with zipfile.ZipFile(shapefile_zip(parcels_gdf, stem=stem)) as source:
                    for member in source.namelist():
                        target.writestr(member, source.read(member))
        assert_error(upload(client, "a.zip", merged.read_bytes()), 400, "ambiguous_archive", engine)

    def test_missing_source_crs(self, client, engine, shapefile_zip, parcels_gdf):
        content = shapefile_zip(parcels_gdf, with_prj=False).read_bytes()
        response = upload(client, "a.zip", content)
        assert_error(response, 422, "missing_source_crs", engine)
        assert "crs field" in response.json()["error"]["message"]

    def test_unreadable_kml(self, client, engine):
        content = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>'
        assert_error(upload(client, "a.kml", content), 422, "unreadable_data", engine)

    def test_unreadable_shapefile(self, client, engine, tmp_path):
        broken = tmp_path / "broken.zip"
        with zipfile.ZipFile(broken, "w") as archive:
            for extension in ("shp", "shx", "dbf"):
                archive.writestr(f"a.{extension}", b"garbage" * 30)
        assert_error(upload(client, "a.zip", broken.read_bytes()), 422, "unreadable_data", engine)

    def test_empty_kml(self, client, engine, kml):
        content = kml.document(kml.folder("Empty")).encode()
        assert_error(upload(client, "a.kml", content), 422, "empty_dataset", engine)

    def test_empty_shapefile(self, client, engine, shapefile_zip):
        import geopandas as gpd

        empty = gpd.GeoDataFrame({"a": []}, geometry=[], crs="EPSG:4326")
        response = upload(client, "a.zip", shapefile_zip(empty).read_bytes())
        assert_error(response, 422, "empty_dataset", engine)

    def test_extent_too_large(self, client, engine, kml):
        content = kml.document(
            kml.folder("F", kml.placemark("a", kml.point((60.0, 12.0))),
                       kml.placemark("b", kml.point((95.0, 13.0))))
        ).encode()  # fmt: skip
        assert_error(upload(client, "a.kml", content), 422, "extent_too_large", engine)

    def test_antimeridian_crossing_is_rejected(self, client, engine, kml):
        content = kml.document(
            kml.folder("F", kml.placemark("a", kml.point((179.9, -17.0))),
                       kml.placemark("b", kml.point((-179.9, -17.0))))
        ).encode()  # fmt: skip
        assert_error(upload(client, "a.kml", content), 422, "extent_too_large", engine)

    def test_missing_file_field(self, client):
        response = client.post("/api/files/", data={"crs": "EPSG:4326"})
        assert_error(response, 422, "validation_error")
        assert "file" in response.json()["error"]["message"]

    def test_unexpected_error_returns_safe_500(self, client, engine, parcels_kml, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("secret detail in /tmp/some/path with password=hunter2")

        monkeypatch.setattr("app.api.routes.files.process_upload", explode)
        response = upload(client, "a.kml", parcels_kml.read_bytes())
        assert_error(response, 500, "internal_error", engine)
        assert "hunter2" not in response.text and "secret" not in response.text

    def test_unknown_route_uses_the_envelope(self, client):
        assert_error(client.get("/nope"), 404, "not_found")

    def test_wrong_method_uses_the_envelope(self, client):
        response = client.put("/api/files/")
        assert_error(response, 405, "method_not_allowed")
        assert "POST" in response.headers["Allow"]


class TestRequestId:
    def test_client_request_id_is_echoed(self, client, parcels_kml):
        response = upload(
            client, "a.kml", parcels_kml.read_bytes(), headers={"X-Request-ID": "trace-123.abc"}
        )
        assert response.headers["X-Request-ID"] == "trace-123.abc"

    def test_request_id_appears_in_error_bodies(self, client):
        response = client.get("/nope", headers={"X-Request-ID": "trace-xyz"})
        assert response.json()["error"]["request_id"] == "trace-xyz"

    def test_unsafe_client_request_id_is_replaced(self, client):
        response = client.get("/nope", headers={"X-Request-ID": "bad id\twith spaces"})
        assert response.headers["X-Request-ID"] != "bad id\twith spaces"
        assert len(response.headers["X-Request-ID"]) == 32

    def test_each_request_gets_a_distinct_id(self, client):
        ids = {client.get("/nope").headers["X-Request-ID"] for _ in range(3)}
        assert len(ids) == 3


class TestRoutingAndDocs:
    def test_post_without_trailing_slash_redirects_with_307(self, client, parcels_kml):
        response = client.post(
            "/api/files",
            files={"file": ("a.kml", parcels_kml.read_bytes())},
            follow_redirects=False,
        )
        assert response.status_code == 307
        assert response.headers["location"].endswith("/api/files/")

    def test_openapi_documents_the_upload_route(self, client):
        operation = client.get("/openapi.json").json()["paths"]["/api/files/"]["post"]
        assert operation["tags"] == ["files"]
        assert operation["summary"]
        assert {"201", "413", "415", "422", "500"} <= set(operation["responses"])
        assert "file_too_large" in operation["responses"]["413"]["description"]
