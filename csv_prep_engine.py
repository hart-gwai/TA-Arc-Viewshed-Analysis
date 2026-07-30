# -*- coding: utf-8 -*-
"""
Step 1 logic: import raw CSV, map columns via user selections, optional building
spatial join, and output a standardized ping point layer for the main workflow.
"""

import csv
import math
import os
import re
from collections import defaultdict
from datetime import date, datetime

import processing
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProcessingContext,
    QgsProject,
    QgsRaster,
    QgsVariantUtils,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant

from .logic_engine import (
    FIELD_AZIMUTH,
    FIELD_BEAM_WIDTH,
    FIELD_END_AZ,
    FIELD_MAX_RADIUS,
    FIELD_MIN_RADIUS,
    FIELD_OBSERVER_H,
    FIELD_START_AZ,
    FIELD_TIMESTAMP,
    FIELD_TOWER,
    LOG_TAG,
    _first_matching_field,
    _normalize_azimuth,
    _safe_float,
)

PREPARED_LAYER_NAME = "Prepared_Ping_Layer"
DEFAULT_CRS = QgsCoordinateReferenceSystem("EPSG:4326")


def _prepared_ping_output_path():
    """GeoPackage next to the saved QGIS project file for Prepare Cell Site Data."""
    project_dir = QgsProject.instance().absolutePath()
    if not project_dir:
        raise RuntimeError(
            "Save the QGIS project before running Prepare Cell Site Data. "
            f"'{PREPARED_LAYER_NAME}' is written next to the project file."
        )
    return os.path.join(project_dir, f"{PREPARED_LAYER_NAME}.gpkg")

# Standard field names written to the prepared layer (match logic_engine lookups).
OUT_TOWER = "Cell_Site"
OUT_MIN_R = "Min of minD"
OUT_MAX_R = "Max of maxD"
OUT_START_AZ = "Start_Azimuth"
OUT_END_AZ = "End_Azimuth"
OUT_TIMESTAMP = "Timestamp"
OUT_OBSERVER_H = "observer_height"

NONE_CHOICE = "(none — skip)"
UNLOCATED_PREFIX = "UNLOCATED"
COORD_PRECISION = 5  # decimal degrees (~1 m) for grouping colocated pings
DEFAULT_BUILDING_NAME_FIELD = "BuildingNameEN"


def _normalize_tower_name(name):
    """Normalize tower / building names for consistent matching (case, spacing)."""
    if name is None:
        return ""
    return " ".join(str(name).split()).upper()


def _coord_tower_key(point, prefix=UNLOCATED_PREFIX):
    """Stable key for one WGS84 coordinate (~1 m precision)."""
    return f"{prefix}_{point.y():.{COORD_PRECISION}f}_{point.x():.{COORD_PRECISION}f}"


def _collect_ping_locations(source_layer):
    """
    Map each source feature to a coordinate key and collect unique locations.

    Returns (feat_to_coord_key, coord_key_to_point) or ({}, {}) when no valid points.
    """
    feat_to_coord_key = {}
    coord_key_to_point = {}

    for feat in source_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PointGeometry:
            continue

        point = geom.asPoint()
        coord_key = _coord_tower_key(point)
        feat_to_coord_key[feat.id()] = coord_key
        coord_key_to_point[coord_key] = point

    return feat_to_coord_key, coord_key_to_point


def _unique_locations_layer(coord_key_to_point):
    """One point per unique coordinate for a single building lookup pass."""
    layer = QgsVectorLayer(
        f"Point?crs={DEFAULT_CRS.authid()}", "unique_ping_locations", "memory"
    )
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("coord_key", QVariant.String)])
    layer.updateFields()

    features = []
    for coord_key, point in coord_key_to_point.items():
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(point))
        feat.setAttributes([coord_key])
        features.append(feat)

    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def building_join_field_list(building_layer, building_field):
    """Field to pull from the building layer during spatial join (name only)."""
    names = {f.name() for f in building_layer.fields()}
    if building_field in names:
        return [building_field]
    return []


