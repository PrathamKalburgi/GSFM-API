# Geospatial File Measurement API: Project Plan

## Purpose

Build a clean, well-documented Python API that accepts KML and zipped Shapefiles, records their features, and returns reliable area and length measurements. The company brief defines the required behaviour; everything in section 1 marked "extra" is also part of the delivery. The result should be easy to run, easy to read, and demonstrably correct.

This plan is written to be handed to coding agents, so contracts and acceptance criteria are explicit.

### Rules for implementing agents

- Follow this plan. Do not add features, dependencies, or abstractions that it does not call for. If the plan is wrong or ambiguous, state the deviation and the reason in the commit message.
- Work in the order of section 11. Do not start a step until the previous step's "Done when" holds.
- Before finishing any step run the quality gate: `ruff check .`, `ruff format --check .`, `pytest`. All must pass.
- Never commit secrets, `.env`, database files, or uploaded data.
- Commit after each step with a message that says what the step delivers.

## 1. Requirements and scope

### Requirements stated in the company brief

| ID | Requirement | Planned implementation | Verified by |
| --- | --- | --- | --- |
| R1 | Use Django + DRF or FastAPI | FastAPI with typed request and response schemas | App starts; `/docs` renders |
| R2 | Accept a zipped Shapefile or KML at `POST /api/files/` | Multipart upload with format and archive validation | Upload tests for both formats |
| R3 | Per feature: ID/index, geometry type, geometry, CRS, properties | Each feature stored and returned with all five | Measurements response test |
| R4 | Polygon area, LineString length, Point needs none; unsupported geometry handled gracefully | Area in m², length in m; per-feature status instead of a crash | Measurement unit tests |
| R5 | Never measure in lat/lon degrees; transform to a projected CRS first | Reproject a copy to a metric CRS chosen from the dataset extent | Known-coordinate tests against an independent oracle (section 9) |
| R6 | `POST /api/files/`, `GET /api/files/{id}/`, `GET /api/files/{id}/measurements/` | Implemented exactly at these paths, including the trailing slash | Integration tests |
| R7 | README: setup (run locally), API (examples), architecture (structure, file-processing flow, measurement flow, CRS handling), design decisions (with alternatives considered) | README outline in section 12 | README review against section 12 |
| R8 | Public GitHub repo, link shared; README mentions learning and future scope | Publish checklist in section 11 | Repo opened logged-out |

The file-info example in the brief shows `id`, `filename`, `feature_count`, `crs`, `status`. The API returns exactly those names (plus extra fields), so a client written against the brief works unchanged.

### Extras (all part of the delivery)

- Paginated file listing and file deletion.
- Health check that verifies the database.
- Consistent error envelope with a documented error-code table.
- Request IDs (`X-Request-ID` header, logs, error bodies) and structured JSON logs.
- Whole-file `summary` block on the measurements response (totals and per-status counts).
- `include_geometry=false` option on the measurements route for lightweight pages.
- Optional `crs` upload field so a Shapefile with no `.prj` can still be processed when the client states its CRS.
- Warnings on upload when some features could not be measured.
- Alembic migrations, Docker image, Makefile, and CI that runs the full test suite against both SQLite and PostgreSQL.
- Polished OpenAPI docs: tags, descriptions, response examples, and documented error responses on every route.

### Out of scope

Authentication, a frontend, a job queue, PostGIS operations, KMZ, and a separate cloud file-storage layer. Uploaded source files are held in temporary local storage only while they are parsed; parsed geometry and results live in the database. These belong in the README's future-scope section.

## 2. Technology choices

| Concern | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11+ | Current, widely available |
| API | FastAPI + Pydantic, plain `def` route handlers | Typed, automatic OpenAPI docs. `def` (not `async def`) because parsing is blocking and CPU-bound; FastAPI runs `def` handlers in a thread pool so the event loop is not blocked |
| Vector reading | GeoPandas ≥ 1.0 with the **pyogrio** engine | pyogrio is the default engine from 1.0 and its wheels bundle GDAL, so no system GDAL install. Fiona is not used |
| Geometry and projections | Shapely 2 + pyproj | Validity checks, CRS handling, transformations, measurement |
| Database | SQLAlchemy 2.0 + psycopg 3 + Alembic | Ordinary transactions, reproducible schema |
| Database target | **SQLite file by default; Supabase PostgreSQL by setting `DATABASE_URL`** | A reviewer can clone and run with no cloud account (R7 requires a local run). Supabase remains the documented production target and CI exercises real PostgreSQL |
| Configuration | Pydantic settings + environment variables | Credentials stay out of Git |
| Quality | Ruff (lint + format), pytest, type hints | Consistent, readable code |
| Packaging | `pyproject.toml` with bounded dependency ranges and `[dev]` extras (pytest, httpx, ruff); commit a lock file | Reproducible installs |

