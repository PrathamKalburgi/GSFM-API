import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import FeatureResult, UploadedFile
from tests.integration.test_upload_api import SQUARE, assert_error, row_counts, upload

SCHEMA = (
    '<Schema name="parcel" id="parcel"><SimpleField name="owner" type="string"/>'
    '<SimpleField name="zone" type="int"/></Schema>'
)
OWNER = (
    '<ExtendedData><SchemaData schemaUrl="#parcel">'
    '<SimpleData name="owner">Asha</SimpleData></SchemaData></ExtendedData>'
)


@pytest.fixture
def multi_layer_kml(kml):
    """Two folders: 2 parcels (one with ExtendedData), 1 road, 1 point, 1 empty Placemark."""
    return kml.document(
        kml.folder(
            "Parcels",
            kml.placemark("North", kml.polygon(SQUARE), pid="p1", extended=OWNER),
            kml.placemark(
                "South",
                kml.polygon([(77.60, 12.98), (77.6006, 12.98), (77.6006, 12.9806), (77.60, 12.98)]),
                pid="p2",
            ),
        ),
        kml.folder(
            "Roads",
            kml.placemark("Main road", kml.line([(77.59, 12.97), (77.60, 12.98)]), pid="r1"),
            kml.placemark("Gate", kml.point((77.6, 12.98))),
            kml.placemark("Draft"),
        ),
        schema=SCHEMA,
    ).encode()


@pytest.fixture
def uploaded(client, multi_layer_kml) -> dict:
    response = upload(client, "survey.kml", multi_layer_kml)
    assert response.status_code == 201
    return response.json()


def measurements(client, file_id, **params):
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestFileDetail:
    def test_returns_the_same_shape_as_the_upload_response(self, client, uploaded):
        response = client.get(f"/api/files/{uploaded['id']}/")
        assert response.status_code == 200
        assert response.json() == uploaded
        assert response.headers["X-Request-ID"]

    def test_unknown_id(self, client):
        assert_error(client.get(f"/api/files/{uuid.uuid4()}/"), 404, "file_not_found")

    def test_malformed_id(self, client):
        response = client.get("/api/files/not-a-uuid/")
        assert_error(response, 422, "validation_error")
        assert "file_id" in response.json()["error"]["message"]

    def test_brief_field_names(self, client, uploaded):
        body = client.get(f"/api/files/{uploaded['id']}/").json()
        for name in ("id", "filename", "feature_count", "crs", "status"):
            assert name in body  # the names used by the company brief's example


