# Geospatial File Measurement API

[![CI](https://github.com/PrathamKalburgi/GSFM-API/actions/workflows/ci.yml/badge.svg)](https://github.com/PrathamKalburgi/GSFM-API/actions/workflows/ci.yml)

A FastAPI service that accepts **KML** files and **zipped Shapefiles**, stores every feature (index, geometry type, geometry, CRS, properties) and returns reliable **area** (m²) and **length** (m) measurements. Geometry is always projected to a metric CRS before it is measured; coordinates in degrees are never measured directly.

- `POST /api/files/` upload a file
- `GET /api/files/{id}/` file details
- `GET /api/files/{id}/measurements/` per-feature results with a whole-file summary
- Extras: paginated listing, deletion, health check, request IDs and JSON logs, a documented error format, Alembic migrations, Docker and CI on SQLite and PostgreSQL.

Interactive docs are served at `/docs` once the app is running. The implementation follows [PROJECT_PLAN.md](docs/PROJECT_PLAN.md).

## 1. Quick start

Requires Python 3.11 or newer (developed and tested on 3.12). No system GDAL is needed: the geospatial wheels bundle it.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

make install                   # pip install -c requirements.lock -e ".[dev]"
make migrate                   # alembic upgrade head
make run                       # uvicorn app.main:app --reload --no-access-log
```

The API is now on <http://127.0.0.1:8000> (docs at <http://127.0.0.1:8000/docs>). By default it uses a SQLite file at `data/app.db`, created on first use, so no database or cloud account is needed.

Other commands: `make test` (pytest), `make lint` (ruff check and format check), `make format`.

**Docker**

```bash
docker build -t geo-measure .
docker run --rm -p 8000:8000 -v geo-data:/app/data geo-measure
curl http://localhost:8000/health
```

The container runs as a non-root user, applies migrations, then starts the server. Pass `-e DATABASE_URL=...` to use PostgreSQL instead of the SQLite file in the volume.

**Configuration** (environment variables or a local `.env`, see `.env.example`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///data/app.db` | SQLite file or PostgreSQL URL |
| `MAX_UPLOAD_BYTES` | 52428800 (50 MB) | Largest accepted upload |
| `MAX_ZIP_ENTRIES` | 200 | Entries allowed in a ZIP |
| `MAX_UNCOMPRESSED_BYTES` | 209715200 (200 MB) | Total expanded size of the Shapefile members |
| `MAX_FEATURES` | 50000 | Features allowed per file |
| `EXTENT_WARN_DEGREES` | 6 | Add a warning when the data spans more than this |
| `EXTENT_MAX_DEGREES` | 30 | Reject data spanning more than this |
| `LOG_LEVEL` | INFO | Log level |

## 2. Using Supabase

SQLite is the default so anyone can run the project locally. Supabase PostgreSQL is the documented production target: set `DATABASE_URL` to the connection string from the Supabase dashboard (Project Settings, Database), then run `make migrate` and `make run` as above.

- **URL scheme.** Supabase gives out `postgresql://...` URLs; SQLAlchemy would then look for psycopg2. The app rewrites the scheme to `postgresql+psycopg://` (psycopg 3) itself, so paste the string as it is.
- **SSL.** `sslmode=require` is added automatically for any host other than localhost, unless the URL already sets an `sslmode`.
- **Connection mode.** Use the **session pooler** or the direct connection, for the API and for Alembic. The direct host can be IPv6-only on some plans; if connections fail, use the session pooler string. If you choose the **transaction pooler** (port 6543), the app disables prepared statements for you (`prepare_threshold=None`).
- **Row Level Security.** The PostgreSQL branch of the migration enables RLS on both tables and adds no policies. Tables created by SQL migrations in the `public` schema are otherwise reachable through Supabase's auto-generated Data API with the public anon key. The API connects as the table-owning role, which bypasses RLS, so it is unaffected. See [Connecting to your database](https://supabase.com/docs/guides/database/connecting-to-postgres).

**Verification status.** The full test suite passes on PostgreSQL 16 (local and in CI), including the migration, JSONB columns, RLS and cascade delete. Manual run against a real Supabase project (migrate, upload, list, delete): _record the date and result here._

## 3. API

All endpoints use the `/api` prefix (except `/health`), JSON is snake_case, and file IDs are UUIDs. Routes are declared with the trailing slash exactly as in the brief. Every response carries an `X-Request-ID` header (your value if you send a safe one, otherwise a generated one). The samples below use the files in [`samples/`](samples/) and show real output.

### Upload: `POST /api/files/`

`multipart/form-data` with a required `file` (a `.kml`, or a `.zip` holding exactly one Shapefile) and an optional `crs`.

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
```

```json
{
  "id": "a810fd8c-1ef6-4665-ad7d-1f21a64f3cc3",
  "filename": "survey.kml",
  "format": "KML",
  "feature_count": 5,
  "crs": "EPSG:4326",
  "measurement_crs": "EPSG:32643",
  "status": "COMPLETED",
  "warnings": [
    "1 feature could not be measured (UNSUPPORTED or INVALID_GEOMETRY); see per-feature status."
  ],
  "created_at": "2026-10-08T11:36:14.377409Z"
}
```

The warning is there because the sample's last Placemark has no geometry yet. Other features are still stored and measured.

`crs` is the CRS of the input. A zipped Shapefile:

```bash
curl -F "file=@samples/parcels.zip" http://localhost:8000/api/files/
```

```json
{
  "id": "12d17501-a12b-48ff-ad5f-52159f755ddb",
  "filename": "parcels.zip",
  "format": "SHAPEFILE_ZIP",
  "feature_count": 3,
  "crs": "EPSG:32643",
  "measurement_crs": "EPSG:32643",
  "status": "COMPLETED",
  "warnings": [],
  "created_at": "2026-10-08T11:36:14.574716Z"
}
```

This Shapefile is already in UTM (EPSG:32643). It is still reprojected to the measurement CRS, which here is a no-op because they match.

**The `crs` upload field.** A Shapefile must carry a `.prj`. Without one the upload is rejected, because the CRS is never guessed from coordinate values:

```bash
curl -F "file=@samples/parcels_no_prj.zip" http://localhost:8000/api/files/
```

```json
{
  "error": {
    "code": "missing_source_crs",
    "message": "The Shapefile has no usable CRS information. Include a valid .prj file or pass a crs field.",
    "request_id": "c73ecaafce5d4a7da55af4ea21d15ba7"
  }
}
```

If you know the CRS, state it and the file is processed, with a warning that the CRS came from you:

```bash
curl -F "file=@samples/parcels_no_prj.zip" -F "crs=EPSG:32643" http://localhost:8000/api/files/
```

```json
{
  "id": "4ae82c31-7e03-4ac2-9b89-15f8b91d0dbf",
  "filename": "parcels_no_prj.zip",
  "format": "SHAPEFILE_ZIP",
  "feature_count": 3,
  "crs": "EPSG:32643",
  "measurement_crs": "EPSG:32643",
  "status": "COMPLETED",
  "warnings": [
    "The CRS (EPSG:32643) was supplied by the client, not read from the file."
  ],
  "created_at": "2026-10-08T11:36:14.723101Z"
}
```

The field is used only when the file has no CRS of its own. When the file already defines one, `crs` is ignored and a warning says so. An invalid value is rejected with `unknown_crs`.

### Detail: `GET /api/files/{id}/`

```bash
curl http://localhost:8000/api/files/a810fd8c-1ef6-4665-ad7d-1f21a64f3cc3/
```

Returns the same shape as the upload response (`id`, `filename`, `feature_count`, `crs`, `status` as in the brief, plus `format`, `measurement_crs`, `warnings`, `created_at`). Unknown IDs return `404`.

### Measurements: `GET /api/files/{id}/measurements/`

| Parameter | Default | Meaning |
| --- | --- | --- |
| `limit` | 100 (max 500) | Page size |
| `offset` | 0 | Features to skip |
| `include_geometry` | `true` | `false` returns `geometry: null` for lighter pages |

```bash
curl "http://localhost:8000/api/files/a810fd8c-1ef6-4665-ad7d-1f21a64f3cc3/measurements/?limit=1"
```

```json
{
  "file_id": "a810fd8c-1ef6-4665-ad7d-1f21a64f3cc3",
  "crs": "EPSG:4326",
  "measurement_crs": "EPSG:32643",
  "total": 5,
  "limit": 1,
  "offset": 0,
  "summary": {
    "features": 5,
    "by_status": {
      "MEASURED": 3,
      "NOT_REQUIRED": 1,
      "UNSUPPORTED": 0,
      "INVALID_GEOMETRY": 1
    },
    "total_area_m2": 5527.83,
    "total_length_m": 318.74
  },
  "features": [
    {
      "feature_index": 0,
      "source_layer": "Parcels",
      "source_feature_id": "p1",
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [[77.59, 12.97], [77.5904, 12.97], [77.5904, 12.9704], [77.59, 12.9704], [77.59, 12.97]]
        ]
      },
      "properties": {
        "Name": "North parcel",
        "owner": "Asha",
        "zone": 3
      },
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

Each feature carries its index, layer, source ID, geometry type, geometry (in the source CRS, named in the feature's `crs`), properties and measurement. Areas are in m² and lengths in m, rounded to two decimals in the response (full precision is stored).

**The `summary` block** covers the **whole file**, not the page: it is computed with SQL aggregates, so it is identical for every page of the same file. All five features of the sample at once (`?include_geometry=false`):

| index | layer | id | type | properties | status | area_m2 | length_m |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Parcels | p1 | Polygon | `{"Name": "North parcel", "owner": "Asha", "zone": 3}` | MEASURED | 1922.72 | - |
| 1 | Parcels | p2 | Polygon | `{"Name": "South parcel"}` | MEASURED | 3605.11 | - |
| 2 | Roads | r1 | LineString | `{"Name": "Access road"}` | MEASURED | - | 318.74 |
| 3 | Roads | g1 | Point | `{"Name": "Gate"}` | NOT_REQUIRED | - | - |
| 4 | Roads | d1 | null | `{"Name": "Draft (no geometry yet)"}` | INVALID_GEOMETRY | - | - |

- Polygons and MultiPolygons get `area_m2`; LineStrings and MultiLineStrings get `length_m`; Points get neither and are `NOT_REQUIRED`.
- A feature with no geometry is `INVALID_GEOMETRY`. A GeometryCollection or LinearRing is `UNSUPPORTED`. A self-intersecting polygon is `INVALID_GEOMETRY` with the reason from Shapely; invalid geometry is reported, never repaired.
- One bad feature never stops the others.

### List and delete

```bash
curl "http://localhost:8000/api/files/?limit=2&offset=0"     # most recent first
```

Returns `{"total": 3, "limit": 2, "offset": 0, "items": [ ... 2 file summaries ... ]}` with `limit` (default 20, max 100) and `offset`.

```bash
curl -X DELETE http://localhost:8000/api/files/a810fd8c-1ef6-4665-ad7d-1f21a64f3cc3/    # 204 No Content
```

Deleting removes the file and all its features in one transaction (foreign-key cascade). Afterwards the ID returns:

```json
{
  "error": {
    "code": "file_not_found",
    "message": "No uploaded file has that ID.",
    "request_id": "7c42fc6538414f8ca1a666f701ba46a3"
  }
}
```

### Health: `GET /health`

```json
{
  "status": "ok",
  "database": "ok"
}
```

Runs `SELECT 1`; returns `503 {"status": "unavailable", "database": "error"}` when the database is unreachable. Connection details never appear in the body.

### Errors

Every error, including FastAPI's validation errors and unknown routes, uses one shape. `request_id` matches the `X-Request-ID` header, and the same ID appears in the server log.

```json
{
  "error": {
    "code": "missing_source_crs",
    "message": "The Shapefile has no usable CRS information. Include a valid .prj file or pass a crs field.",
    "request_id": "..."
  }
}
```

| Code | HTTP | When |
| --- | --- | --- |
| `unsupported_format` | 415 | Not .kml or .zip, content does not match the extension, or KMZ |
| `file_too_large` | 413 | Upload byte limit exceeded |
| `archive_limit_exceeded` | 413 | Too many ZIP entries or uncompressed size too large |
| `too_many_features` | 413 | Feature-count limit exceeded |
| `invalid_archive` | 400 | Corrupt ZIP, unsafe entries, or nested archives |
| `missing_shapefile_component` | 400 | .shp, .shx or .dbf missing |
| `ambiguous_archive` | 400 | More than one Shapefile in the ZIP |
| `missing_source_crs` | 422 | No usable CRS and no crs field |
| `unknown_crs` | 422 | CRS present or supplied but cannot be interpreted |
| `unreadable_data` | 422 | The driver cannot read the dataset |
| `empty_dataset` | 422 | Zero features |
| `extent_too_large` | 422 | Extent too wide for a single measurement projection |
| `validation_error` | 422 | Bad request parameters |
| `file_not_found` | 404 | Unknown file ID |
| `internal_error` | 500 | Anything unexpected; no stack trace, paths or secrets |

Responses for requests that never reach a route use three extra codes:

| Code | HTTP | When |
| --- | --- | --- |
| `not_found` | 404 | Unknown route |
| `method_not_allowed` | 405 | HTTP method not supported on this route |
| `bad_request` | 400 | Malformed request that never reached a route |

Messages are written for users. Stack traces, file paths and credentials are only ever written to the server log.

## 4. Architecture

### Application structure

```text
app/
├── main.py                  app factory: middleware, exception handlers, routers
├── api/routes/
│   ├── files.py             HTTP contract only: parameters, status codes, schemas, OpenAPI
│   └── health.py
├── core/
│   ├── config.py            settings from environment variables, DB URL normalisation
│   ├── errors.py            AppError, error codes, exception handlers
│   ├── logging.py           JSON log formatter
│   └── middleware.py        request ID + access log, request-body size limit
├── db/                      declarative base, engine and session
├── models/                  SQLAlchemy models: uploaded_files, feature_results
├── repositories/            every query and the insert transaction
├── schemas/                 API response shapes, separate from the models
└── services/
    ├── upload_service.py    orchestrates one upload
    ├── archive.py           safe ZIP member selection and extraction
    ├── reader.py            format detection, layers, property normalisation
    ├── crs.py               pure functions: source CRS, measurement CRS, extent guard
    └── measurement.py       pure functions: per-geometry measurement and status
```

Routes contain no SQL and no parsing. `crs.py` and `measurement.py` import neither FastAPI nor SQLAlchemy, which is what lets them be tested against an independent oracle in isolation.

### File-processing flow

1. The body is capped while it is read (see the size note below). The route copies the upload to a fresh temporary directory under a generated name, counting bytes and aborting at the limit.
2. The format is detected from the extension **and** the content: ZIP magic bytes for `.zip`, KML markup for `.kml`. `.kmz` is rejected with a clear message.
3. For a ZIP, only the Shapefile members (`.shp`, `.shx`, `.dbf`, `.prj`, optional `.cpg`) are copied, under generated names. Nothing else is extracted, and names inside the archive never reach the filesystem.
4. Each layer is read separately (a KML `<Folder>` is a layer). Layers are never concatenated, so a feature never inherits another layer's empty attribute columns. The feature count is checked before geometry is loaded where the driver reports it.
5. The source CRS is determined and one measurement CRS is chosen for the whole file.
6. Every feature gets a measurement or a status.
7. The file row and all feature rows are inserted in **one transaction**, in batches. Any failure rolls everything back, so a half-processed file is never visible.
8. The temporary directories are removed on success and on failure. The response is `201` with the file summary.

*Size note.* Starlette parses a multipart upload completely before the route runs, so a size check inside the route would come too late. A small middleware therefore counts body bytes as they arrive (and refuses a declared `Content-Length` that is too large) so that an oversized or chunked upload is aborted early. The route repeats the exact per-file check.

### Measurement flow

1. Source geometries are read as they are and kept for reporting (in their own CRS).
2. A transformed **copy** is made: Z is dropped (`shapely.force_2d`, measurement is planar), then the copy is reprojected to the file's measurement CRS.
3. Polygon area and line length are taken from the projected copy: `area_m2` in square metres, `length_m` in metres.
4. Validity is checked on the **source** geometry before measuring. Empty, invalid and unsupported geometries get a status and a message instead of a number.
5. Full-precision floats are stored; the API rounds to two decimals when serialising.

### CRS handling

- **Source CRS.** KML is WGS 84 (EPSG:4326) by definition. A Shapefile's CRS comes from its `.prj`. The CRS is reported as `EPSG:<code>` when it resolves to one, otherwise as the CRS string.
- **Missing or unknown CRS.** A Shapefile without a usable `.prj` is rejected (`missing_source_crs`) unless the client passes `crs`. A CRS that cannot be interpreted is rejected (`unknown_crs`). The CRS is never guessed from coordinate values.
- **Choosing the measurement CRS.** One projected CRS per file, so totals within a file are consistent. First an extent guard runs on the combined bounds converted to degrees: a warning above about 6° in either direction, a rejection (`extent_too_large`) above about 30°. A file crossing the antimeridian spans close to 360° of longitude and is rejected by the same rule. Then GeoPandas' `estimate_utm_crs()` picks the UTM zone of the extent's centre. At polar latitudes, where no UTM zone exists, Universal Polar Stereographic is used (EPSG:32661 north, EPSG:32761 south). If every geometry is empty, `measurement_crs` is `null` and a warning is added.
- **Transformation.** Every file is reprojected, **including inputs that are already projected**. Web Mercator, for example, inflates area by roughly 1/cos²(latitude), so its numbers are never trusted. Axis order is fixed to longitude/latitude (`to_crs` / `always_xy`), so latitude and longitude cannot be swapped.
- **Reported.** The upload response and the measurements response return both `crs` (source) and `measurement_crs`; every feature names the CRS of its coordinates.

## 5. Design decisions and alternatives

| Decision | Alternatives considered | Why this one |
| --- | --- | --- |
| Reproject to a **UTM** zone and measure there | Geodesic calculation with `pyproj.Geod`; one equal-area CRS | UTM errors are small within a zone and planar geometry operations are simple and fast. A geodesic calculation is more exact but needs separate code paths per geometry type; an equal-area CRS distorts lengths. `Geod` is used as the **independent oracle in the tests** instead. |
| Measure in **Python** (Shapely + pyproj) | Measure in PostGIS | Works identically on SQLite and PostgreSQL and needs no database extension, so the project runs without any account. |
| **Synchronous** processing in the request | A job queue with a status endpoint | The size limits keep requests short, and a queue adds infrastructure the brief does not need. Plain `def` handlers run in FastAPI's thread pool, so blocking parsing does not block the event loop. |
| **SQLite by default**, Supabase as the target | Supabase only | A reviewer can clone and run with no cloud account. CI also runs the full suite on real PostgreSQL. |
| **pyogrio** (via GeoPandas) | Fiona; `fastkml`/`pyshp` | pyogrio's wheels bundle GDAL, so no system install. One reader handles both formats. Fiona is not used. |
| **Reject** a Shapefile with no CRS unless `crs` is given | Guess the CRS from coordinates; assume WGS 84 | Guessing silently produces wrong areas. An explicit client-supplied CRS is honest and is flagged in `warnings`. |
| **Global** `feature_index` across layers | Per-layer index | A per-layer index collides with the `(file_id, feature_index)` unique constraint as soon as a KML has two folders. |
| Store the **source** geometry and project on demand | Store projected geometry | The API returns real source coordinates; the measurement CRS is stored per file so results are reproducible. |
| Report invalid geometry, **do not repair** it | `make_valid` automatically | Silent repair changes what is being measured. The status and Shapely's reason let the client decide. |
| One thin repository | Queries in the routes | Keeps SQL and the transaction out of the routes. |

## 6. Accuracy and limitations

UTM keeps length and area errors small within a zone: about **0.1%** in the check below, and a few tenths of a percent at the edge of a zone. That suits local and regional survey files. It is **not** a promise of survey-grade accuracy for arbitrary global datasets.

One small polygon near Bengaluru:

| Method | Area | Difference |
| --- | --- | --- |
| Geodesic (`pyproj.Geod`) | 1920.5 m² | reference |
| Projected to UTM 43N (what this API does) | 1922.7 m² | +0.12% |
| Measured in Web Mercator (EPSG:3857) | 2034.6 m² | +5.9% |

The last row is why projected inputs are reprojected as well. The test suite compares results with the geodesic oracle within 0.5% for four locations on different continents and latitudes, and checks that the same polygon in EPSG:4326 and EPSG:3857 gives the same area.

Limitations to know about:

- **Polar data.** Universal Polar Stereographic has a scale factor of 0.994 at the pole, so areas very close to a pole carry roughly 1% error. The polar tests accept 2%.
- **Extent.** Data spanning more than about 30° is rejected, and anything above about 6° carries a warning, because one projection cannot describe it accurately.
- **Geometry.** Z values are ignored (measurement is planar). GeometryCollections, LinearRings, empty and invalid geometries are reported per feature and not measured. Invalid geometry is not repaired.
- **KML attributes.** GDAL drops `SchemaData/SimpleData` values when the `Schema` they refer to is not declared in the document. Declared schemas and untyped `Data/value` pairs are kept. GDAL's fixed style fields (`altitudeMode`, `tessellate`, `extrude`, `visibility`, `drawOrder`, `icon`) are dropped, and a KML feature carries only the attributes it actually has.
- **Formats.** KMZ and GeoJSON are not supported. A ZIP must contain exactly one Shapefile.
- **Limits** (configurable): 50 MB upload, 200 ZIP entries, 200 MB expanded, 50,000 features.
- **Storage.** Uploaded source files are held in temporary storage only while they are parsed. Only parsed geometry and results are stored.

## 7. Learning

- **CRS pitfalls.** A projected input is not automatically safe to measure in: Web Mercator overstates area by 5.9% here. Axis order is a real trap (EPSG:4326 is latitude/longitude by definition, GIS libraries usually want longitude/latitude), and `always_xy` settles it. `estimate_utm_crs()` returns a meaningless zone for data crossing the antimeridian and raises at polar latitudes, so the extent guard has to come first.
- **Independent oracle.** Checking against `pyproj.Geod` verifies the projection choice and axis order without hand-computed constants, and a naive calculation in degrees fails it by many orders of magnitude.
- **ZIP safety.** Not extracting the archive removes path traversal instead of filtering for it. Declared sizes in a ZIP can lie, so the size limit is also enforced on the bytes actually read. macOS archives add `__MACOSX/` and `._*` entries that would otherwise look like extra Shapefiles.
- **The GDAL KML driver.** Each `<Folder>` is a layer. Fixed style fields are added to every feature, and extended-data columns exist for every feature in the layer, so features without them get NaN. Reading layers separately avoids cross-layer nulls. GDAL only keeps `SimpleData` values for declared schemas.
- **NaN versus JSONB.** PostgreSQL's JSON types reject the `NaN` token that Python's encoder emits, so unhandled missing values would pass on SQLite and fail on PostgreSQL. All attribute values are converted (NaN, NaT, NA and infinity become `null`), and the suite runs on both databases. A related pandas quirk: an integer column with a missing value turns into floats, so declared integer fields are restored (`3`, not `3.0`).
- **SQLite and foreign keys.** `ON DELETE CASCADE` silently does nothing on SQLite unless `PRAGMA foreign_keys=ON` is set on every connection.
- **Where limits really apply.** Starlette reads the whole multipart body before the route runs, so an upload limit has to be enforced earlier, while bytes arrive.
- **Supabase specifics.** The URL scheme, SSL, pooler modes and Row Level Security on tables created from SQL (section 2).

## 8. Future scope

Real extensions beyond this delivery:

- KMZ support (extract `doc.kml` safely and reuse the KML path).
- Authentication and per-user access to files.
- A job queue and a status endpoint for very large files.
- PostGIS-side queries and spatial indexes for filtering by area or location.
- Perimeter and other metrics, and filtering features by status.
- Optional geodesic measurement for very wide extents.