### Spike results (verified October 2026; do not re-litigate)

A throwaway spike confirmed these facts. Baseline that worked: GeoPandas 1.2.0, pyogrio 0.13.0 (GDAL 3.12), Shapely 2.2.0, pyproj 3.8.0, pandas 3.

- pyogrio reads KML with the built-in `KML` driver. Each KML `<Folder>` becomes a layer (`pyogrio.list_layers`). Layers with no geometry type report `Unknown`.
- `pyogrio.read_info(path, layer=...)["features"]` gives a cheap feature count for KML and Shapefile.
- KML `ExtendedData` (both `SchemaData/SimpleData` and `Data/value`) arrives as ordinary columns, but those columns exist for every feature in the layer, so features without them get NaN.
- GDAL adds fixed fields to every KML feature: `id` (the Placemark id), `Name`, `description`, `timestamp`, `begin`, `end`, `altitudeMode`, `tessellate`, `extrude`, `visibility`, `drawOrder`, `icon`. Most are noise.
- KML geometries carry Z (`has_z` is true) when the source has altitudes. A Placemark with no geometry gives a null geometry. A `MultiGeometry` mixing types becomes a `GeometryCollection`.
- A Shapefile holds a single geometry type, and GDAL refuses to write mixed types into one. A Shapefile without `.prj` reads with `crs is None`. A `.prj` in ESRI WKT form resolves to the right EPSG code. A hand-made custom projection has no EPSG code and a name of `unknown`.
- Missing attribute values arrive as NaN or NaT, including for string columns.
- `GeoDataFrame.estimate_utm_crs()` returns the right UTM CRS (Bengaluru → EPSG:32643, Sydney → EPSG:32756) and also works when the input is projected. It **raises `RuntimeError` at polar latitudes** (about 85°) and **`ValueError` when all geometries are empty or null**. For an antimeridian-spanning dataset it silently returns a meaningless zone, so the extent guard must run first.
- Accuracy check on one small polygon near Bengaluru: geodesic area 1920.5 m², UTM area 1922.7 m² (+0.12%), the same polygon measured in EPSG:3857 2034.6 m² (+5.9%). This is why projected inputs are reprojected too.

## 3. Architecture and request flow

### Upload and processing

1. The files route streams the upload to a fresh temporary directory under a generated name, counting bytes as it writes and aborting at the limit (a `Content-Length` header alone can be wrong or absent).
2. Format detection uses extension **and** content: ZIP magic bytes for `.zip`, a successful KML open for `.kml`. A `.kmz` is rejected with a clear message.
3. For a ZIP, the archive helper picks out only the Shapefile members it needs and copies them under generated names (section 7). Nothing else is extracted.
4. The reader lists layers and, where the driver reports it, checks the feature count before loading geometry. Each layer is read separately; layers are never concatenated (see section 4).
5. The CRS step determines the source CRS (from the dataset, or from the optional `crs` field only when the dataset has none) and selects one measurement CRS for the whole file from the combined extent.
6. The measurement step assigns every feature a measurement or a status.
7. The repository inserts the file record and all feature rows in one transaction.
8. The temporary directory is removed on success and on failure (`TemporaryDirectory` context manager). The API returns `201` with the file summary.

A failed parse or database write must not leave a partially completed file visible. Errors are mapped at the API boundary to the standard error shape; details go to server logs only.

### Read and delete requests

Detail and list routes read file metadata. The measurements route returns stored features ordered by `feature_index`, paginated, plus a whole-file `summary` computed with SQL aggregates (not from the current page). Delete removes the file row; feature rows go with it through the foreign-key cascade, inside one transaction.

### Module responsibilities