class TestMeasurements:
    def test_response_shape(self, client, uploaded):
        body = measurements(client, uploaded["id"])
        assert set(body) == {
            "file_id", "crs", "measurement_crs", "total", "limit", "offset", "summary", "features",
        }  # fmt: skip
        assert body["file_id"] == uploaded["id"]
        assert body["crs"] == "EPSG:4326"
        assert body["measurement_crs"] == "EPSG:32643"
        assert (body["total"], body["limit"], body["offset"]) == (5, 100, 0)
        first = body["features"][0]
        assert set(first) == {
            "feature_index", "source_layer", "source_feature_id", "geometry_type",
            "crs", "geometry", "properties", "measurement",
        }  # fmt: skip
        assert set(first["measurement"]) == {"area_m2", "length_m", "status", "message"}

    def test_features_cover_the_brief_requirements(self, client, uploaded):
        features = measurements(client, uploaded["id"])["features"]
        north = features[0]
        assert north["feature_index"] == 0  # R3: index
        assert north["geometry_type"] == "Polygon"  # R3: geometry type
        assert north["geometry"]["type"] == "Polygon"  # R3: geometry
        assert north["geometry"]["coordinates"][0][0][:2] == [77.59, 12.97]  # source coordinates
        assert north["crs"] == "EPSG:4326"  # R3: CRS
        assert north["properties"] == {"Name": "North", "owner": "Asha"}  # R3: properties
        assert north["source_feature_id"] == "p1"
        assert north["measurement"]["status"] == "MEASURED"

    def test_global_feature_index_and_layers(self, client, uploaded):
        features = measurements(client, uploaded["id"])["features"]
        assert [f["feature_index"] for f in features] == [0, 1, 2, 3, 4]
        assert [f["source_layer"] for f in features] == [
            "Parcels", "Parcels", "Roads", "Roads", "Roads",
        ]  # fmt: skip

    def test_properties_do_not_leak_between_layers(self, client, uploaded):
        features = measurements(client, uploaded["id"])["features"]
        assert features[1]["properties"] == {"Name": "South"}  # no owner: null keys omitted
        assert features[2]["properties"] == {"Name": "Main road"}  # nothing from Parcels

    def test_each_geometry_type_is_measured_correctly(self, client, uploaded):
        by_name = {
            f["properties"]["Name"]: f for f in measurements(client, uploaded["id"])["features"]
        }
        polygon = by_name["North"]["measurement"]
        assert polygon["status"] == "MEASURED"
        assert polygon["area_m2"] == pytest.approx(1922.7, abs=25)
        assert polygon["length_m"] is None
        road = by_name["Main road"]["measurement"]
        assert road["status"] == "MEASURED" and road["length_m"] > 1000
        assert road["area_m2"] is None
        gate = by_name["Gate"]
        assert gate["measurement"]["status"] == "NOT_REQUIRED"
        assert gate["measurement"]["area_m2"] is None and gate["measurement"]["length_m"] is None
        draft = by_name["Draft"]
        assert draft["geometry"] is None and draft["geometry_type"] is None
        assert draft["measurement"]["status"] == "INVALID_GEOMETRY"
        assert draft["measurement"]["message"] == "geometry is empty"

    def test_values_are_rounded_to_two_decimals(self, client, uploaded):
        for feature in measurements(client, uploaded["id"])["features"]:
            for key in ("area_m2", "length_m"):
                value = feature["measurement"][key]
                assert value is None or value == round(value, 2)

    def test_summary_describes_the_whole_file(self, client, uploaded):
        summary = measurements(client, uploaded["id"])["summary"]
        assert summary["features"] == 5
        assert summary["by_status"] == {
            "MEASURED": 3, "NOT_REQUIRED": 1, "UNSUPPORTED": 0, "INVALID_GEOMETRY": 1,
        }  # fmt: skip
        assert summary["total_area_m2"] > 0 and summary["total_length_m"] > 0

    def test_summary_matches_the_sum_of_features_whatever_the_page(self, client, uploaded):
        everything = measurements(client, uploaded["id"], limit=500)
        area = sum(f["measurement"]["area_m2"] or 0 for f in everything["features"])
        length = sum(f["measurement"]["length_m"] or 0 for f in everything["features"])
        for params in (
            {"limit": 2, "offset": 0},
            {"limit": 2, "offset": 2},
            {"limit": 1, "offset": 4},
        ):
            page = measurements(client, uploaded["id"], **params)
            assert page["summary"] == everything["summary"]  # not computed from the page
        assert everything["summary"]["total_area_m2"] == pytest.approx(area, abs=0.02)
        assert everything["summary"]["total_length_m"] == pytest.approx(length, abs=0.02)

    def test_pagination(self, client, uploaded):
        page = measurements(client, uploaded["id"], limit=2, offset=1)
        assert (page["total"], page["limit"], page["offset"]) == (5, 2, 1)
        assert [f["feature_index"] for f in page["features"]] == [1, 2]
        past_the_end = measurements(client, uploaded["id"], limit=2, offset=50)
        assert past_the_end["features"] == []
        assert past_the_end["total"] == 5

    @pytest.mark.parametrize(
        "params",
        [
            {"limit": 0},
            {"limit": 501},
            {"limit": "x"},
            {"offset": -1},
            {"include_geometry": "maybe"},
        ],
    )
    def test_invalid_query_parameters(self, client, uploaded, params):
        response = client.get(f"/api/files/{uploaded['id']}/measurements/", params=params)
        assert_error(response, 422, "validation_error")

    def test_maximum_page_size_is_accepted(self, client, uploaded):
        assert measurements(client, uploaded["id"], limit=500)["limit"] == 500

    def test_include_geometry_false_nulls_geometry_only(self, client, uploaded):
        light = measurements(client, uploaded["id"], include_geometry="false")
        full = measurements(client, uploaded["id"])
        assert all(f["geometry"] is None for f in light["features"])
        assert any(f["geometry"] is not None for f in full["features"])
        for lighter, fuller in zip(light["features"], full["features"], strict=True):
            assert {**lighter, "geometry": None} == {**fuller, "geometry": None}
        assert light["summary"] == full["summary"]

    def test_unknown_file(self, client):
        response = client.get(f"/api/files/{uuid.uuid4()}/measurements/")
        assert_error(response, 404, "file_not_found")

    def test_shapefile_measurements(self, client, shapefile_zip, parcels_gdf):
        created = upload(client, "parcels.zip", shapefile_zip(parcels_gdf).read_bytes()).json()
        body = measurements(client, created["id"])
        assert body["crs"] == "EPSG:4326" and body["measurement_crs"] == "EPSG:32643"
        assert [f["source_layer"] for f in body["features"]] == ["parcels", "parcels"]
        assert body["features"][0]["properties"] == {"owner": "Asha", "zone": 3}
        assert body["features"][1]["properties"] == {"owner": None, "zone": 4}  # missing -> null
        assert body["summary"]["by_status"]["MEASURED"] == 2


