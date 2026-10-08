"""Shared fixtures. All test data is generated here; no binary fixtures are committed."""

import itertools
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from shapely.geometry import Point, box
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import Settings, normalize_database_url
from app.db.session import build_engine, get_session
from app.main import create_app
from app.models import FeatureResult, UploadedFile

ROOT = Path(__file__).resolve().parent.parent


def _coords(points) -> str:
    return " ".join(",".join(str(value) for value in point) for point in points)


@pytest.fixture
def kml(tmp_path):
    """Helpers that build KML strings and write them to a temporary file."""

    def placemark(name, geometry="", *, pid=None, extended=""):
        id_attribute = f' id="{pid}"' if pid else ""
        return f"<Placemark{id_attribute}><name>{name}</name>{extended}{geometry}</Placemark>"

    def folder(name, *placemarks):
        return f"<Folder><name>{name}</name>{''.join(placemarks)}</Folder>"

    def document(*folders, schema=""):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"{schema}{''.join(folders)}</Document></kml>"
        )

    def write(content, name="data.kml") -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return SimpleNamespace(
        placemark=placemark,
        folder=folder,
        document=document,
        write=write,
        point=lambda pt: f"<Point><coordinates>{_coords([pt])}</coordinates></Point>",
        line=lambda pts: f"<LineString><coordinates>{_coords(pts)}</coordinates></LineString>",
        polygon=lambda ring: (
            "<Polygon><outerBoundaryIs><LinearRing>"
            f"<coordinates>{_coords(ring)}</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon>"
        ),
    )


@pytest.fixture
def shapefile_zip(tmp_path):
    """Factory: write a GeoDataFrame as a Shapefile and zip it, with knobs for edge cases."""
    counter = itertools.count()

    def make(
        gdf: gpd.GeoDataFrame,
        *,
        stem="parcels",
        prefix="",
        with_prj=True,
        drop_extensions=(),
        extra_entries=None,
    ) -> Path:
        number = next(counter)
        work = tmp_path / f"build_{number}"
        work.mkdir()
        gdf.to_file(work / f"{stem}.shp", driver="ESRI Shapefile")
        zip_path = tmp_path / f"{stem}_{number}.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for file in sorted(work.iterdir()):
                extension = file.suffix.lower()
                if extension == ".prj" and not with_prj:
                    continue
                if extension in drop_extensions:
                    continue
                archive.write(file, arcname=f"{prefix}{file.name}")
            for name, content in (extra_entries or {}).items():
                archive.writestr(name, content)
        return zip_path

    return make


@pytest.fixture
def parcels_gdf() -> gpd.GeoDataFrame:
    """Two small polygons near Bengaluru in EPSG:4326."""
    return gpd.GeoDataFrame(
        {"owner": ["Asha", None], "zone": [3, 4]},
        geometry=[box(77.59, 12.97, 77.5904, 12.9704), box(77.60, 12.98, 77.6006, 12.9806)],
        crs="EPSG:4326",
    )


@pytest.fixture
def points_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(77.59, 12.97)], crs="EPSG:4326")


def _alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # configparser escaping
    return config


@pytest.fixture(scope="session")
def migrate():
    """Callable: migrate(url, "upgrade" | "downgrade", revision)."""

    def run(url: str, direction: str = "upgrade", revision: str = "head") -> None:
        getattr(command, direction)(_alembic_config(url), revision)

    return run


@pytest.fixture(scope="session")
def database_url(tmp_path_factory) -> str:
    """SQLite temp file by default; set TEST_DATABASE_URL to run the suite on PostgreSQL."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return normalize_database_url(url)
    return f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"


@pytest.fixture(scope="session")
def engine(database_url, migrate):
    migrate(database_url)  # the schema always comes from the real migration
    engine = build_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session
    with engine.begin() as connection:  # leave the tables empty for the next test
        connection.execute(delete(FeatureResult))
        connection.execute(delete(UploadedFile))


@pytest.fixture
def make_client(engine, session, monkeypatch):
    """Factory for a test client on the test database, with optional settings overrides."""

    def build(**overrides) -> TestClient:
        settings = Settings(_env_file=None, **overrides)
        for target in (
            "app.main.get_settings",
            "app.api.routes.files.get_settings",
            "app.services.upload_service.get_settings",
        ):
            monkeypatch.setattr(target, lambda: settings)
        app = create_app()

        def test_session():
            with Session(engine, expire_on_commit=False) as db_session:
                yield db_session

        app.dependency_overrides[get_session] = test_session
        return TestClient(app, raise_server_exceptions=False)

    return build


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
