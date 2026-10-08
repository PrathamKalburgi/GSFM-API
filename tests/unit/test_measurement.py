import math

import numpy as np
import pytest
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.services.crs import ExtentLimits, select_measurement_crs
from app.services.measurement import MeasurementStatus, measure_features

GEOD = Geod(ellps="WGS84")  # independent oracle
WGS84 = CRS.from_epsg(4326)
TOLERANCE = 0.005  # 0.5 %
LIMITS = ExtentLimits()

# (name, lon, lat): mid-latitude north, south, high latitude, equator
LOCATIONS = [
    ("bengaluru", 77.59, 12.97),
    ("sydney", 151.20, -33.87),
    ("tromso", 18.95, 69.65),
    ("quito", -78.50, -0.20),
]


def geodesic_area(polygon) -> float:
    return abs(GEOD.geometry_area_perimeter(polygon)[0])


def reproject(geom, transformer):
    return shapely.transform(
        geom, lambda coords: np.column_stack(transformer.transform(coords[:, 0], coords[:, 1]))
    )


def measure_in_wgs84(geoms):
    measurement_crs, _ = select_measurement_crs(geoms, WGS84, LIMITS)
    return measure_features(geoms, WGS84, measurement_crs)


@pytest.mark.parametrize(("name", "lon", "lat"), LOCATIONS)
def test_polygon_area_matches_geodesic_oracle(name, lon, lat):
    polygon = box(lon, lat, lon + 0.02, lat + 0.02)
    (result,) = measure_in_wgs84([polygon])
    assert result.status == MeasurementStatus.MEASURED
    assert result.length_m is None
    assert math.isclose(result.area_m2, geodesic_area(polygon), rel_tol=TOLERANCE)
    # A naive calculation in degrees would give ~4e-4, nowhere near a real area.
    assert result.area_m2 > 1_000_000


@pytest.mark.parametrize(("name", "lon", "lat"), LOCATIONS)
def test_line_length_matches_geodesic_oracle(name, lon, lat):
    line = LineString([(lon, lat), (lon + 0.02, lat + 0.01), (lon + 0.03, lat + 0.03)])
    (result,) = measure_in_wgs84([line])
    assert result.status == MeasurementStatus.MEASURED
    assert result.area_m2 is None
    assert math.isclose(result.length_m, GEOD.geometry_length(line), rel_tol=TOLERANCE)


def test_multipolygon_and_multilinestring():
    first, second = box(77.59, 12.97, 77.60, 12.98), box(77.61, 12.97, 77.62, 12.98)
    lines = MultiLineString([[(77.59, 12.97), (77.60, 12.97)], [(77.59, 12.98), (77.60, 12.98)]])
    area, length = measure_in_wgs84([MultiPolygon([first, second]), lines])
    expected_area = geodesic_area(first) + geodesic_area(second)
    assert math.isclose(area.area_m2, expected_area, rel_tol=TOLERANCE)
    assert math.isclose(length.length_m, GEOD.geometry_length(lines), rel_tol=TOLERANCE)


def test_points_need_no_measurement():
    point, multipoint = measure_in_wgs84([Point(77.59, 12.97), MultiPoint([(77.59, 12.97)])])
    for result in (point, multipoint):
        assert result.status == MeasurementStatus.NOT_REQUIRED
        assert result.area_m2 is None
        assert result.length_m is None


def test_z_coordinates_are_ignored():
    flat = box(77.59, 12.97, 77.60, 12.98)
    raised = shapely.force_3d(flat, z=950.0)
    flat_result, raised_result = measure_in_wgs84([flat, raised])
    assert raised_result.area_m2 == pytest.approx(flat_result.area_m2)


def test_unsupported_geometry_types():
    collection = GeometryCollection(
        [Point(77.59, 12.97), LineString([(77.59, 12.97), (77.6, 12.98)])]
    )
    ring = LinearRing([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98)])
    collection_result, ring_result = measure_in_wgs84([collection, ring])
    for result in (collection_result, ring_result):
        assert result.status == MeasurementStatus.UNSUPPORTED
        assert result.message
        assert result.area_m2 is None and result.length_m is None