class TestListFiles:
    def test_empty(self, client):
        response = client.get("/api/files/")
        assert response.status_code == 200
        assert response.json() == {"total": 0, "limit": 20, "offset": 0, "items": []}

    def test_most_recent_first_and_paginated(self, client, kml):
        ids = []
        for number in range(3):
            content = kml.document(
                kml.folder("F", kml.placemark(f"n{number}", kml.point((77.0 + number / 100, 12.9))))
            ).encode()
            ids.append(upload(client, f"file{number}.kml", content).json()["id"])

        body = client.get("/api/files/").json()
        assert body["total"] == 3
        assert [item["id"] for item in body["items"]] == ids[::-1]
        assert set(body["items"][0]) == {
            "id", "filename", "format", "feature_count", "crs",
            "measurement_crs", "status", "warnings", "created_at",
        }  # fmt: skip

        second = client.get("/api/files/", params={"limit": 2, "offset": 2}).json()
        assert (second["total"], second["limit"], second["offset"]) == (3, 2, 2)
        assert [item["id"] for item in second["items"]] == [ids[0]]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    def test_invalid_parameters(self, client, params):
        assert_error(client.get("/api/files/", params=params), 422, "validation_error")

    def test_maximum_page_size_is_accepted(self, client):
        assert client.get("/api/files/", params={"limit": 100}).status_code == 200