def tower_from_joined_feature(feat, building_field):
    """
    Return the building display name from a spatial join result.

    Empty when the name field is NULL — treated the same as no building match.
    """
    if not building_field:
        return ""
    col = f"bldg_{building_field}"
    names = {f.name() for f in feat.fields()}
    if col in names and not _is_null_value(feat[col]):
        return _normalize_tower_name(feat[col])
    return ""


def _cell_site_label(label, point):
    """Append coordinates so each physical site location has a distinct Cell_Site."""
    lat = f"{point.y():.{COORD_PRECISION}f}"
    lon = f"{point.x():.{COORD_PRECISION}f}"
    if not label or str(label).startswith(UNLOCATED_PREFIX):
        return _coord_tower_key(point)
    return f"{label} ({lat}, {lon})"


def _ensure_unique_cell_sites(cell_sites, coord_key_to_point):
    """
    Guarantee one distinct Cell_Site per unique lat/lon.

    Different coordinates can receive the same building name when they fall inside
    the same building polygon. Append coordinates to disambiguate collisions.
    Single-location building names are kept as-is.
    """
    label_to_keys = defaultdict(list)
    for coord_key, label in cell_sites.items():
        label_to_keys[label].append(coord_key)

    for label, coord_keys in label_to_keys.items():
        if len(coord_keys) <= 1:
            continue
        for coord_key in coord_keys:
            point = coord_key_to_point[coord_key]
            cell_sites[coord_key] = _cell_site_label(label, point)

    return cell_sites


