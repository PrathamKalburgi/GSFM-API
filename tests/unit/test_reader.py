import math

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pyproj import CRS
from shapely.geometry import Point, box

from app.core.errors import AppError, ErrorCode
from app.services.archive import ArchiveLimits, extract_shapefile
from app.services.reader import detect_format, read_dataset, to_json_safe

SCHEMA = (
    '<Schema name="parcel" id="parcel"><SimpleField name="owner" type="string"/>'
    '<SimpleField name="zone" type="int"/></Schema>'
)
RING = [(77.59, 12.97, 5), (77.5904, 12.97, 5), (77.5904, 12.9704, 5), (77.59, 12.97, 5)]


def code_of(call) -> ErrorCode:
    with pytest.raises(AppError) as error:
        call()
    return error.value.code


class TestDetectFormat:
    def test_kml(self, kml):
        path = kml.write(kml.document(kml.folder("F", kml.placemark("a", kml.point((1, 2))))))
        assert detect_format(path, "survey.KML") == "KML"

    def test_zip(self, shapefile_zip, parcels_gdf):
        assert detect_format(shapefile_zip(parcels_gdf), "survey.zip") == "SHAPEFILE_ZIP"

    def test_kmz_is_rejected_with_a_clear_message(self, tmp_path):
        path = tmp_path / "x.kmz"
        path.write_bytes(b"PK\x03\x04")
        with pytest.raises(AppError) as error:
            detect_format(path, "x.kmz")
        assert error.value.code == ErrorCode.unsupported_format
        assert "KMZ" in error.value.message

    def test_zip_extension_with_other_content(self, tmp_path):
        path = tmp_path / "fake.zip"
        path.write_text("not a zip")
        assert code_of(lambda: detect_format(path, "fake.zip")) == ErrorCode.unsupported_format

    def test_kml_extension_with_other_content(self, tmp_path):
        path = tmp_path / "fake.kml"
        path.write_text('{"type": "FeatureCollection", "features": []}')
        assert code_of(lambda: detect_format(path, "fake.kml")) == ErrorCode.unsupported_format

    def test_kmz_renamed_to_kml(self, tmp_path):
        path = tmp_path / "x.kml"
        path.write_bytes(b"PK\x03\x04" + b"\0" * 100)
        assert code_of(lambda: detect_format(path, "x.kml")) == ErrorCode.unsupported_format

    @pytest.mark.parametrize("name", ["data.geojson", "data.csv", "noextension"])
    def test_other_extensions(self, tmp_path, name):
        path = tmp_path / "f"
        path.write_text("x")
        assert code_of(lambda: detect_format(path, name)) == ErrorCode.unsupported_format