- **Routes:** HTTP contract, pagination parameters, status codes, response schemas, OpenAPI metadata. No SQL, no parsing.
- **Upload service:** orchestrates steps 1 to 8 above.
- **Archive helper:** safe member selection and extraction, limit enforcement.
- **Reader:** format detection, layer reading, property and geometry normalization.
- **CRS module:** source CRS validation, measurement CRS selection, extent checks. Pure functions.
- **Measurement module:** per-geometry measurement and status. Pure functions, no FastAPI or database imports.
- **Repository:** all queries and the insert transaction.
- **Schemas:** API response shapes, separate from SQLAlchemy models.
- **Core:** settings, JSON logging, request-ID middleware, error classes and exception handlers.

## 4. Data model

### uploaded_files

| Field | Purpose |
| --- | --- |
| id | UUID (generated in Python), used in API paths |
| original_filename | Display name only: `Path(name).name`, truncated; never used for filesystem paths |
| format | `KML` or `SHAPEFILE_ZIP` |
| source_crs | CRS of the input (exposed as `crs` in the API) |
| measurement_crs | Projected CRS used for calculations; nullable if the file has no usable geometry |
| feature_count | Total parsed features across layers |
| status | `COMPLETED`. Failed uploads are never persisted, so this is constant for now but matches the brief |
| warnings | JSON list of non-fatal notes |
| created_at | UTC timestamp, indexed descending for the list route |

### feature_results

| Field | Purpose |
| --- | --- |
| id | Row identifier |
| file_id | Foreign key to `uploaded_files`, `ON DELETE CASCADE` |
| feature_index | **Zero-based index across the whole file** (layers in order, rows in order) |
| source_layer | Layer name (KML folders are separate layers; the Shapefile layer name otherwise) |
| source_feature_id | Source ID when available (rules below), otherwise null |
| geometry_type | Original geometry type; null for features with no geometry |
| geometry_json | Source-CRS geometry as JSON; null when absent |
| properties_json | Feature attributes as JSON |
| area_m2 | Polygon/MultiPolygon area, otherwise null |
| length_m | Line length, otherwise null |
| measurement_status | `MEASURED`, `NOT_REQUIRED`, `UNSUPPORTED`, `INVALID_GEOMETRY` |
| measurement_message | Safe explanation when there is no measurement |

Constraints and indexes: unique `(file_id, feature_index)`. Its index already serves ordered pagination, per-file aggregates, and the cascade, so no extra index is needed.

`feature_index` is global because a per-layer index would collide with the unique constraint as soon as a KML has two folders. Layers are processed individually, not concatenated, because concatenation adds every other layer's attribute columns to each feature as nulls.

### Property and ID normalization

- **Shapefile:** `source_feature_id` is the FID as a string. `properties` contains every DBF field; missing values become `null`.
- **KML:** `source_feature_id` is the Placemark `id` when present (and `id` is excluded from properties). Drop GDAL's fixed style fields `altitudeMode`, `tessellate`, `extrude`, `visibility`, `drawOrder`, `icon`. Keep `Name`, `description`, `timestamp`/`begin`/`end` and all ExtendedData fields, and **omit any key whose value is null**, so features do not carry other features' empty fields.
- All values become JSON-safe (section 7). Keys keep the names the driver reports (for example `Name`); input attributes are not renamed.

## 5. CRS and measurement rules