def test_empty_and_missing_geometry():
    results = measure_in_wgs84([None, Polygon(), Point(77.59, 12.97)])
    assert results[0].status == MeasurementStatus.INVALID_GEOMETRY
    assert results[0].message == "geometry is empty"
    assert results[1].status == MeasurementStatus.INVALID_GEOMETRY
    assert results[2].status == MeasurementStatus.NOT_REQUIRED


def test_invalid_geometry_is_reported_not_repaired():
    bowtie = Polygon([(77.59, 12.97), (77.60, 12.98), (77.60, 12.97), (77.59, 12.98)])
    (result,) = measure_in_wgs84([bowtie, Point(77.59, 12.97)])[:1]
    assert result.status == MeasurementStatus.INVALID_GEOMETRY
    assert "Self-intersection" in result.message
    assert result.area_m2 is None


def test_one_bad_feature_does_not_stop_the_others():
    good = box(77.59, 12.97, 77.60, 12.98)
    geoms = [None, good, GeometryCollection(), LineString([(77.59, 12.97), (77.6, 12.98)])]
    results = measure_in_wgs84(geoms)
    assert [r.status for r in results] == [
        MeasurementStatus.INVALID_GEOMETRY,
        MeasurementStatus.MEASURED,
        MeasurementStatus.INVALID_GEOMETRY,
        MeasurementStatus.MEASURED,
    ]


def test_output_matches_input_order_and_length():
    geoms = [Point(77.59, 12.97), box(77.59, 12.97, 77.60, 12.98), None]
    results = measure_in_wgs84(geoms)
    assert len(results) == len(geoms)
    assert results[1].status == MeasurementStatus.MEASURED


def test_all_null_geometries_need_no_measurement_crs():
    results = measure_features([None, None], WGS84, None)
    assert [r.status for r in results] == [MeasurementStatus.INVALID_GEOMETRY] * 2


def test_measurement_crs_required_when_something_is_measurable():
    with pytest.raises(ValueError):
        measure_features([box(0, 0, 1, 1)], WGS84, None)


def test_projected_input_is_reprojected_not_trusted():
    """The same polygon in EPSG:4326 and EPSG:3857 must give the same area."""
    polygon = box(77.59, 12.97, 77.5904, 12.9704)
    to_mercator = Transformer.from_crs(4326, 3857, always_xy=True)
    mercator_polygon = reproject(polygon, to_mercator)
    mercator = CRS.from_epsg(3857)

    wgs84_crs, _ = select_measurement_crs([polygon], WGS84, LIMITS)
    mercator_crs, _ = select_measurement_crs([mercator_polygon], mercator, LIMITS)
    assert wgs84_crs == mercator_crs

    (from_wgs84,) = measure_features([polygon], WGS84, wgs84_crs)
    (from_mercator,) = measure_features([mercator_polygon], mercator, mercator_crs)
    assert from_mercator.area_m2 == pytest.approx(from_wgs84.area_m2, rel=1e-6)
    assert math.isclose(from_mercator.area_m2, geodesic_area(polygon), rel_tol=TOLERANCE)
    # Measuring Mercator metres directly would overstate the area by about 6 %.
    assert mercator_polygon.area > geodesic_area(polygon) * 1.05


@pytest.mark.parametrize(("lon", "lat", "epsg"), [(0.0, 88.0, 32661), (0.0, -88.0, 32761)])
def test_polar_polygons_are_measured_with_ups(lon, lat, epsg):
    polygon = box(lon, lat, lon + 0.5, lat + 0.2)
    measurement_crs, _ = select_measurement_crs([polygon], WGS84, LIMITS)
    assert measurement_crs.to_epsg() == epsg
    (result,) = measure_features([polygon], WGS84, measurement_crs)
    # UPS has a 0.994 scale factor at the pole, so allow about 2 % here.
    assert math.isclose(result.area_m2, geodesic_area(polygon), rel_tol=0.02)