class TestReadKml:
    def test_simple_kml(self, kml):
        content = kml.document(
            kml.folder(
                "Parcels",
                kml.placemark("North", kml.polygon(RING), pid="p1"),
                kml.placemark("Gate", kml.point((77.6, 12.98))),
            )
        )
        dataset = read_dataset(kml.write(content), "KML", encoding=None, max_features=100)
        assert dataset.format == "KML"
        assert dataset.crs == CRS.from_epsg(4326)
        assert [f.feature_index for f in dataset.features] == [0, 1]
        assert dataset.features[0].source_feature_id == "p1"
        assert dataset.features[1].source_feature_id is None
        assert dataset.features[0].geometry.geom_type == "Polygon"
        assert dataset.features[0].geometry.has_z  # source geometry is kept as read

    def test_several_folders_use_a_global_index_and_do_not_leak_properties(self, kml):
        extended = (
            '<ExtendedData><SchemaData schemaUrl="#parcel">'
            '<SimpleData name="owner">Asha</SimpleData><SimpleData name="zone">3</SimpleData>'
            "</SchemaData></ExtendedData>"
        )
        content = kml.document(
            kml.folder(
                "Parcels",
                kml.placemark("A", kml.point((77.59, 12.97)), pid="p1", extended=extended),
                kml.placemark("B", kml.point((77.60, 12.97)), pid="p2"),
            ),
            kml.folder(
                "Roads", kml.placemark("R", kml.line([(77.59, 12.97), (77.6, 12.98)]), pid="r1")
            ),
            schema=SCHEMA,
        )
        dataset = read_dataset(kml.write(content), "KML", encoding=None, max_features=100)
        features = dataset.features
        assert [f.feature_index for f in features] == [0, 1, 2]
        assert [f.source_layer for f in features] == ["Parcels", "Parcels", "Roads"]
        assert [f.source_feature_id for f in features] == ["p1", "p2", "r1"]
        # ExtendedData is kept, style fields and the id are dropped, nulls are omitted.
        assert features[0].properties == {"Name": "A", "owner": "Asha", "zone": 3}
        assert type(features[0].properties["zone"]) is int  # not 3.0, despite missing values
        assert features[1].properties == {"Name": "B"}
        assert features[2].properties == {"Name": "R"}  # no owner/zone leaked from Parcels

    def test_extended_data_without_schema(self, kml):
        extended = '<ExtendedData><Data name="crop"><value>rice</value></Data></ExtendedData>'
        content = kml.document(
            kml.folder("F", kml.placemark("A", kml.point((77.59, 12.97)), extended=extended))
        )
        (feature,) = read_dataset(
            kml.write(content), "KML", encoding=None, max_features=10
        ).features
        assert feature.properties["crop"] == "rice"

    def test_placemark_without_geometry_is_kept(self, kml):
        content = kml.document(kml.folder("F", kml.placemark("empty")))
        (feature,) = read_dataset(
            kml.write(content), "KML", encoding=None, max_features=10
        ).features
        assert feature.geometry is None
        assert feature.properties == {"Name": "empty"}

    def test_empty_kml_is_rejected(self, kml):
        path = kml.write(kml.document(kml.folder("Empty")))
        assert (
            code_of(lambda: read_dataset(path, "KML", encoding=None, max_features=10))
            == ErrorCode.empty_dataset
        )

    def test_malformed_kml_is_unreadable(self, kml):
        path = kml.write('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>')
        assert (
            code_of(lambda: read_dataset(path, "KML", encoding=None, max_features=10))
            == ErrorCode.unreadable_data
        )

    def test_feature_limit(self, kml):
        placemarks = [kml.placemark(f"p{i}", kml.point((77.0 + i / 1000, 12.0))) for i in range(5)]
        path = kml.write(kml.document(kml.folder("F", *placemarks)))
        assert (
            code_of(lambda: read_dataset(path, "KML", encoding=None, max_features=4))
            == ErrorCode.too_many_features
        )
        assert len(read_dataset(path, "KML", encoding=None, max_features=5).features) == 5