1. **Source CRS.** KML is WGS 84 (EPSG:4326). A Shapefile must carry a `.prj` that parses to a CRS. If it does not, use the optional `crs` upload field when given (validated with `CRS.from_user_input`; invalid → `unknown_crs`) and add a warning that the CRS was client-supplied; if absent, reject with `missing_source_crs`. Never guess from coordinate values. If `crs` is supplied but the dataset already has a CRS, ignore the field and add a warning saying so.
2. **CRS label.** Report `EPSG:<code>` when the CRS resolves to one; otherwise fall back to `crs.to_string()`.
3. **Always reproject; never trust the source projection for measurement.** This includes projected inputs: Web Mercator inflates area by roughly 1/cos²(latitude). Transforming to the chosen measurement CRS is a no-op when the source already matches it.
4. **Extent guard first.** Compute the combined bounds of all non-empty geometries in the source CRS (converted to degrees if projected). Defaults, configurable: warn when the extent exceeds about 6° in either direction; reject with `extent_too_large` (422) beyond about 30°. A file crossing the antimeridian spans close to 360° of longitude and is rejected by the same rule with no special-case code.
5. **Choosing the measurement CRS.** One CRS per file, so totals within a file are consistent. Call `GeoDataFrame.estimate_utm_crs()` on the combined extent. If it raises `RuntimeError` (polar latitudes), use Universal Polar Stereographic by hemisphere (EPSG:32661 north, EPSG:32761 south). If there are no finite bounds (every geometry null or empty), skip selection: `measurement_crs` is null and a warning is added.
6. **Axis order.** Use GeoPandas `to_crs`, or pyproj with `always_xy=True`.
7. **Measure a transformed copy**, forcing 2D (`shapely.force_2d`) since measurement is planar; keep the source geometry and CRS for reporting. Validity is checked on the **source** geometry.
8. **Per geometry type:**

   | Geometry | Result |
   | --- | --- |
   | Polygon, MultiPolygon | `area_m2`, status `MEASURED` |
   | LineString, MultiLineString | `length_m`, status `MEASURED` |
   | Point, MultiPoint | no measurement, `NOT_REQUIRED` |
   | GeometryCollection, LinearRing, other | `UNSUPPORTED` with message |
   | Null or empty geometry | `INVALID_GEOMETRY`, message "geometry is empty" |
   | Invalid geometry (self-intersection etc.) | `INVALID_GEOMETRY`, message from `shapely.validation.explain_validity`; not repaired |

   Other features in the same file are still processed. If any feature is `UNSUPPORTED` or `INVALID_GEOMETRY`, add one summary warning with the count.
9. **Rounding.** Store full-precision floats; round to 2 decimals when serialising.
10. A file with zero features is rejected with `empty_dataset` (422).
11. Return both `crs` and `measurement_crs`.

Accuracy statement for the README: UTM keeps length and area errors small within a zone (about 0.1% in the spike, a few tenths of a percent at the zone edge), which suits local and regional survey files. This is not a promise of survey-grade accuracy for arbitrary global datasets.

## 6. API contract

