"""CRS validation and measurement-CRS selection. Pure functions: no web or database imports."""

from collections.abc import Sequence
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry

from app.core.errors import AppError, ErrorCode

UPS_NORTH = 32661
UPS_SOUTH = 32761


@dataclass(frozen=True)
class ExtentLimits:
    warn_degrees: float = 6.0
    max_degrees: float = 30.0


def crs_label(crs: CRS) -> str:
    """`EPSG:<code>` when the CRS resolves to one, otherwise the CRS string."""
    code = crs.to_epsg()
    return f"EPSG:{code}" if code else crs.to_string()


def resolve_source_crs(dataset_crs: CRS | None, override: str | None) -> tuple[CRS, list[str]]:
    """Return the source CRS and warnings.

    The client's `override` is used only when the dataset has no usable CRS. The CRS is
    never guessed from coordinate values.
    """
    override = (override or "").strip() or None
    if dataset_crs is not None and _is_usable(dataset_crs):
        warnings = []
        if override:
            warnings.append(
                "The crs field was ignored because the file already defines a CRS "
                f"({crs_label(dataset_crs)})."
            )
        return dataset_crs, warnings
    if override:
        try:
            crs = CRS.from_user_input(override)
        except CRSError as exc:
            raise AppError(
                ErrorCode.unknown_crs, f"The crs value {override!r} is not valid."
            ) from exc
        if not _is_usable(crs):
            raise AppError(
                ErrorCode.unknown_crs,
                f"The crs value {override!r} is not a geographic or projected CRS.",
            )
        return crs, [
            f"The CRS ({crs_label(crs)}) was supplied by the client, not read from the file."
        ]
    if dataset_crs is not None:
        raise AppError(ErrorCode.unknown_crs, "The CRS defined in the file cannot be interpreted.")
    raise AppError(
        ErrorCode.missing_source_crs,
        "The Shapefile has no usable CRS information. "
        "Include a valid .prj file or pass a crs field.",
    )


def select_measurement_crs(
    geoms: Sequence[BaseGeometry | None], source_crs: CRS, limits: ExtentLimits
) -> tuple[CRS | None, list[str]]:
    """Pick one projected CRS for the whole file from the combined extent.

    Raises `AppError(extent_too_large)` when the data is too wide for one projection.
    Returns `None` plus a warning when no geometry is usable.
    """
    usable = [geom for geom in geoms if geom is not None and not geom.is_empty]
    no_geometry = ["No feature has usable geometry, so no measurement CRS was selected."]
    if not usable:
        return None, no_geometry

    # Extent in degrees. Projected inputs are converted so one rule covers every input,
    # including data crossing the antimeridian (it spans close to 360 degrees of longitude).
    series = gpd.GeoSeries(usable, crs=source_crs)
    degrees = series if source_crs.is_geographic else series.to_crs(4326)
    min_x, min_y, max_x, max_y = degrees.total_bounds
    if not np.isfinite([min_x, min_y, max_x, max_y]).all():
        return None, no_geometry

    width, height = max_x - min_x, max_y - min_y
    widest = max(width, height)
    if widest > limits.max_degrees:
        raise AppError(
            ErrorCode.extent_too_large,
            f"The data spans {width:.1f} by {height:.1f} degrees, which is too wide to "
            "measure in a single projection.",
        )
    warnings = []
    if widest > limits.warn_degrees:
        warnings.append(
            f"The data spans {width:.1f} by {height:.1f} degrees; measurements near the "
            "edges of the extent are less accurate."
        )
    try:
        return degrees.estimate_utm_crs(), warnings
    except RuntimeError:  # polar latitudes have no UTM zone
        ups = UPS_NORTH if (min_y + max_y) / 2 >= 0 else UPS_SOUTH
        return CRS.from_epsg(ups), warnings


def _is_usable(crs: CRS) -> bool:
    return crs.is_geographic or crs.is_projected