class TestDelete:
    def test_delete_removes_the_file_and_all_its_features(self, client, engine, uploaded, kml):
        other = upload(
            client,
            "other.kml",
            kml.document(kml.folder("F", kml.placemark("a", kml.point((77, 12))))).encode(),
        ).json()
        assert row_counts(engine) == (2, 6)

        response = client.delete(f"/api/files/{uploaded['id']}/")
        assert response.status_code == 204
        assert response.content == b""
        assert row_counts(engine) == (1, 1)  # cascade left no orphaned feature rows
        with Session(engine) as fresh:
            orphans = fresh.scalar(
                select(func.count())
                .select_from(FeatureResult)
                .where(FeatureResult.file_id == uuid.UUID(uploaded["id"]))
            )
            assert orphans == 0
            assert fresh.get(UploadedFile, uuid.UUID(other["id"])) is not None

        assert_error(client.get(f"/api/files/{uploaded['id']}/"), 404, "file_not_found")
        assert_error(
            client.get(f"/api/files/{uploaded['id']}/measurements/"), 404, "file_not_found"
        )

    def test_deleting_twice_returns_404(self, client, uploaded):
        assert client.delete(f"/api/files/{uploaded['id']}/").status_code == 204
        assert_error(client.delete(f"/api/files/{uploaded['id']}/"), 404, "file_not_found")

    def test_unknown_id(self, client):
        assert_error(client.delete(f"/api/files/{uuid.uuid4()}/"), 404, "file_not_found")


class TestHealth:
    def test_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}
        assert response.headers["X-Request-ID"]

    def test_database_down_returns_503_without_secrets(self, client):
        class BrokenSession:
            def execute(self, *args, **kwargs):
                raise OperationalError(
                    "SELECT 1", {}, Exception("could not connect to postgresql://u:hunter2@db/prod")
                )

        def broken():
            yield BrokenSession()

        client.app.dependency_overrides[get_session] = broken
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "database": "error"}
        assert "hunter2" not in response.text and "postgresql" not in response.text
        assert response.headers["X-Request-ID"]


class TestRoutes:
    @pytest.mark.parametrize("path", ["/api/files", "/api/files/abc/measurements"])
    def test_missing_trailing_slash_redirects(self, client, path):
        assert client.get(path, follow_redirects=False).status_code == 307

    def test_exact_brief_paths_exist(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths["/api/files/"]) == {"post", "get"}
        assert set(paths["/api/files/{file_id}/"]) == {"get", "delete"}
        assert set(paths["/api/files/{file_id}/measurements/"]) == {"get"}
        assert set(paths["/health"]) == {"get"}


class TestOpenApi:
    @pytest.fixture
    def schema(self, client):
        return client.get("/openapi.json").json()

    def test_docs_page_renders(self, client):
        assert client.get("/docs").status_code == 200

    def test_every_operation_has_a_tag_and_a_summary(self, schema):
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                assert operation["tags"], f"{method} {path}"
                assert operation["summary"], f"{method} {path}"

    def test_error_responses_are_documented_on_every_files_route(self, schema):
        expected = {
            ("/api/files/", "get"): {"422"},
            ("/api/files/{file_id}/", "get"): {"404", "422"},
            ("/api/files/{file_id}/", "delete"): {"204", "404", "422"},
            ("/api/files/{file_id}/measurements/", "get"): {"404", "422"},
            ("/api/files/", "post"): {"201", "413", "415", "422", "500"},
        }
        for (path, method), statuses in expected.items():
            assert statuses <= set(schema["paths"][path][method]["responses"]), (path, method)

    def test_error_responses_use_the_envelope_model(self, schema):
        response = schema["paths"]["/api/files/{file_id}/"]["get"]["responses"]["404"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")
        assert "file_not_found" in response["description"]
        assert (
            response["content"]["application/json"]["example"]["error"]["code"] == "file_not_found"
        )

    def test_response_examples_are_present(self, schema):
        schemas = schema["components"]["schemas"]
        assert schemas["FileSummary"]["examples"]
        assert schemas["MeasurementsResponse"]["examples"]

    def test_tags_have_descriptions(self, schema):
        assert {t["name"] for t in schema["tags"]} == {"files", "health"}
        assert all(t["description"] for t in schema["tags"])

    def test_query_parameters_are_documented(self, schema):
        parameters = schema["paths"]["/api/files/{file_id}/measurements/"]["get"]["parameters"]
        by_name = {p["name"]: p for p in parameters}
        assert by_name["limit"]["schema"]["maximum"] == 500
        assert by_name["include_geometry"]["schema"]["default"] is True