class TestReadShapefile:
    def read(self, zip_path, tmp_path, **kwargs):
        shp, encoding = extract_shapefile(zip_path, tmp_path / "out", ArchiveLimits())
        return read_dataset(shp, "SHAPEFILE_ZIP", encoding=encoding, max_features=100, **kwargs)

    def test_valid_shapefile(self, shapefile_zip, parcels_gdf, tmp_path):
        dataset = self.read(shapefile_zip(parcels_gdf), tmp_path, layer_name="parcels")
        assert dataset.format == "SHAPEFILE_ZIP"
        assert dataset.crs.to_epsg() == 4326
        assert [f.feature_index for f in dataset.features] == [0, 1]
        assert [f.source_feature_id for f in dataset.features] == ["0", "1"]
        assert {f.source_layer for f in dataset.features} == {"parcels"}
        assert dataset.features[0].properties == {"owner": "Asha", "zone": 3}
        # missing attribute values become null (key kept), not NaN
        assert dataset.features[1].properties == {"owner": None, "zone": 4}

    def test_projected_crs_is_preserved(self, shapefile_zip, parcels_gdf, tmp_path):
        projected = parcels_gdf.to_crs(32643)
        dataset = self.read(shapefile_zip(projected), tmp_path)
        assert dataset.crs.to_epsg() == 32643
        assert dataset.features[0].geometry.bounds[0] > 1000  # real metre coordinates

    def test_missing_prj_gives_no_crs(self, shapefile_zip, parcels_gdf, tmp_path):
        dataset = self.read(shapefile_zip(parcels_gdf, with_prj=False), tmp_path)
        assert dataset.crs is None
        assert len(dataset.features) == 2

    def test_default_layer_name_comes_from_the_dataset(self, shapefile_zip, parcels_gdf, tmp_path):
        dataset = self.read(shapefile_zip(parcels_gdf), tmp_path)
        assert dataset.features[0].source_layer == "data"

    def test_integer_attributes_stay_integers_when_some_rows_are_missing(
        self, shapefile_zip, tmp_path
    ):
        gdf = gpd.GeoDataFrame(
            {"count": pd.array([1, None, 3], dtype="Int64"), "ratio": [1.5, None, 2.0]},
            geometry=[Point(77.59, 12.97)] * 3,
            crs=4326,
        )
        dataset = self.read(shapefile_zip(gdf), tmp_path)
        first, second, third = (f.properties for f in dataset.features)
        assert first["count"] == 1 and type(first["count"]) is int
        assert second["count"] is None
        assert type(third["count"]) is int
        assert type(third["ratio"]) is float and third["ratio"] == 2.0  # real floats stay floats

    def test_empty_shapefile(self, shapefile_zip, tmp_path):
        empty = gpd.GeoDataFrame({"a": []}, geometry=[], crs="EPSG:4326")
        assert code_of(lambda: self.read(shapefile_zip(empty), tmp_path)) == ErrorCode.empty_dataset

    def test_non_ascii_attributes_use_cpg(self, shapefile_zip, tmp_path):
        gdf = gpd.GeoDataFrame({"name": ["Zürich Ünï"]}, geometry=[Point(8.5, 47.4)], crs=4326)
        dataset = self.read(shapefile_zip(gdf), tmp_path)
        assert dataset.features[0].properties["name"] == "Zürich Ünï"

    def test_feature_limit(self, shapefile_zip, parcels_gdf, tmp_path):
        shp, enc = extract_shapefile(shapefile_zip(parcels_gdf), tmp_path / "o", ArchiveLimits())
        assert (
            code_of(lambda: read_dataset(shp, "SHAPEFILE_ZIP", encoding=enc, max_features=1))
            == ErrorCode.too_many_features
        )

    def test_corrupt_dbf_is_unreadable(self, tmp_path):
        for ext in ("shp", "shx", "dbf"):
            (tmp_path / f"data.{ext}").write_bytes(b"garbage" * 20)
        assert (
            code_of(
                lambda: read_dataset(
                    tmp_path / "data.shp", "SHAPEFILE_ZIP", encoding=None, max_features=10
                )
            )
            == ErrorCode.unreadable_data
        )


class TestToJsonSafe:
    @pytest.mark.parametrize(
        "missing", [None, np.nan, float("nan"), pd.NA, pd.NaT, np.datetime64("NaT", "ns")]
    )
    def test_missing_values_become_none(self, missing):
        assert to_json_safe(missing) is None

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), np.float64("inf")])
    def test_infinity_becomes_none(self, bad):
        assert to_json_safe(bad) is None

    def test_numpy_scalars_become_python_numbers(self):
        for value, expected in [(np.int64(3), 3), (np.float32(1.5), 1.5), (np.bool_(True), True)]:
            result = to_json_safe(value)
            assert result == expected
            assert type(result) in (int, float, bool)

    def test_dates_become_iso_strings(self):
        assert to_json_safe(pd.Timestamp("2026-10-07T10:00:00")) == "2026-10-07T10:00:00"
        assert to_json_safe(np.datetime64("2026-10-07")) == "2026-10-07T00:00:00"

    def test_bytes_are_decoded_safely(self):
        assert to_json_safe(b"abc") == "abc"
        assert to_json_safe(b"\xff") == "\ufffd"

    def test_containers_are_converted_recursively(self):
        assert to_json_safe({"a": [np.nan, np.int64(1)]}) == {"a": [None, 1]}

    def test_plain_values_pass_through(self):
        assert to_json_safe("text") == "text"
        assert to_json_safe(True) is True
        assert to_json_safe(2.5) == 2.5
        assert math.isfinite(to_json_safe(0.0))

    def test_result_is_valid_strict_json(self):
        import json

        values = [np.nan, np.inf, np.int64(2), pd.NaT, "x"]
        json.dumps([to_json_safe(v) for v in values], allow_nan=False)


def test_box_helper_is_valid():  # guards the shared fixtures themselves
    assert box(0, 0, 1, 1).is_valid