All endpoints use the `/api` prefix (except `/health`), JSON uses snake_case, and file IDs are UUIDs. Routes are declared **with the trailing slash exactly as in the brief**; otherwise FastAPI redirects between variants and a redirected `POST` is unreliable for clients. Every response carries an `X-Request-ID` header (the client's value if sent, otherwise generated). OpenAPI docs are at `/docs`, with tags, summaries, examples, and `responses=` entries for each documented error.

### POST /api/files/

`multipart/form-data`: required `file`; optional `crs` (for example `EPSG:32643`, used only when a Shapefile has no CRS). Accepts `.kml`, or `.zip` containing exactly one Shapefile dataset (`.shp`, `.shx`, `.dbf`, plus a usable `.prj` unless `crs` is given). Returns `201 Created`:

```json
{
  "id": "b74e4198-6c9f-4893-bbaa-2552ae9c20ef",
  "filename": "survey.kml",
  "format": "KML",
  "feature_count": 120,
  "crs": "EPSG:4326",
  "measurement_crs": "EPSG:32643",
  "status": "COMPLETED",
  "warnings": ["2 features could not be measured (UNSUPPORTED or INVALID_GEOMETRY); see per-feature status."],
  "created_at": "2026-10-07T10:00:00Z"
}
```

`crs` is the source CRS. There is no separate `source_crs` field in the API.

### GET /api/files/

Recent files first. `limit` (default 20, max 100) and `offset`. Returns `{"total", "limit", "offset", "items": [<file summary>, ...]}`.

### GET /api/files/{id}/

The same summary shape as the upload response. `404` for an unknown ID.

### GET /api/files/{id}/measurements/

Query parameters: `limit` (default 100, max 500), `offset` (default 0), `include_geometry` (default `true`; when `false`, `geometry` is `null` in every feature). Ordered by `feature_index`.

```json
{
  "file_id": "b74e4198-6c9f-4893-bbaa-2552ae9c20ef",
  "crs": "EPSG:4326",
  "measurement_crs": "EPSG:32643",
  "total": 120,
  "limit": 100,
  "offset": 0,
  "summary": {
    "features": 120,
    "by_status": { "MEASURED": 110, "NOT_REQUIRED": 8, "UNSUPPORTED": 1, "INVALID_GEOMETRY": 1 },
    "total_area_m2": 245072.18,
    "total_length_m": 18234.55
  },
  "features": [
    {
      "feature_index": 0,
      "source_layer": "Parcels",
      "source_feature_id": "p1",
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "geometry": { "type": "Polygon", "coordinates": [[[77.59, 12.97], [77.5904, 12.97], [77.5904, 12.9704], [77.59, 12.9704], [77.59, 12.97]]] },
      "properties": { "Name": "North parcel" },
      "measurement": {
        "area_m2": 1922.72,
        "length_m": null,
        "status": "MEASURED",
        "message": null
      }
    }
  ]
}
```

`summary` covers the whole file, not just the page. Coordinates are real source-CRS coordinates and each feature names the CRS they use (R3).

### DELETE /api/files/{id}/

`204 No Content` on success, `404` if the ID does not exist.

### GET /health

`200 {"status": "ok", "database": "ok"}` after a trivial `SELECT 1`; `503` if the database is unreachable. No connection strings or secrets in the body.

### Errors

One shape everywhere:

```json
{
  "error": {
    "code": "missing_source_crs",
    "message": "The Shapefile has no usable CRS information. Include a valid .prj file or pass a crs field.",
    "request_id": "..."
  }
}
```

FastAPI's validation errors (bad UUID, `limit` out of range, missing `file` field) and framework 404/405 responses use a different default shape, so register exception handlers for `RequestValidationError` and Starlette's `HTTPException` that emit this envelope.

| Code | HTTP | When |
| --- | --- | --- |
| `unsupported_format` | 415 | Not `.kml` or `.zip`; content does not match extension; KMZ |
| `file_too_large` | 413 | Upload byte limit exceeded |
| `archive_limit_exceeded` | 413 | Too many entries or uncompressed size too large |
| `too_many_features` | 413 | Feature-count limit exceeded |
| `invalid_archive` | 400 | Corrupt ZIP, unsafe entries, nested archives |
| `missing_shapefile_component` | 400 | `.shp`, `.shx` or `.dbf` missing |
| `ambiguous_archive` | 400 | More than one Shapefile in the ZIP |
| `missing_source_crs` | 422 | No usable CRS and no `crs` field |
| `unknown_crs` | 422 | CRS present or supplied but cannot be interpreted |
| `unreadable_data` | 422 | The driver cannot read the dataset |
| `empty_dataset` | 422 | Zero features |
| `extent_too_large` | 422 | Extent too wide for a single measurement projection |
| `validation_error` | 422 | Bad request parameters |
| `file_not_found` | 404 | Unknown file ID |
| `internal_error` | 500 | Anything unexpected; no stack trace, paths, or secrets in the message |

## 7. Validation and reliability

### Limits (configurable; defaults are a starting point)

| Setting | Default |
| --- | --- |
| Upload size | 50 MB |
| ZIP entries | 200 |
| Total uncompressed size | 200 MB |
| Feature count | 50,000 |

### Archive handling

- Do not extract the whole archive. List entries, select the Shapefile members (`.shp`, `.shx`, `.dbf`, `.prj`, optional `.cpg`), and copy each to a generated name in a fresh temporary directory (`data.shp`, `data.dbf`, ...). Names and paths inside the archive are never used for the filesystem, which removes path traversal rather than filtering for it.
- Reject symlinks, encrypted entries, and nested archives. Match extensions case-insensitively.
- Allow Shapefiles inside a folder in the ZIP. Ignore `__MACOSX/` and `._*` entries that macOS adds, or they cause false "ambiguous archive" errors.
- Declared uncompressed sizes can lie, so enforce the size cap while copying (chunked read with a running total), not only from the ZIP directory.
- Group members by stem. Exactly one complete group is required; none is `missing_shapefile_component`, several is `ambiguous_archive`.
- If a `.cpg` file is present, pass its encoding to the reader so non-UTF-8 attribute text is not garbled.

### Data handling

- Validate content, not just extension or client MIME type.
- Convert attribute values to JSON-safe values: NumPy scalars to Python numbers, dates and timestamps to ISO strings, bytes safely handled, and **`NaN`, `NaT`, pandas `NA` and infinity to `null`**. PostgreSQL JSON/JSONB rejects the `NaN` token Python's serializer emits, so unhandled missing values would fail inserts on PostgreSQL while passing on SQLite.
- Insert the file row and all features in one transaction, in batches using SQLAlchemy's executemany insert. Roll back everything on any failure.
- Remove temporary files on both success and failure.
- Cap page size.

### Database notes

- JSON columns use `JSON().with_variant(JSONB(), "postgresql")` so one model works on both databases.
- SQLite needs `PRAGMA foreign_keys=ON` on each connection (connect event) or `ON DELETE CASCADE` silently does nothing. The default SQLite path is under a git-ignored `data/` directory, created on startup.
- Supabase hands out `postgresql://...` URLs; SQLAlchemy would then look for psycopg2. Rewrite the scheme to `postgresql+psycopg://` in settings code and require `sslmode=require` for remote hosts.
- Use the session pooler or direct connection for the API and for Alembic. If the transaction pooler (port 6543) is used, pass `prepare_threshold=None` in psycopg `connect_args`. The direct host may be IPv6-only on some plans; if connections fail, use the session pooler string from the dashboard (see https://supabase.com/docs/guides/database/connecting-to-postgres).
- **Enable Row Level Security on both tables in the PostgreSQL branch of the migration, with no policies.** Tables created by SQL migrations in the `public` schema are reachable through Supabase's auto-generated Data API with the public anon key unless RLS is on. The API connects as the table-owning role, which bypasses RLS, so it is unaffected.

### Operations

- Credentials only in server environment variables; `.env`, uploads, and databases are git-ignored; `.env.example` defaults to SQLite.
- JSON log lines via stdlib `logging` with a custom formatter (no extra dependency). One line per request: `request_id`, method, path, status, duration. Log server-side error detail; return only safe messages.

## 8. Code organization and readability

A small layered layout that makes the request path easy to follow. Keep parsing and CRS work out of route handlers; keep modules small and focused; avoid abstractions with a single trivial implementation (the repository is justified only because it keeps SQL and the transaction out of routes, so keep it thin). Descriptive names, type hints, comments only for reasoning that is not obvious from the code.

```text
geospatial-file-measurement/
├── .github/workflows/ci.yml
├── app/
│   ├── main.py
│   ├── api/
│   │   └── routes/
│   │       ├── files.py
│   │       └── health.py
│   ├── core/
│   │   ├── config.py
│   │   ├── errors.py          # error classes + exception handlers
│   │   ├── logging.py         # JSON formatter
│   │   └── middleware.py      # request ID + access log
│   ├── db/
│   │   ├── base.py
│   │   └── session.py
│   ├── models/
│   │   ├── uploaded_file.py
│   │   └── feature_result.py
│   ├── repositories/
│   │   └── file_repository.py
│   ├── schemas/
│   │   ├── file.py
│   │   ├── measurement.py
│   │   └── error.py
│   └── services/
│       ├── upload_service.py
│       ├── archive.py         # safe ZIP member selection and extraction
│       ├── reader.py          # format detection, layers, normalization
│       ├── crs.py             # pure functions
│       └── measurement.py     # pure functions
├── alembic/
│   └── versions/
├── samples/                   # small synthetic KML + zipped Shapefile for README examples
├── tests/
│   ├── conftest.py            # builds fixtures programmatically; DB from TEST_DATABASE_URL
│   ├── unit/
│   └── integration/
├── .dockerignore
├── .env.example               # DATABASE_URL defaults to local SQLite
├── .gitignore
├── Dockerfile
├── Makefile                   # install, migrate, run, test, lint, format
├── alembic.ini
├── pyproject.toml
├── README.md
└── PROJECT_PLAN.md
```

### Module contracts

Keep these signatures (names may be refined, shapes should not change) so the pieces compose and can be tested in isolation.

```python
# services/reader.py
@dataclass(frozen=True)
class FeatureRecord:
    feature_index: int
    source_layer: str
    source_feature_id: str | None
    geometry: BaseGeometry | None  # source CRS, as read
    properties: dict[str, Any]  # normalized per section 4, JSON-safe


@dataclass(frozen=True)
class Dataset:
    format: Literal["KML", "SHAPEFILE_ZIP"]
    crs: CRS | None  # None only for a Shapefile with no usable CRS
    features: list[FeatureRecord]


def read_dataset(path: Path, fmt: str, *, encoding: str | None, max_features: int) -> Dataset: ...


# services/crs.py
def crs_label(crs: CRS) -> str: ...
def resolve_source_crs(
    dataset_crs: CRS | None, override: str | None
) -> tuple[CRS, list[str]]: ...  # (crs, warnings)
def select_measurement_crs(
    geoms: Sequence[BaseGeometry | None], source_crs: CRS, limits: ExtentLimits
) -> tuple[CRS | None, list[str]]: ...  # raises AppError(extent_too_large)


# services/measurement.py
class MeasurementStatus(StrEnum):
    MEASURED
    NOT_REQUIRED
    UNSUPPORTED
    INVALID_GEOMETRY


@dataclass(frozen=True)
class Measurement:
    area_m2: float | None
    length_m: float | None
    status: MeasurementStatus
    message: str | None


def measure_features(
    geoms: Sequence[BaseGeometry | None], source_crs: CRS, measurement_crs: CRS | None
) -> list[Measurement]: ...  # same order and length as input


# services/archive.py
def extract_shapefile(
    zip_path: Path, dest: Path, limits: ArchiveLimits
) -> tuple[Path, str | None]: ...  # (.shp path, encoding)


# services/upload_service.py
def process_upload(
    session: Session, upload: Path, filename: str, crs_override: str | None
) -> UploadedFile: ...


# repositories/file_repository.py
def create_file_with_features(session, file, features) -> None: ...  # single transaction
def get_file(session, file_id) -> UploadedFile | None: ...
def list_files(session, limit, offset) -> tuple[list[UploadedFile], int]: ...
def list_features(session, file_id, limit, offset) -> tuple[list[FeatureResult], int]: ...
def get_summary(session, file_id) -> MeasurementSummary: ...  # SQL aggregates over the whole file
def delete_file(session, file_id) -> bool: ...
```

`crs.py` and `measurement.py` must not import FastAPI, SQLAlchemy, or anything from `app/api` or `app/db`.

Fixtures are generated in `conftest.py` (handwritten KML strings; Shapefiles written with GeoPandas into a temporary ZIP) so no opaque binary test data is committed. The only committed data files are the small `samples/` files used by the README's `curl` examples.

## 9. Verification plan

Use small synthetic files with known coordinates. **Use an independent oracle for numbers:** for geometry in EPSG:4326, compare the API result with `pyproj.Geod` geodesic area/length (`geometry_area_perimeter`, `geometry_length`) and assert agreement within about 0.5%. This verifies the projection choice and axis order without hand-computed constants, and a naive degree-based calculation would fail it badly.

| Area | Verify |
| --- | --- |
| Measurements | Polygon and MultiPolygon area, LineString and MultiLineString length, within tolerance of the geodesic oracle; Point is `NOT_REQUIRED` with null measurements; Z coordinates ignored |
| CRS | Same polygon supplied in EPSG:4326 and EPSG:3857 gives the same area within tolerance (the spike's 5.9% gap must not appear); `crs` and `measurement_crs` returned; Shapefile without `.prj` rejected; `crs` override accepted when `.prj` is missing, ignored with a warning when present, rejected when invalid; southern-hemisphere and polar cases pick a valid CRS; antimeridian-spanning and very wide extents are rejected; all-null geometries give null `measurement_crs` |
| Reading | Valid KML; KML with several folders (global `feature_index`, correct layer names, no cross-layer null properties, style fields dropped, ExtendedData kept, Placemark `id` as `source_feature_id`); valid Shapefile ZIP; Shapefile inside a subfolder; ZIP with macOS junk entries; missing sidecars; malformed ZIP; ZIP with two Shapefiles; empty input; KMZ rejected |
| Resilience | Invalid polygon, empty geometry, GeometryCollection, and a feature with no geometry do not stop other features; the warning count is correct; `NaN` attributes stored as null |
| API | Upload, list, detail, pagination bounds, `include_geometry=false`, summary totals match the full file regardless of page, delete (cascade leaves no feature rows), health (including DB-down `503`), unknown ID, validation errors in the standard envelope, `X-Request-ID` echoed, trailing-slash routes |
| Database | Migration applies on SQLite and PostgreSQL; forcing a failure mid-insert (monkeypatch) leaves no file or feature rows; attributes with missing values insert on PostgreSQL |
| Security | ZIP traversal names, oversized upload, zip-bomb sizes, and entry-count limits rejected; no credentials, paths, or stack traces in responses |
| Setup | Fresh clone on SQLite: install, migrate, run, upload the sample files with the README's commands. Docker image builds and serves `/health`. One manual run against a real Supabase project (migrate, upload, list, delete), noted in the README |

Tests run on SQLite by default so they need no network or credentials; `TEST_DATABASE_URL` points the same suite at PostgreSQL.

## 10. Delivery tooling

- **Dockerfile:** `python:3.12-slim`, installs the package from `pyproject.toml`, runs as a non-root user, applies migrations then starts uvicorn. Works without system GDAL because the wheels bundle it.
- **Makefile:** `install`, `migrate`, `run`, `test`, `lint`, `format`. Each target is one command that the README also documents.
- **CI (`.github/workflows/ci.yml`):** on push and pull request, run `ruff check`, `ruff format --check`, and `pytest`, as a matrix over `sqlite` and a PostgreSQL service container (via `TEST_DATABASE_URL`). Add the badge to the README.

## 11. Implementation order and completion checklist

0. **Dependency spike:** already done (section 2). Pin the baseline versions in `pyproject.toml`, generate the lock file, confirm a clean install works.
   *Done when:* a fresh virtual environment installs and imports the stack.
1. **Geospatial core, no web or database:** `archive.py`, `reader.py`, `crs.py`, `measurement.py` and their unit tests, using the contracts in section 8.
   *Done when:* unit tests cover every row of section 5's rules and pass, including the oracle comparisons.
2. **Scaffold and persistence:** FastAPI app, settings, logging and request-ID middleware, DB session, models, Alembic migration (SQLite and PostgreSQL branches, including RLS), repository.
   *Done when:* migration applies on SQLite and PostgreSQL; repository tests (including rollback and cascade delete) pass on both.
3. **Upload path:** upload service, `POST /api/files/`, error envelope and handlers.
   *Done when:* integration tests for both formats, every error code in section 6, and the `crs` override pass.
4. **Read routes:** detail, measurements (pagination, summary, `include_geometry`), list, delete, health.
   *Done when:* integration tests for each route pass; `/docs` renders with tags, examples, and error responses.
5. **Hardening:** archive-attack and limit tests, rollback test, log format check.
   *Done when:* the security rows of section 9 pass.
6. **Delivery tooling and docs:** Dockerfile, Makefile, CI, `samples/`, README (section 12).
   *Done when:* CI is green on both databases; Docker image serves `/health`; a fresh clone follows the README on SQLite and the sample `curl` commands produce the documented output; the Supabase run is recorded.
7. **Publish:** scan the working tree **and Git history** for secrets (a `.env` committed once and removed later is still in history); create the public GitHub repository; open the link logged out to confirm it is public; share it.

The project is complete when a fresh clone runs the API with no external accounts; valid files persist metadata and features; measurements come back in metric units after projection; invalid input gets clear errors; every extra in section 1 works; tests pass in CI on both databases; the README covers section 12; and the public repository contains no secrets or private uploads.

## 12. README outline (maps to brief items 7 and 8)

1. **Overview and quick start:** what it does; install, migrate, run via `make` or the raw commands, SQLite default; Docker alternative; CI badge.
2. **Using Supabase:** setting `DATABASE_URL`, URL scheme and SSL, which connection mode to use, the RLS note.
3. **API:** each endpoint with a `curl` example against the files in `samples/` and the real response; the `crs` upload field, `include_geometry`, and the `summary` block; error shape and code table.
4. **Architecture:** application structure; file-processing flow; measurement calculation flow; CRS handling (the four items the brief names).
5. **Design decisions and alternatives considered:** projected UTM vs geodesic calculation (`pyproj.Geod`) vs an equal-area CRS; measuring in Python vs PostGIS; synchronous processing vs a job queue; SQLite default with a Supabase target vs Supabase only; pyogrio vs Fiona vs `fastkml`/`pyshp`; reject missing CRS vs guess vs client-supplied override; global `feature_index` vs per-layer.
6. **Accuracy and limitations:** the accuracy statement from section 5 with the spike's numbers; unsupported geometry behaviour; limits.
7. **Learning:** what was learned building it (for example CRS pitfalls such as projected inputs and axis order, ZIP safety, GDAL KML driver behaviour, NaN versus JSONB).
8. **Future scope:** real extensions beyond this delivery (for example KMZ, authentication, a job queue for very large files, PostGIS-side queries, perimeter and other metrics, filtering features by status). Describe the delivered scope accurately and do not list required functionality as unfinished work.