def guess_delimiter(path):
    """Guess CSV delimiter from the first line."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        sample = handle.readline()
    if sample.count("\t") >= sample.count(",") and sample.count("\t") > 0:
        return "\t"
    if sample.count(";") > sample.count(","):
        return ";"
    return ","


def read_csv_column_names(path):
    """Return column names from the first row of a CSV file."""
    delimiter = guess_delimiter(path)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        row = next(reader, [])
    return [col.strip() for col in row if col.strip()]


def auto_guess_mapping(column_names):
    """
    Build default dropdown selections from raw CSV headers.
    Returns dict of role -> column name or NONE_CHOICE.
    """
    fake_fields = [QgsField(name, QVariant.String) for name in column_names]

    def _pick(candidates):
        match = _first_matching_field(fake_fields, candidates)
        return match or NONE_CHOICE

    lon_candidates = ("Longitude", "longitude", "Lon", "lon", "X", "x", "lng", "LNG")
    lat_candidates = ("Latitude", "latitude", "Lat", "lat", "Y", "y")

    lon = next((c for c in column_names if c in lon_candidates), NONE_CHOICE)
    lat = next((c for c in column_names if c in lat_candidates), NONE_CHOICE)

    return {
        "tower": _pick(FIELD_TOWER),
        "min_r": _pick(FIELD_MIN_RADIUS),
        "max_r": _pick(FIELD_MAX_RADIUS),
        "start_az": _pick(FIELD_START_AZ),
        "end_az": _pick(FIELD_END_AZ),
        "azimuth": _pick(FIELD_AZIMUTH),
        "beam": _pick(FIELD_BEAM_WIDTH),
        "timestamp": _pick(FIELD_TIMESTAMP),
        "observer_h": _pick(FIELD_OBSERVER_H),
        "x": lon,
        "y": lat,
    }


def _file_uri(path, x_field, y_field, crs_authid, delimiter=","):
    clean_path = path.replace("\\", "/")
    delim = "%09" if delimiter == "\t" else delimiter
    return (
        f"file:///{clean_path}"
        f"?delimiter={delim}"
        f"&detectTypes=yes"
        f"&xField={x_field}"
        f"&yField={y_field}"
        f"&crs={crs_authid}"
    )


def load_csv_as_layer(path, x_field, y_field, crs_authid="EPSG:4326"):
    """Load a CSV file as a temporary point layer."""
    delimiter = guess_delimiter(path)
    uri = _file_uri(path, x_field, y_field, crs_authid, delimiter)
    layer = QgsVectorLayer(uri, "csv_import_temp", "delimitedtext")
    if layer.isValid():
        return layer

    # Fallback: try comma explicitly if auto-guess delimiter failed.
    if delimiter != ",":
        uri = _file_uri(path, x_field, y_field, crs_authid, ",")
        layer = QgsVectorLayer(uri, "csv_import_temp", "delimitedtext")
    return layer


def _is_none_choice(value):
    return not value or value == NONE_CHOICE


def _is_null_value(value):
    """Return True when a feature attribute is NULL / empty."""
    if value is None:
        return True
    try:
        if QgsVariantUtils.isNull(value):
            return True
    except Exception:
        pass
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _format_timestamp(value):
    """Convert QDateTime and other date types to a plain string."""
    if _is_null_value(value):
        return ""

    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-dd HH:mm:ss") if value.isValid() else ""

    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd") if value.isValid() else ""

    if isinstance(value, QTime):
        return value.toString("HH:mm:ss") if value.isValid() else ""

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, QVariant):
        if value.type() == QVariant.DateTime:
            return _format_timestamp(value.toDateTime())
        if value.type() == QVariant.Date:
            return _format_timestamp(value.toDate())
        if value.type() == QVariant.Time:
            return _format_timestamp(value.toTime())

    text = str(value).strip()

    # Repair timestamps saved as Python repr strings from an earlier bug.
    dt_match = re.match(
        r"PyQt5\.QtCore\.QDateTime\((\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*(\d+))?\)",
        text,
    )
    if dt_match:
        parts = [int(dt_match.group(i)) for i in range(1, 7)]
        y, mo, d, h, mi = parts[:5]
        sec = parts[5] if parts[5] is not None else 0
        return QDateTime(y, mo, d, h, mi, sec).toString("yyyy-MM-dd HH:mm:ss")

    date_match = re.match(r"PyQt5\.QtCore\.QDate\((\d+),\s*(\d+),\s*(\d+)\)", text)
    if date_match:
        y, mo, d = (int(date_match.group(i)) for i in range(1, 4))
        return QDate(y, mo, d).toString("yyyy-MM-dd")

    return text


def _parse_sample_result(result):
    """
    Parse provider.sample() return value.

    QGIS Python bindings differ by version:
    - (success, value)
    - (value, success)
    """
    if not isinstance(result, tuple) or len(result) < 2:
        return None

    a, b = result[0], result[1]

    def _is_success_flag(v):
        if isinstance(v, bool):
            return True
        if isinstance(v, (int, float)) and v in (0, 1):
            return True
        return False

    def _to_float(v):
        try:
            if isinstance(v, bool):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    # (success, value)
    if _is_success_flag(a):
        if a in (True, 1, 1.0):
            return _to_float(b)
        return None

    # (value, success)
    if _is_success_flag(b):
        if b in (True, 1, 1.0):
            return _to_float(a)
        return None

    # Both numeric — use the value that looks like an elevation, not a flag
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None:
        return fb
    if fb is None:
        return fa
    if fa in (0.0, 1.0) and fb not in (0.0, 1.0):
        return fb
    if fb in (0.0, 1.0) and fa not in (0.0, 1.0):
        return fa
    return max(fa, fb, key=abs)


def _raster_value_at_point(provider, point, band=1):
    """
    Read a single raster band value at *point* using the most compatible API
    available in the running QGIS version.
    """
    # identify() has a stable {band: value} result dict across QGIS versions
    fmt = None
    try:
        fmt = QgsRaster.IdentifyFormatValue
    except AttributeError:
        pass
    if fmt is None:
        try:
            from qgis.core import Qgis

            fmt = Qgis.RasterIdentifyFormat.Value
        except AttributeError:
            fmt = 0

    try:
        ident = provider.identify(point, fmt)
        if ident.isValid():
            value = ident.results().get(band)
            if not _is_null_value(value):
                return float(value)
    except (TypeError, ValueError, AttributeError):
        pass

    # Fallback: sample()
    try:
        result = provider.sample(point, band)
        parsed = _parse_sample_result(result)
        if parsed is not None:
            return parsed
        if result is not None and not isinstance(result, tuple) and not _is_null_value(result):
            return float(result)
    except (TypeError, ValueError, AttributeError):
        pass

    return None


def _sample_dem_at_point(dem_layer, point, source_crs):
    """Sample band 1 of *dem_layer* at *point* (in *source_crs*)."""
    if dem_layer is None or not dem_layer.isValid():
        return None

    sample_point = QgsPointXY(point.x(), point.y())
    dem_crs = dem_layer.crs()
    if source_crs != dem_crs:
        transform = QgsCoordinateTransform(source_crs, dem_crs, QgsProject.instance())
        sample_point = transform.transform(sample_point)

    provider = dem_layer.dataProvider()
    value = _raster_value_at_point(provider, sample_point, band=1)
    if value is None:
        return None

    nodata = provider.sourceNoDataValue(1)
    if nodata is not None and float(value) == float(nodata):
        return None

    return float(value)


def _arc_angles_from_bearing(bearing, delta):
    """Build start/end azimuth as bearing ± delta degrees."""
    bearing = _safe_float(bearing)
    delta = abs(_safe_float(delta, 45.0))
    return _normalize_azimuth(bearing - delta), _normalize_azimuth(bearing + delta)


def _arc_angles_from_attrs(attrs, use_azimuth_mode, azimuth_delta=45.0):
    """Return (start_az, end_az) from mapped attribute dict."""
    if not use_azimuth_mode:
        return (
            _safe_float(attrs.get(OUT_START_AZ)),
            _safe_float(attrs.get(OUT_END_AZ)),
        )

    return _arc_angles_from_bearing(attrs.get("_azimuth_raw"), azimuth_delta)


def _processing_output_layer(output, name="temp"):
    if isinstance(output, str):
        layer = QgsVectorLayer(output, name, "memory")
        if not layer.isValid():
            raise RuntimeError(f"Failed to load processing output: {output}")
        return layer
    return output


class CsvPrepEngine:
    """Prepare raw CSV / table data into a standardized ping layer."""

    def log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def _processing_context_skip_invalid(self):
        """Processing context that skips invalid geometries instead of aborting."""
        context = QgsProcessingContext()
        skip_invalid = getattr(QgsFeatureRequest, "InvalidGeometryCheckSkipInvalid", None)
        if skip_invalid is None:
            skip_invalid = getattr(QgsFeatureRequest, "GeometrySkipInvalid", None)
        if skip_invalid is not None and hasattr(context, "setInvalidGeometryCheck"):
            context.setInvalidGeometryCheck(skip_invalid)
        return context

    def _buildings_near_points(self, buildings, points, buffer_metres=1000):
        """
        Clip the building layer to the ping area only.

        The full HK building outline has hundreds of thousands of polygons; spatial
        joins only need buildings near the cell-site coordinates.
        """
        if buildings is None or points is None or points.featureCount() == 0:
            return buildings

        extent = points.extent()
        if extent.isEmpty():
            return buildings

        total = buildings.featureCount()
        extent.grow(buffer_metres)
        result = processing.run(
            "native:extractbyextent",
            {
                "INPUT": buildings,
                "EXTENT": extent,
                "CLIP": True,
                "OUTPUT": "memory:",
            },
            context=self._processing_context_skip_invalid(),
        )
        subset = _processing_output_layer(result["OUTPUT"], "buildings_near_pings")
        self.log(
            f"Building layer clipped to ping area (+{buffer_metres} m): "
            f"{subset.featureCount()} of {total} feature(s)."
        )
        return subset

    def _clean_vector_layer(self, layer, label="layer"):
        """
        Repair and remove invalid geometries before spatial processing.

        Uses native:fixgeometries when available, then copies only valid features
        in Python so we do not depend on algorithms missing in some QGIS builds.
        """
        if layer is None or not layer.isValid():
            return layer

        input_count = layer.featureCount()
        source = layer

        try:
            source = _processing_output_layer(
                processing.run(
                    "native:fixgeometries",
                    {"INPUT": layer, "OUTPUT": "memory:"},
                )["OUTPUT"],
                f"{label}_fixed",
            )
        except Exception as exc:
            self.log(
                f"{label}: fix geometries skipped ({exc}); filtering invalid features only.",
                Qgis.Warning,
            )

        wkb_name = QgsWkbTypes.displayString(source.wkbType())
        cleaned = QgsVectorLayer(
            f"{wkb_name}?crs={source.crs().authid()}",
            f"{label}_clean",
            "memory",
        )
        provider = cleaned.dataProvider()
        provider.addAttributes(source.fields())
        cleaned.updateFields()

        kept = []
        dropped = 0
        request = QgsFeatureRequest()
        skip_invalid = getattr(QgsFeatureRequest, "InvalidGeometryCheckSkipInvalid", None)
        if skip_invalid is None:
            skip_invalid = getattr(QgsFeatureRequest, "GeometrySkipInvalid", None)
        if skip_invalid is not None:
            request.setInvalidGeometryCheck(skip_invalid)

        for feat in source.getFeatures(request):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                dropped += 1
                continue

            if hasattr(geom, "isGeosValid") and not geom.isGeosValid():
                if hasattr(geom, "makeValid"):
                    geom = geom.makeValid()
                if geom is None or geom.isEmpty():
                    dropped += 1
                    continue
                if hasattr(geom, "isGeosValid") and not geom.isGeosValid():
                    dropped += 1
                    continue

            out_feat = QgsFeature(cleaned.fields())
            out_feat.setGeometry(QgsGeometry(geom))
            out_feat.setAttributes(feat.attributes())
            kept.append(out_feat)

        provider.addFeatures(kept)
        cleaned.updateExtents()

        removed = input_count - len(kept)
        if removed:
            self.log(
                f"{label}: skipped {removed} feature(s) with invalid geometry "
                f"({input_count} -> {len(kept)}).",
                Qgis.Warning,
            )
        return cleaned

    def _apply_time_binning(self, features, bin_minutes, fields):
        """
        Group features by (Cell_Site, Rounded_Timestamp, Start_Azimuth, End_Azimuth)
        and aggregate TA ranges: min(Min_Radius), max(Max_Radius).
        """
        from datetime import timedelta

        grouped = defaultdict(list)
        for feat in features:
            tower = feat[OUT_TOWER]
            start_az = feat[OUT_START_AZ]
            end_az = feat[OUT_END_AZ]
            timestamp_str = feat[OUT_TIMESTAMP]

            # Parse and floor timestamp
            if not timestamp_str:
                binned_ts = ""
            else:
                try:
                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    # Floor to nearest bin_minutes
                    minute_offset = dt.minute % bin_minutes
                    floored_dt = dt - timedelta(minutes=minute_offset, seconds=dt.second, microseconds=dt.microsecond)
                    binned_ts = floored_dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    binned_ts = timestamp_str

            group_key = (tower, binned_ts, start_az, end_az)
            grouped[group_key].append(feat)

        binned_features = []
        for (tower, binned_ts, start_az, end_az), group_feats in grouped.items():
            if not group_feats:
                continue

            min_r = min(f[OUT_MIN_R] for f in group_feats)
            max_r = max(f[OUT_MAX_R] for f in group_feats)
            observer_h = group_feats[0][OUT_OBSERVER_H]
            geom = group_feats[0].geometry()

            out_feat = QgsFeature(fields)
            out_feat.setGeometry(QgsGeometry(geom))
            out_feat.setAttributes([
                tower,
                min_r,
                max_r,
                start_az,
                end_az,
                binned_ts,
                observer_h
            ])
            binned_features.append(out_feat)

        self.log(f"Time binning ({bin_minutes} mins) reduced {len(features)} raw records to {len(binned_features)} aggregated points.")
        return binned_features

    def prepare_layer(
        self,
        source_layer,
        mapping,
        use_azimuth_mode=False,
        building_layer=None,
        building_field=None,
        fill_tower_from_buildings=False,
        dem_layer=None,
        azimuth_delta=45.0,
        time_bin_minutes=0,
    ):
        """
        Build Prepared_Ping_Layer from *source_layer* using column *mapping*.

        mapping keys: tower, min_r, max_r, start_az, end_az, azimuth, beam,
                      timestamp, observer_h

        When observer height is missing, *dem_layer* is sampled at each point.
        """
        if source_layer is None or not source_layer.isValid():
            raise ValueError("Source layer is invalid.")

        if _is_none_choice(mapping.get("min_r")) or _is_none_choice(mapping.get("max_r")):
            raise ValueError("Min radius and Max radius columns are required.")

        if not use_azimuth_mode:
            if _is_none_choice(mapping.get("start_az")) or _is_none_choice(mapping.get("end_az")):
                raise ValueError("Start and End azimuth columns are required in this mode.")
        else:
            if _is_none_choice(mapping.get("azimuth")):
                raise ValueError("Bearing / Azimuth column is required in this mode.")
            if azimuth_delta <= 0:
                raise ValueError("Azimuth delta must be greater than zero.")

        feat_to_coord_key, coord_key_to_point = _collect_ping_locations(source_layer)
        if not coord_key_to_point:
            raise ValueError("No valid point features found in the source layer.")

        join_fields = []
        if building_layer is not None and building_field and not _is_none_choice(building_field):
            join_fields = building_join_field_list(building_layer, building_field)

        if _is_none_choice(mapping.get("tower")):
            if not fill_tower_from_buildings or not join_fields:
                raise ValueError(
                    "Tower ID column is not mapped. Either map a tower column, or enable "
                    "'Fill Tower ID from buildings' with a building polygon layer."
                )

        cell_sites_by_location = self._resolve_cell_sites_by_location(
            source_layer=source_layer,
            feat_to_coord_key=feat_to_coord_key,
            coord_key_to_point=coord_key_to_point,
            mapping=mapping,
            building_layer=building_layer,
            building_field=building_field,
            join_fields=join_fields,
            fill_tower_from_buildings=fill_tower_from_buildings,
        )

        out_layer = QgsVectorLayer(
            f"Point?crs={DEFAULT_CRS.authid()}", PREPARED_LAYER_NAME, "memory"
        )
        provider = out_layer.dataProvider()
        provider.addAttributes(
            [
                QgsField(OUT_TOWER, QVariant.String),
                QgsField(OUT_MIN_R, QVariant.Double),
                QgsField(OUT_MAX_R, QVariant.Double),
                QgsField(OUT_START_AZ, QVariant.Double),
                QgsField(OUT_END_AZ, QVariant.Double),
                QgsField(OUT_TIMESTAMP, QVariant.String),
                QgsField(OUT_OBSERVER_H, QVariant.Double),
            ]
        )
        out_layer.updateFields()

        input_count = source_layer.featureCount()
        out_features = []
        skipped_geom = 0

        for feat in source_layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                skipped_geom += 1
                continue

            if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PointGeometry:
                skipped_geom += 1
                continue

            coord_key = feat_to_coord_key.get(feat.id())
            if not coord_key:
                skipped_geom += 1
                continue

            tower_val = cell_sites_by_location[coord_key]

            min_r = _safe_float(feat[mapping["min_r"]])
            max_r = _safe_float(feat[mapping["max_r"]])
            if min_r is None or max_r is None or math.isnan(min_r) or math.isnan(max_r):
                skipped_geom += 1
                continue

            attrs = {
                OUT_START_AZ: feat[mapping["start_az"]] if not _is_none_choice(mapping.get("start_az")) else 0,
                OUT_END_AZ: feat[mapping["end_az"]] if not _is_none_choice(mapping.get("end_az")) else 0,
                "_azimuth_raw": feat[mapping["azimuth"]] if not _is_none_choice(mapping.get("azimuth")) else 0,
            }
            start_az, end_az = _arc_angles_from_attrs(attrs, use_azimuth_mode, azimuth_delta)

            timestamp = ""
            if not _is_none_choice(mapping.get("timestamp")):
                timestamp = _format_timestamp(feat[mapping["timestamp"]])

            observer_h = None
            if not _is_none_choice(mapping.get("observer_h")):
                raw_obs = feat[mapping["observer_h"]]
                if not _is_null_value(raw_obs):
                    candidate = _safe_float(raw_obs)
                    # Ignore 0/1 flags often picked up from wrong CSV columns
                    if candidate not in (0.0, 1.0):
                        observer_h = candidate

            if observer_h is None and dem_layer is not None:
                observer_h = _sample_dem_at_point(
                    dem_layer, geom.asPoint(), source_layer.crs()
                )

            out_feat = QgsFeature(out_layer.fields())
            out_feat.setGeometry(QgsGeometry(geom))
            out_feat.setAttributes(
                [
                    tower_val,
                    min_r,
                    max_r,
                    start_az,
                    end_az,
                    timestamp,
                    observer_h,
                ]
            )
            out_features.append(out_feat)

        if time_bin_minutes > 0:
            out_features = self._apply_time_binning(out_features, time_bin_minutes, out_layer.fields())

        if not out_features:
            raise ValueError(
                "No valid point features were created. Check column mapping, coordinates, "
                "and tower ID / building join settings."
            )

        provider.addFeatures(out_features)
        out_layer.updateExtents()

        filled = sum(
            1 for f in out_features if f[OUT_OBSERVER_H] is not None and not _is_null_value(f[OUT_OBSERVER_H])
        )
        if dem_layer is not None:
            self.log(
                f"Observer height populated for {filled}/{len(out_features)} feature(s) "
                f"(CSV and/or DEM '{dem_layer.name()}')."
            )

        existing = QgsProject.instance().mapLayersByName(PREPARED_LAYER_NAME)
        for old in existing:
            QgsProject.instance().removeMapLayer(old.id())

        output_path = _prepared_ping_output_path()
        processing.run(
            "native:savefeatures",
            {
                "INPUT": out_layer,
                "OUTPUT": output_path,
                "LAYER_NAME": PREPARED_LAYER_NAME,
            },
        )
        saved_layer = QgsVectorLayer(output_path, PREPARED_LAYER_NAME, "ogr")
        if not saved_layer.isValid():
            raise RuntimeError(f"Failed to load saved prepared ping layer: {output_path}")

        QgsProject.instance().addMapLayer(saved_layer)
        self.log(f"Saved prepared ping layer to: {output_path}")
        unique_locations = len(coord_key_to_point)
        unique_sites = len({f[OUT_TOWER] for f in out_features})
        unmatched_locations = sum(
            1
            for coord_key in coord_key_to_point
            if str(cell_sites_by_location.get(coord_key, coord_key)).startswith(UNLOCATED_PREFIX)
        )
        self.log(
            f"Prepared {len(out_features)}/{input_count} ping feature(s) in "
            f"'{PREPARED_LAYER_NAME}' from {unique_locations} unique location(s) "
            f"({unique_sites} unique Cell_Site value(s), "
            f"{unmatched_locations} location(s) without building match, "
            f"{skipped_geom} skipped geometry)."
        )
        if len(out_features) < input_count:
            self.log(
                f"Warning: {input_count - len(out_features)} input row(s) were not exported. "
                "Check for invalid coordinates.",
                Qgis.Warning,
            )
        if unmatched_locations:
            self.log(
                f"{unmatched_locations} unique location(s) had no building match — assigned "
                f"'{UNLOCATED_PREFIX}_…' Cell_Site IDs. Check that ping coordinates fall on building footprints.",
                Qgis.Warning,
            )
        return saved_layer

    def _resolve_cell_sites_by_location(
        self,
        source_layer,
        feat_to_coord_key,
        coord_key_to_point,
        mapping,
        building_layer,
        building_field,
        join_fields,
        fill_tower_from_buildings,
    ):
        """
        Resolve one Cell_Site name per unique lat/lon, then reuse it for every ping.

        Building spatial join runs once on deduplicated locations — not per CSV row.
        """
        cell_sites = {}

        if not _is_none_choice(mapping.get("tower")):
            for feat in source_layer.getFeatures():
                coord_key = feat_to_coord_key.get(feat.id())
                if not coord_key or coord_key in cell_sites:
                    continue
                tower_val = str(feat[mapping["tower"]] or "").strip()
                if tower_val:
                    cell_sites[coord_key] = _normalize_tower_name(tower_val)

        if fill_tower_from_buildings and join_fields and building_layer is not None:
            unique_layer = _unique_locations_layer(coord_key_to_point)
            join_input = unique_layer
            if unique_layer.crs() != building_layer.crs():
                reproj = processing.run(
                    "native:reprojectlayer",
                    {
                        "INPUT": unique_layer,
                        "TARGET_CRS": building_layer.crs(),
                        "OUTPUT": "memory:",
                    },
                )
                join_input = reproj["OUTPUT"]
                if isinstance(join_input, str):
                    join_input = QgsVectorLayer(join_input, "join_input", "memory")

            joined = self._join_buildings(
                join_input,
                building_layer,
                join_fields,
                building_field,
            )

            matched = 0
            for feat in joined.getFeatures():
                coord_key = feat["coord_key"]
                if coord_key in cell_sites:
                    continue
                tower_val = tower_from_joined_feature(feat, building_field)
                if tower_val:
                    cell_sites[coord_key] = tower_val
                    matched += 1

            self.log(
                f"Building join: matched {matched}/{len(coord_key_to_point)} unique location(s)."
            )

        for coord_key in coord_key_to_point:
            cell_sites.setdefault(coord_key, coord_key)

        return _ensure_unique_cell_sites(cell_sites, coord_key_to_point)

    def _join_buildings(self, points, buildings, join_fields, building_field=None):
        """Join building attributes where ping intersects a building polygon."""
        context = self._processing_context_skip_invalid()
        buildings_subset = self._buildings_near_points(buildings, points)

        for attempt in range(2):
            building_layer = buildings_subset
            if attempt == 1:
                self.log(
                    "Building join hit invalid geometry — cleaning nearby buildings and retrying.",
                    Qgis.Warning,
                )
                building_layer = self._clean_vector_layer(buildings_subset, buildings.name())

            try:
                return self._run_building_join(
                    points,
                    building_layer,
                    join_fields,
                    building_field,
                    context,
                )
            except Exception as exc:
                if attempt == 0 and "invalid geometry" in str(exc).lower():
                    continue
                raise

    def _run_building_join(self, points, buildings, join_fields, building_field, context):
        joined = self._spatial_join_buildings(points, buildings, join_fields, context)

        unmatched = sum(
            1
            for feat in joined.getFeatures()
            if not tower_from_joined_feature(feat, building_field)
        )
        if unmatched:
            self.log(
                f"Building join (intersects): {unmatched} location(s) did not match "
                f"a building polygon.",
                Qgis.Warning,
            )

        return joined

    def _spatial_join_buildings(self, points, buildings, join_fields, context=None):
        """Spatial join: ping intersects building polygon."""
        result = processing.run(
            "native:joinattributesbylocation",
            {
                "INPUT": points,
                "JOIN": buildings,
                "PREDICATE": [0],  # intersects
                "JOIN_FIELDS": join_fields,
                "METHOD": 1,
                "DISCARD_NONMATCHING": False,
                "PREFIX": "bldg_",
                "OUTPUT": "memory:",
            },
            context=context or self._processing_context_skip_invalid(),
        )
        joined = result["OUTPUT"]
        return _processing_output_layer(joined, "joined_temp")
