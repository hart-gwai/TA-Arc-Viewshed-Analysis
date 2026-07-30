# -*- coding: utf-8 -*-
"""
Core spatial and processing logic for TA Arc & Viewshed Analysis.

Field names below match typical SAR cell-sector CSV exports. Adjust constants
if your CSV uses different column headers.
"""

import math
import os
import re
import traceback
from collections import defaultdict
from datetime import datetime

import processing
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

LOG_TAG = "TA Arc Viewshed Analysis"

# --- CRS ---
CRS_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
CRS_HK1980 = QgsCoordinateReferenceSystem("EPSG:2326")

# --- CSV attribute names (customize to match your ping CSV) ---
FIELD_TOWER = ("Cell_Site", "Site_ID", "Tower", "Site Name", "eNodeB", "Site")
FIELD_MIN_RADIUS = ("Min of minD", "minD", "Min_minD", "inner_radius")
FIELD_MAX_RADIUS = ("Max of maxD", "maxD", "Max_maxD", "outer_radius")
FIELD_START_AZ = ("Start_Azimuth", "StartAz", "Azimuth_Start", "start_bearing", "Min of Azimuth")
FIELD_END_AZ = ("End_Azimuth", "EndAz", "Azimuth_End", "end_bearing", "Max of Azimuth")
FIELD_AZIMUTH = ("Azimuth", "Bearing", "Direction")
FIELD_BEAM_WIDTH = ("Beam_Width", "BeamWidth", "Sector_Width", "Angle")
FIELD_TIMESTAMP = ("Timestamp", "Event_Time", "DateTime", "Time", "Ping_Time", "Minute")
FIELD_OBSERVER_H = ("observer_height", "Observer_Height", "obs_height", "height")

LAYER_UNIQUE_SITES = "Unique_Cell_Sites"
LAYER_TA_POLYGONS = "TA polygons"
LAYER_CASCADE = "TA polygon overlapped"
LEGACY_LAYER_CASCADE = "TA polygons overlaped"
GROUP_MASTER_VIEWSHEDS = "Master Viewshed"
GROUP_VIEWSHED_WITH_TA = "Viewshed with TA"
GROUP_COMBINED_VIEWSHED = "Combined Viewshed"
GROUP_TIMESTAMPED_VIEWSHEDS = "Timestamped Viewshed Layers"
VIEWSHED_OUTPUT_GROUPS = (
    GROUP_MASTER_VIEWSHEDS,
    GROUP_VIEWSHED_WITH_TA,
    GROUP_COMBINED_VIEWSHED,
)
LEGACY_MASTER_VIEWSHED_GROUPS = (
    "Master Cell Site Viewshed",
    "Master Tower Viewsheds",
)
VIEWSHED_LAYER_PREFIX = "Viewshed_"

ARC_SEGMENTS = 48
OBSERVER_HEIGHT = 1.75
TARGET_HEIGHT = 0.0


def _first_matching_field(fields, candidates):
    """Return the first field name from *candidates* present in *fields*."""
    names = {f.name() for f in fields}
    for candidate in candidates:
        if candidate in names:
            return candidate
    return None


def _safe_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_azimuth(angle):
    """Wrap angle to [0, 360)."""
    return angle % 360.0


def _tower_key(value):
    if value is None:
        return ""
    return " ".join(str(value).split()).upper()


def _safe_file_stem(value, fallback="site"):
    """Filesystem-safe stem for temp viewshed output files."""
    stem = re.sub(r"[^\w.-]+", "_", str(value).strip())
    stem = stem.strip("._")
    return (stem[:100] if stem else fallback)


def _safe_group_name(value, fallback="unknown"):
    """Legend-safe name for layer-tree groups."""
    name = re.sub(r"[^\w\s.-]+", "_", str(value).strip())
    name = " ".join(name.split())
    return (name[:80] if name else fallback)


def _viewshed_layer_names(display_towers, pocket_id):
    """Layer name: SITE or SITE1 x SITE2; file stem includes pocket id."""
    ordered = sorted(display_towers, key=lambda name: name.upper())
    if len(ordered) == 1:
        layer_name = ordered[0]
    else:
        layer_name = " x ".join(ordered)
    file_stem = _safe_file_stem(f"{layer_name}_p{pocket_id}", f"pocket_{pocket_id}")
    return layer_name, file_stem


RASTER_NODATA = -9999.0
COMBINED_EMPTY_SUBGROUP_SUFFIX = " — no line of sight"


def _read_raster_band(path):
    """Read band 1 from a GeoTIFF into a numpy array plus georeferencing."""
    from osgeo import gdal

    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    if arr is None:
        raise RuntimeError(f"Cannot read raster band: {path}")
    meta = {
        "nodata": band.GetNoDataValue(),
        "geotransform": ds.GetGeoTransform(),
        "projection": ds.GetProjection(),
    }
    ds = None
    return arr, meta


def _write_float32_raster(path, arr, meta, nodata=RASTER_NODATA):
    """Write a single-band Float32 GeoTIFF."""
    from osgeo import gdal
    import numpy as np

    data = np.asarray(arr, dtype=np.float32)
    driver = gdal.GetDriverByName("GTiff")
    rows, cols = data.shape
    out_ds = driver.Create(
        path, cols, rows, 1, gdal.GDT_Float32, options=["COMPRESS=LZW"]
    )
    out_ds.SetGeoTransform(meta["geotransform"])
    out_ds.SetProjection(meta["projection"])
    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(nodata)
    out_band.WriteArray(data)
    out_band.FlushCache()
    out_ds = None


def _array_to_binary(arr, nodata):
    """Convert visibility values to Float32 0.0 (not visible) or 1.0 (visible)."""
    import numpy as np

    data = np.asarray(arr)
    if data.size == 0:
        raise RuntimeError("Raster array is empty.")
    visible = data > 0
    if nodata is not None:
        try:
            visible = visible & (data != nodata)
        except (TypeError, ValueError):
            pass
    return np.where(visible, 1.0, 0.0).astype(np.float32)


def _raster_has_visible_cells(path):
    """True when a clipped viewshed raster contains at least one visible cell."""
    import numpy as np

    arr, meta = _read_raster_band(path)
    binary = _array_to_binary(arr, meta["nodata"])
    return bool(np.any(binary >= 0.5))


def _timestamp_subgroup_key(timestamp):
    """Legend subgroup from the pocket timestamp (first value if merged)."""
    raw = str(timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if "|" in raw:
        raw = raw.split("|", 1)[0].strip()
    return _safe_group_name(raw)


def _timestamp_sort_key(label):
    """Sort timestamp labels chronologically when parseable."""
    text = str(label or "").strip()
    if not text:
        return (1, "")
    normalized = text.replace("_", ":")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(normalized, fmt))
        except ValueError:
            continue
    return (1, text)


def _legend_subgroup_sort_key(name):
    """Chronological sort key for Step 4 legend timestamp subgroups."""
    text = str(name or "").strip()
    if COMBINED_EMPTY_SUBGROUP_SUFFIX in text:
        text = text.split(COMBINED_EMPTY_SUBGROUP_SUFFIX, 1)[0].strip()
    return _timestamp_sort_key(text)


def _repair_geometry(geom):
    """Repair invalid geometries before GEOS boolean ops (avoids hard crashes)."""
    if geom is None or geom.isEmpty():
        return geom
    if hasattr(geom, "isGeosValid") and not geom.isGeosValid():
        repaired = geom.makeValid()
        if repaired and not repaired.isEmpty():
            return repaired
    return geom


def _arc_timestamp_from_feature(feat, timestamp_field):
    """Normalize ping timestamp values to yyyy-MM-dd HH:mm:ss strings."""
    if not timestamp_field:
        return ""
    from .csv_prep_engine import _format_timestamp

    return _format_timestamp(feat[timestamp_field])


def _normalize_polygon_for_export(geom):
    """
    Reduce cascade intersection results to polygon/multipolygon geometries.

    GEOS intersections often return GeometryCollections (polygons + lines/points).
    The QGIS memory provider silently drops those, which is why only ~122/413
    pockets were being exported before normalization.
    """
    geom = _repair_geometry(geom)
    if geom is None or geom.isEmpty():
        return QgsGeometry()

    if hasattr(geom, "makeValid"):
        valid = geom.makeValid()
        if valid and not valid.isEmpty():
            geom = valid

    flat = QgsWkbTypes.flatType(geom.wkbType())

    if flat == QgsWkbTypes.GeometryCollection:
        polygons = []
        for part in geom.asGeometryCollection():
            part_norm = _normalize_polygon_for_export(part)
            if part_norm.isEmpty():
                continue
            part_flat = QgsWkbTypes.flatType(part_norm.wkbType())
            if part_flat == QgsWkbTypes.Polygon:
                polygons.append(part_norm.asPolygon())
            elif part_flat == QgsWkbTypes.MultiPolygon:
                polygons.extend(part_norm.asMultiPolygon())
        if not polygons:
            return QgsGeometry()
        return QgsGeometry.fromMultiPolygonXY(polygons)

    if flat in (QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon):
        return geom

    try:
        buffered = geom.buffer(0, 1)
        if buffered and not buffered.isEmpty():
            return _normalize_polygon_for_export(buffered)
    except Exception:
        pass

    return QgsGeometry()


def _prepare_geometry_for_export(geom):
    """Backward-compatible wrapper for cascade export normalization."""
    return _normalize_polygon_for_export(geom)


def _write_cascade_pockets_to_gpkg(pockets, output_path, layer_name):
    """
    Write cascade pockets with OGR so complex intersection geometries export reliably.
    Returns (written_count, skipped_count).
    """
    from osgeo import ogr, osr

    if os.path.isfile(output_path):
        os.remove(output_path)

    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("OGR GeoPackage driver is not available.")

    dataset = driver.CreateDataSource(output_path)
    if dataset is None:
        raise RuntimeError(f"Could not create GeoPackage: {output_path}")

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(CRS_HK1980.authid().split(":")[1]))

    ogr_layer = dataset.CreateLayer(layer_name, srs, ogr.wkbMultiPolygon)
    if ogr_layer is None:
        raise RuntimeError(f"Could not create layer '{layer_name}' in GeoPackage.")

    for field_name, field_type, width in (
        ("pocket_id", ogr.OFTInteger, 0),
        ("cascade_rule", ogr.OFTString, 80),
        ("Participating_Towers", ogr.OFTString, 254),
        ("Timestamp", ogr.OFTString, 32),
    ):
        field = ogr.FieldDefn(field_name, field_type)
        if width:
            field.SetWidth(width)
        ogr_layer.CreateField(field)

    layer_defn = ogr_layer.GetLayerDefn()
    written = 0
    skipped = 0

    for pocket_id, pocket in enumerate(pockets, start=1):
        geom = _normalize_polygon_for_export(pocket["geometry"])
        if geom.isEmpty():
            skipped += 1
            continue

        ogr_geom = ogr.CreateGeometryFromWkt(geom.asWkt())
        if ogr_geom is None:
            skipped += 1
            continue

        ogr_geom = ogr.ForceToMultiPolygon(ogr_geom)
        if ogr_geom is None or ogr_geom.IsEmpty():
            skipped += 1
            continue

        ogr_feature = ogr.Feature(layer_defn)
        ogr_feature.SetField("pocket_id", pocket_id)
        ogr_feature.SetField("cascade_rule", pocket.get("rule", ""))
        ogr_feature.SetField(
            "Participating_Towers", "|".join(pocket.get("towers", []))
        )
        ogr_feature.SetField("Timestamp", pocket.get("timestamp", ""))
        ogr_feature.SetGeometry(ogr_geom)

        if ogr_layer.CreateFeature(ogr_feature) != 0:
            skipped += 1
        else:
            written += 1

    dataset = None
    return written, skipped


def _fallback_isolated_pockets(arcs):
    """One pocket per arc when full cascade logic fails or returns nothing."""
    pockets = []
    for arc in arcs:
        geom = _repair_geometry(arc.get("geometry"))
        if geom is None or geom.isEmpty():
            continue
        pockets.append(
            {
                "geometry": geom,
                "towers": [arc["tower"]],
                "rule": "Rule 1 - No Overlap (fallback)",
                "timestamp": arc.get("timestamp", ""),
            }
        )
    return pockets


def _raster_file_path(layer):
    """Return the on-disk path for a file-backed raster layer."""
    path = layer.source()
    if path and "|" in path:
        path = path.split("|", 1)[0]
    return path


def _require_processing_algorithm(algorithm_id):
    registry = QgsApplication.processingRegistry()
    if registry is None or registry.algorithmById(algorithm_id) is None:
        raise RuntimeError(
            f"Processing algorithm '{algorithm_id}' is not available. "
            "Install the 'Visibility Analysis' plugin from the QGIS plugin manager, "
            "enable it, and restart QGIS."
        )


class TAArcViewshedEngine:
    """Stateful engine holding intermediate layers and master viewshed paths."""

    def __init__(self, iface):
        self.iface = iface
        self.master_viewshed_paths = {}  # Cell_Site -> raster file path
        self.master_viewshed_output_dir = ""
        self.viewshed_with_ta_output_dir = ""
        self.combined_viewshed_output_dir = ""
        self.timestamped_viewshed_output_dir = ""
        self.last_viewshed_ta_count = 0
        self.last_viewshed_combined_count = 0
        self.skipped_master_viewsheds = []
        self._transform_wgs84_to_hk = QgsCoordinateTransform(
            CRS_WGS84, CRS_HK1980, QgsProject.instance()
        )

    # ------------------------------------------------------------------ utils
    def log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def find_layer_by_name(self, name):
        layers = QgsProject.instance().mapLayersByName(name)
        return layers[0] if layers else None

    def find_ta_polygons_layer(self):
        """Original TA arc polygon layer from Step 3."""
        return self.find_layer_by_name(LAYER_TA_POLYGONS)

    def find_cascade_layer(self):
        """Step 3 / Step 4 cascade pocket layer (current or legacy name)."""
        for name in (LAYER_CASCADE, LEGACY_LAYER_CASCADE, "Cascade_Polygons"):
            layer = self.find_layer_by_name(name)
            if layer is not None:
                return layer
        return None

    def _project_output_dir(self):
        """Directory containing the saved QGIS project file."""
        project_dir = QgsProject.instance().absolutePath()
        if not project_dir:
            raise RuntimeError(
                "Save the QGIS project before running this step. "
                "Outputs are written next to the project file."
            )
        return project_dir

    def _unique_sites_output_path(self):
        """GeoPackage next to the project file for Step 1 unique sites."""
        return os.path.join(self._project_output_dir(), f"{LAYER_UNIQUE_SITES}.gpkg")

    def _remove_output_files(self, *paths):
        """Delete prior Step 2 outputs so GDAL/OGR can recreate them."""
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
            except OSError as exc:
                self.log(f"Could not remove stale file {path}: {exc}", Qgis.Warning)

    def _observer_point_for_dem(self, geometry, source_crs, dem_layer):
        """Return observer location and CRS authid matching the DEM."""
        pt = geometry.asPoint()
        dem_crs = dem_layer.crs()
        if source_crs != dem_crs:
            transform = QgsCoordinateTransform(
                source_crs, dem_crs, QgsProject.instance()
            )
            pt = transform.transform(pt)
        return pt, dem_crs.authid()

    def _log_tower_dem_diagnostics(self, feat, dem_layer, tower, source_crs):
        """Log tower vs DEM details to help diagnose viewshed failures."""
        from .csv_prep_engine import _sample_dem_at_point

        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            return

        pt_source = geom.asPoint()
        pt_dem, _ = self._observer_point_for_dem(geom, source_crs, dem_layer)
        layer_extent = dem_layer.extent()
        provider_extent = dem_layer.dataProvider().extent()
        elev = _sample_dem_at_point(dem_layer, pt_source, source_crs)

        self.log(
            f"DEM check for '{tower}': layer='{dem_layer.name()}', "
            f"crs={dem_layer.crs().authid()}, "
            f"tower=({pt_source.x():.2f}, {pt_source.y():.2f}) {source_crs.authid()}, "
            f"on_dem=({pt_dem.x():.2f}, {pt_dem.y():.2f}), "
            f"sample_elev={elev}, "
            f"layer_extent=({layer_extent.xMinimum():.0f}–{layer_extent.xMaximum():.0f}, "
            f"{layer_extent.yMinimum():.0f}–{layer_extent.yMaximum():.0f}), "
            f"provider_extent=({provider_extent.xMinimum():.0f}–{provider_extent.xMaximum():.0f}, "
            f"{provider_extent.yMinimum():.0f}–{provider_extent.yMaximum():.0f})"
        )

    def _load_processing_layer(self, output, name="temp"):
        """Load a processing OUTPUT value as a vector layer."""
        if isinstance(output, QgsVectorLayer):
            return output
        if isinstance(output, str):
            layer = QgsVectorLayer(output, name, "memory")
            if layer.isValid():
                return layer
            layer = QgsVectorLayer(output, name, "ogr")
            if layer.isValid():
                return layer
            raise RuntimeError(f"Failed to load processing output: {output}")
        raise RuntimeError(f"Unexpected processing output type: {type(output)!r}")

    def _master_viewshed_output_dir(self):
        """Folder next to the saved QGIS project file for Step 2 rasters."""
        output_dir = os.path.join(self._project_output_dir(), GROUP_MASTER_VIEWSHEDS)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _ta_polygons_output_path(self):
        """GeoPackage for original arc sectors before cascade (Step 3)."""
        return os.path.join(self._project_output_dir(), f"{LAYER_TA_POLYGONS}.gpkg")

    def _cascade_polygons_output_path(self):
        """GeoPackage next to the project file for Step 3 cascade pockets."""
        return os.path.join(self._project_output_dir(), f"{LAYER_CASCADE}.gpkg")

    def _viewshed_with_ta_output_dir(self):
        """Folder next to the project file for Step 4 TA polygon viewsheds."""
        output_dir = os.path.join(self._project_output_dir(), GROUP_VIEWSHED_WITH_TA)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _combined_viewshed_output_dir(self):
        """Folder next to the project file for Step 4 combined viewsheds."""
        output_dir = os.path.join(
            self._project_output_dir(), GROUP_COMBINED_VIEWSHED
        )
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _timestamped_viewshed_output_dir(self):
        """Legacy alias for combined viewshed output folder."""
        return self._combined_viewshed_output_dir()

    def _save_vector_to_gpkg(self, memory_layer, output_path, layer_name):
        """Write a memory vector layer to GeoPackage and return the file layer."""
        expected = memory_layer.featureCount()
        if os.path.isfile(output_path):
            try:
                os.remove(output_path)
            except OSError as exc:
                self.log(
                    f"Could not remove existing GeoPackage before save "
                    f"({output_path}): {exc}",
                    Qgis.Warning,
                )

        processing.run(
            "native:savefeatures",
            {
                "INPUT": memory_layer,
                "OUTPUT": output_path,
                "LAYER_NAME": layer_name,
            },
        )
        saved_layer = QgsVectorLayer(output_path, layer_name, "ogr")
        if not saved_layer.isValid():
            raise RuntimeError(f"Failed to load saved layer: {output_path}")

        actual = saved_layer.featureCount()
        if actual != expected:
            raise RuntimeError(
                f"GeoPackage save mismatch for '{layer_name}': wrote "
                f"{expected} feature(s) in memory but loaded {actual} from disk. "
                "Some cascade geometries may be invalid — check the message log."
            )
        return saved_layer

    def _ensure_group(self, group_name):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)
        return group

    def _timestamp_subgroup_insert_index(self, parent, child_name):
        """Return insert index for newest-first timestamp subgroup ordering."""
        from qgis.core import QgsLayerTreeGroup

        new_key = _legend_subgroup_sort_key(child_name)
        for index, child in enumerate(parent.children()):
            if isinstance(child, QgsLayerTreeGroup):
                if _legend_subgroup_sort_key(child.name()) < new_key:
                    return index
        return len(parent.children())

    def _insert_timestamp_subgroup(self, parent, child_name):
        """Create a timestamp subgroup at its sorted legend position."""
        child = parent.findGroup(child_name)
        if child is not None:
            return child
        index = self._timestamp_subgroup_insert_index(parent, child_name)
        return parent.insertGroup(index, child_name)

    def _ensure_subgroup(self, parent_name, child_name):
        """Return a nested subgroup, creating parent/child groups if needed."""
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup(parent_name)
        if parent is None:
            parent = root.insertGroup(0, parent_name)
        return self._insert_timestamp_subgroup(parent, child_name)

    def _prepare_timestamp_subgroups(self, parent_name, ts_keys):
        """Pre-create timestamp subgroups newest-first before adding rasters."""
        if not ts_keys:
            return
        parent = self._ensure_group(parent_name)
        for ts_key in sorted(ts_keys, key=_legend_subgroup_sort_key, reverse=True):
            self._insert_timestamp_subgroup(parent, ts_key)

    def _clear_layer_tree_group(self, group_name):
        """Remove a legend group and all layers inside it."""
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(group_name)
        if group is None:
            return
        for node in group.findLayers():
            layer = node.layer()
            if layer:
                QgsProject.instance().removeMapLayer(layer.id())
        root.removeChildNode(group)

    def _clear_legacy_cascade_groups(self):
        """Remove empty groups left by older Step 4 runs."""
        root = QgsProject.instance().layerTreeRoot()
        for child in list(root.children()):
            if not hasattr(child, "name"):
                continue
            name = child.name()
            if name.startswith("Cascade Viewsheds") or name == GROUP_TIMESTAMPED_VIEWSHEDS:
                root.removeChildNode(child)

    def _towers_from_polygon_feature(self, feat, mode):
        """Return display tower names and lookup keys from a polygon feature."""
        if mode == "ta":
            site = str(feat["Cell_Site"] or "").strip()
            if not site:
                return [], []
            return [site], [_tower_key(site)]

        towers_raw = feat["Participating_Towers"] or ""
        display_towers = [t.strip() for t in towers_raw.split("|") if t.strip()]
        return display_towers, [_tower_key(t) for t in display_towers]

    def _feature_key(self, feat, mode, index):
        """Stable numeric id for temp/output file naming."""
        if mode == "cascade" and feat.fields().indexOf("pocket_id") >= 0:
            return int(feat["pocket_id"])
        if feat.id() >= 0:
            return feat.id()
        return index + 1

    def _timestamp_from_polygon_feature(self, feat):
        """Read timestamp attribute from TA or cascade polygon features."""
        if feat.fields().indexOf("Timestamp") >= 0:
            return feat["Timestamp"]
        ts_field = _first_matching_field(feat.fields(), FIELD_TIMESTAMP)
        if ts_field:
            return feat[ts_field]
        return ""

    def _ensure_empty_combined_subgroups(self, group_name, ts_stats):
        """Legend placeholders for timestamps whose combined viewsheds are all empty."""
        parent = QgsProject.instance().layerTreeRoot().findGroup(group_name)
        if parent is None:
            return
        for ts_key, stats in ts_stats.items():
            if stats.get("visible", 0) == 0 and stats.get("empty", 0) > 0:
                label = f"{ts_key}{COMBINED_EMPTY_SUBGROUP_SUFFIX}"
                empty_group = parent.findGroup(ts_key)
                if empty_group is not None and not empty_group.findLayers():
                    parent.removeChildNode(empty_group)
                self._ensure_subgroup(group_name, label)
                self.log(
                    f"{group_name}/{label}: skipped {stats['empty']} empty "
                    "combined viewshed pocket(s)."
                )

    def _hide_viewshed_group(self, group_name):
        """Collapse viewshed output groups in the legend (hidden until user enables)."""
        group = QgsProject.instance().layerTreeRoot().findGroup(group_name)
        if group is not None:
            group.setItemVisibilityChecked(False)

    def _apply_default_multicolour_symbology(self, group_names):
        """Apply multicoloured symbology to freshly generated viewshed groups."""
        from .symbology_engine import SYMBOL_MODE_MULTI, apply_viewshed_symbology

        for group_name in group_names:
            try:
                apply_viewshed_symbology(group_name, SYMBOL_MODE_MULTI)
            except ValueError:
                pass

    def _run_viewshed_analysis_pass(
        self,
        polygon_layer,
        group_name,
        output_dir,
        mode,
        work_subdir,
        progress_callback=None,
        cancel_fn=None,
    ):
        """
        Multiply master viewsheds per polygon feature and clip to its geometry.

        mode='ta' uses original TA polygons (single tower per feature).
        mode='cascade' uses TA polygon overlapped pockets.
        """
        features = list(polygon_layer.getFeatures())
        total = len(features) or 1
        created_count = 0
        skip_empty_combined = mode == "cascade"
        ts_combined_stats = defaultdict(lambda: {"visible": 0, "empty": 0})
        work_dir = os.path.join(output_dir, work_subdir)
        os.makedirs(work_dir, exist_ok=True)

        ts_keys = {
            _timestamp_subgroup_key(self._timestamp_from_polygon_feature(feat))
            for feat in features
        }
        ts_keys.discard("")
        if not skip_empty_combined:
            self._prepare_timestamp_subgroups(group_name, ts_keys)

        for idx, feat in enumerate(features):
            if cancel_fn and cancel_fn():
                return created_count

            if progress_callback:
                progress_callback(int(100 * idx / total))

            display_towers, towers = self._towers_from_polygon_feature(feat, mode)
            if not towers:
                continue

            feature_key = self._feature_key(feat, mode, idx)
            ts_key = _timestamp_subgroup_key(self._timestamp_from_polygon_feature(feat))

            raster_paths = [
                self.master_viewshed_paths[t]
                for t in towers
                if t in self.master_viewshed_paths
            ]
            missing = [t for t in towers if t not in self.master_viewshed_paths]
            for tower in missing:
                self.log(
                    f"{group_name} feature {feature_key}: "
                    f"missing master viewshed for {tower}",
                    Qgis.Warning,
                )
            if not raster_paths:
                continue

            ts_subdir = os.path.join(output_dir, ts_key)
            os.makedirs(ts_subdir, exist_ok=True)

            bbox = feat.geometry().boundingBox()
            bbox.grow(max(bbox.width(), bbox.height()) * 0.01 + 1.0)
            projwin = (
                f"{bbox.xMinimum()},{bbox.xMaximum()},"
                f"{bbox.yMinimum()},{bbox.yMaximum()}"
            )
            layer_name, file_stem = _viewshed_layer_names(display_towers, feature_key)
            multiplied_path = os.path.join(work_dir, f"{file_stem}_multiplied.tif")
            clipped_path = os.path.join(ts_subdir, f"{file_stem}.tif")

            try:
                self._combine_viewsheds(
                    raster_paths, feature_key, projwin, work_dir, multiplied_path
                )
                mask_layer = self._vector_mask_from_feature(feat, feature_key)
                self._clip_viewshed_to_mask(
                    multiplied_path, mask_layer, clipped_path
                )

                if skip_empty_combined and not _raster_has_visible_cells(clipped_path):
                    ts_combined_stats[ts_key]["empty"] += 1
                    try:
                        os.remove(clipped_path)
                    except OSError:
                        pass
                    self.log(
                        f"{layer_name}: no visible cells in combined viewshed; "
                        f"skipped layer ({group_name}/{ts_key})."
                    )
                    continue

                subgroup = self._ensure_subgroup(group_name, ts_key)
                raster_layer = QgsRasterLayer(clipped_path, layer_name, "gdal")
                if not raster_layer.isValid():
                    raise RuntimeError(
                        f"Failed to load clipped viewshed: {clipped_path}"
                    )
                QgsProject.instance().addMapLayer(raster_layer, False)
                subgroup.addLayer(raster_layer)
                created_count += 1
                if skip_empty_combined:
                    ts_combined_stats[ts_key]["visible"] += 1
                self.log(
                    f"{layer_name}: saved to {clipped_path} and added under "
                    f"{group_name}/{ts_key}"
                )
            except Exception as exc:
                self.log(
                    f"{group_name} feature {feature_key} failed: {exc}",
                    Qgis.Warning,
                )
                self.log(traceback.format_exc(), Qgis.Warning)

        if skip_empty_combined:
            self._ensure_empty_combined_subgroups(group_name, ts_combined_stats)

        if progress_callback:
            progress_callback(100)
        return created_count

    def _add_vector_to_project(self, layer, group_name=None):
        if group_name:
            group = self._ensure_group(group_name)
            QgsProject.instance().addMapLayer(layer, False)
            group.addLayer(layer)
        else:
            QgsProject.instance().addMapLayer(layer, True)
        return layer

    def _add_layer_at_project_root(self, layer):
        """Add a layer at the top level of the legend, outside any group."""
        QgsProject.instance().addMapLayer(layer, False)
        QgsProject.instance().layerTreeRoot().insertLayer(0, layer)
        return layer

    def _add_raster_to_project(self, path, layer_name, group_name=None):
        layer = QgsRasterLayer(path, layer_name)
        if not layer.isValid():
            raise RuntimeError(f"Failed to load raster: {path}")
        if group_name:
            group = self._ensure_group(group_name)
            QgsProject.instance().addMapLayer(layer, False)
            group.addLayer(layer)
        else:
            QgsProject.instance().addMapLayer(layer, True)
        return layer

    def resolve_master_viewshed_paths(self):
        """
        Build Cell_Site -> viewshed file path mapping from the current project.

        Uses in-memory paths from Step 2 when available, otherwise loads rasters
        from the Master Viewshed group or output folder on disk.
        """
        if self.master_viewshed_paths:
            return self.master_viewshed_paths

        resolved = {}
        root = QgsProject.instance().layerTreeRoot()
        for group_name in (GROUP_MASTER_VIEWSHEDS,) + LEGACY_MASTER_VIEWSHED_GROUPS:
            group = root.findGroup(group_name)
            if group is None:
                continue
            for node in group.findLayers():
                layer = node.layer()
                if layer is None or layer.type() != QgsMapLayerType.RasterLayer:
                    continue
                name = layer.name()
                if not name.startswith(VIEWSHED_LAYER_PREFIX):
                    continue
                tower = _tower_key(name[len(VIEWSHED_LAYER_PREFIX):])
                path = _raster_file_path(layer)
                if tower and path and os.path.isfile(path):
                    resolved[tower] = path

        if not resolved:
            try:
                project_dir = self._project_output_dir()
            except RuntimeError:
                project_dir = ""

            output_dirs = []
            if project_dir:
                for name in (GROUP_MASTER_VIEWSHEDS,) + LEGACY_MASTER_VIEWSHED_GROUPS:
                    path = os.path.join(project_dir, name)
                    if os.path.isdir(path):
                        output_dirs.append(path)

            sites_layer = self.find_layer_by_name(LAYER_UNIQUE_SITES)
            for output_dir in output_dirs:
                if not sites_layer:
                    break
                tower_field = _first_matching_field(
                    sites_layer.fields(), FIELD_TOWER
                )
                if not tower_field:
                    break
                for idx, feat in enumerate(sites_layer.getFeatures()):
                    tower = _tower_key(feat[tower_field])
                    if not tower or tower in resolved:
                        continue
                    file_stem = _safe_file_stem(tower, f"site_{idx}")
                    viewshed_path = os.path.join(
                        output_dir, f"{file_stem}_viewshed.tif"
                    )
                    if os.path.isfile(viewshed_path):
                        resolved[tower] = viewshed_path

        if resolved:
            self.master_viewshed_paths.update(resolved)
            self.master_viewshed_output_dir = os.path.dirname(
                next(iter(resolved.values()))
            )
            self.log(
                f"Resolved {len(resolved)} master viewshed(s) for Step 4."
            )
        return self.master_viewshed_paths

    def _transform_point_to_hk(self, point_wgs84):
        """Transform a QgsPointXY in WGS84 to HK1980."""
        return self._transform_wgs84_to_hk.transform(point_wgs84)

    def _resolve_field_map(self, layer):
        fields = layer.fields()
        field_map = {
            "tower": _first_matching_field(fields, FIELD_TOWER),
            "min_r": _first_matching_field(fields, FIELD_MIN_RADIUS),
            "max_r": _first_matching_field(fields, FIELD_MAX_RADIUS),
            "start_az": _first_matching_field(fields, FIELD_START_AZ),
            "end_az": _first_matching_field(fields, FIELD_END_AZ),
            "azimuth": _first_matching_field(fields, FIELD_AZIMUTH),
            "beam": _first_matching_field(fields, FIELD_BEAM_WIDTH),
            "timestamp": _first_matching_field(fields, FIELD_TIMESTAMP),
            "observer_h": _first_matching_field(fields, FIELD_OBSERVER_H),
        }
        missing = [k for k in ("tower", "min_r", "max_r") if field_map[k] is None]
        if missing:
            raise ValueError(
                "Ping layer is missing required fields. Expected tower ID and radius columns. "
                f"Could not resolve: {missing}. "
                f"Available fields: {[f.name() for f in fields]}"
            )
        return field_map

    def _centroid_xy(self, points):
        """Mean location of one or more HK1980 points."""
        if not points:
            return QgsPointXY(0, 0)
        if len(points) == 1:
            return points[0]
        xs = [pt.x() for pt in points]
        ys = [pt.y() for pt in points]
        return QgsPointXY(sum(xs) / len(xs), sum(ys) / len(ys))

    def _site_lookup(self, sites_layer):
        """Build Cell_Site -> {point_hk, min_radius, max_radius} from unique sites layer."""
        tower_field = _first_matching_field(sites_layer.fields(), FIELD_TOWER)
        if not tower_field:
            raise ValueError("Unique sites layer is missing a Cell_Site field.")

        lookup = {}
        for feat in sites_layer.getFeatures():
            tower = _tower_key(feat[tower_field])
            if not tower:
                continue
            lookup[tower] = {
                "point": feat.geometry().asPoint(),
                "min_radius": float(feat["min_radius"]),
                "max_radius": float(feat["max_radius"]),
            }
        return lookup

    # --------------------------------------------------------- arc geometry
    @staticmethod
    def build_arc_sector_polygon(center, inner_r, outer_r, start_deg, end_deg, segments=ARC_SEGMENTS):
        """
        Build an annular sector polygon in projected coordinates (metres).

        Azimuth convention: 0° = North, clockwise positive (survey/GIS standard).
        """
        if outer_r <= 0:
            return QgsGeometry()
        inner_r = max(0.0, inner_r)
        if inner_r >= outer_r:
            inner_r = 0.0

        start_deg = _normalize_azimuth(start_deg)
        end_deg = _normalize_azimuth(end_deg)
        if end_deg <= start_deg:
            end_deg += 360.0

        sweep = end_deg - start_deg
        steps = max(3, int(round(segments * sweep / 360.0)))

        def _ring(radius, angle_start, angle_end, step_count, reverse=False):
            pts = []
            for i in range(step_count + 1):
                t = i / step_count
                ang = math.radians(angle_start + t * (angle_end - angle_start))
                # North-based clockwise: x = sin(az), y = cos(az)
                x = center.x() + radius * math.sin(ang)
                y = center.y() + radius * math.cos(ang)
                pts.append(QgsPointXY(x, y))
            if reverse:
                pts.reverse()
            return pts

        outer_pts = _ring(outer_r, start_deg, end_deg, steps, reverse=False)
        if inner_r > 0:
            inner_pts = _ring(inner_r, start_deg, end_deg, steps, reverse=True)
            ring = outer_pts + inner_pts + [outer_pts[0]]
        else:
            ring = [QgsPointXY(center.x(), center.y())] + outer_pts + [QgsPointXY(center.x(), center.y())]

        return QgsGeometry.fromPolygonXY([ring])

    def _arc_angles_from_feature(self, feat, field_map):
        """Resolve start/end azimuth for one ping feature."""
        if field_map["start_az"] and field_map["end_az"]:
            start_az = _safe_float(feat[field_map["start_az"]])
            end_az = _safe_float(feat[field_map["end_az"]])
            return start_az, end_az

        az = _safe_float(feat[field_map["azimuth"]]) if field_map["azimuth"] else 0.0
        beam = _safe_float(feat[field_map["beam"]], 120.0) if field_map["beam"] else 120.0
        half = beam / 2.0
        return _normalize_azimuth(az - half), _normalize_azimuth(az + half)

    def _build_arc_from_ping(self, feat, field_map, site_lookup):
        tower = _tower_key(feat[field_map["tower"]])
        if tower not in site_lookup:
            raise ValueError(f"Ping references unknown tower '{tower}'. Run Prepare Cell Site Data first.")

        site = site_lookup[tower]
        min_r = _safe_float(feat[field_map["min_r"]], site["min_radius"])
        max_r = _safe_float(feat[field_map["max_r"]], site["max_radius"])
        start_az, end_az = self._arc_angles_from_feature(feat, field_map)

        geom = self.build_arc_sector_polygon(
            site["point"], min_r, max_r, start_az, end_az
        )
        timestamp = _arc_timestamp_from_feature(feat, field_map["timestamp"])

        return {
            "tower": tower,
            "geometry": geom,
            "timestamp": timestamp,
            "min_r": min_r,
            "max_r": max_r,
            "start_az": start_az,
            "end_az": end_az,
        }

    # ----------------------------------------------------------- Step 1
    def extract_unique_sites(self, ping_layer, dem_layer=None, progress_callback=None):
        """
        Parse ping CSV layer, transform tower coordinates to EPSG:2326, and
        aggregate global Min_Radius / Max_Radius per unique Cell_Site.
        """
        from .csv_prep_engine import _is_null_value, _sample_dem_at_point

        field_map = self._resolve_field_map(ping_layer)
        tower_field = field_map["tower"]

        aggregates = {}
        total = ping_layer.featureCount() or 1
        for i, feat in enumerate(ping_layer.getFeatures()):
            if progress_callback and i % 50 == 0:
                progress_callback(int(100 * i / total))

            tower = _tower_key(feat[tower_field])
            if not tower:
                continue

            pt_wgs84 = feat.geometry().asPoint()
            pt_hk = self._transform_point_to_hk(pt_wgs84)
            min_r = _safe_float(feat[field_map["min_r"]])
            max_r = _safe_float(feat[field_map["max_r"]])
            obs_h = None
            if field_map.get("observer_h"):
                raw_obs = feat[field_map["observer_h"]]
                if not _is_null_value(raw_obs):
                    candidate = _safe_float(raw_obs)
                    if candidate not in (0.0, 1.0):
                        obs_h = candidate

            if tower not in aggregates:
                aggregates[tower] = {
                    "points": [pt_hk],
                    "min_radius": min_r,
                    "max_radius": max_r,
                    "observer_height": obs_h,
                }
            else:
                aggregates[tower]["points"].append(pt_hk)
                aggregates[tower]["min_radius"] = min(
                    aggregates[tower]["min_radius"], min_r
                )
                aggregates[tower]["max_radius"] = max(
                    aggregates[tower]["max_radius"], max_r
                )
                if obs_h is not None:
                    existing_obs = aggregates[tower].get("observer_height")
                    aggregates[tower]["observer_height"] = (
                        max(existing_obs, obs_h) if existing_obs is not None else obs_h
                    )

        if dem_layer is not None:
            for data in aggregates.values():
                if data.get("observer_height") is None:
                    centroid = self._centroid_xy(data["points"])
                    data["observer_height"] = _sample_dem_at_point(
                        dem_layer, centroid, CRS_HK1980
                    )

        if not aggregates:
            raise ValueError("No valid ping features found for unique site extraction.")

        existing = self.find_layer_by_name(LAYER_UNIQUE_SITES)
        if existing:
            QgsProject.instance().removeMapLayer(existing.id())

        layer = QgsVectorLayer(
            f"Point?crs={CRS_HK1980.authid()}", LAYER_UNIQUE_SITES, "memory"
        )
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField(tower_field, QVariant.String),
                QgsField("min_radius", QVariant.Double),
                QgsField("max_radius", QVariant.Double),
                QgsField("observer_height", QVariant.Double),
            ]
        )
        layer.updateFields()

        features = []
        for tower, data in sorted(aggregates.items()):
            centroid = self._centroid_xy(data["points"])
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(centroid))
            feat.setAttributes(
                [
                    tower,
                    data["min_radius"],
                    data["max_radius"],
                    data.get("observer_height"),
                ]
            )
            features.append(feat)

        provider.addFeatures(features)
        layer.updateExtents()

        output_path = self._unique_sites_output_path()
        saved_layer = self._save_vector_to_gpkg(layer, output_path, LAYER_UNIQUE_SITES)
        self._add_vector_to_project(saved_layer)
        self.log(f"Saved unique sites to: {output_path}")

        if progress_callback:
            progress_callback(100)

        self.log(
            f"Unique sites: {len(features)} towers; "
            f"global min radius range computed per tower."
        )
        return saved_layer

    # ----------------------------------------------------------- Step 2
    def generate_master_viewsheds(self, sites_layer, dem_layer, progress_callback=None, cancel_fn=None):
        """
        Generate one 360° donut viewshed per tower using visibility algorithms.
        RADIUS_IN = min_radius (inner dead-zone), RADIUS_OBS = max_radius.
        """
        self.master_viewshed_paths.clear()
        self.skipped_master_viewsheds = []
        output_dir = self._master_viewshed_output_dir()
        self.master_viewshed_output_dir = output_dir
        self.log(f"Writing master viewsheds to: {output_dir}")
        _require_processing_algorithm("visibility:createviewpoints")
        _require_processing_algorithm("visibility:viewshed")
        if dem_layer.crs().mapUnits() != 0:
            raise ValueError(
                "DEM must use a projected CRS in metres (e.g. EPSG:2326). "
                f"Current DEM CRS: {dem_layer.crs().authid()}"
            )

        features = list(sites_layer.getFeatures())
        total = len(features) or 1
        sites_crs = sites_layer.crs()
        tower_field = _first_matching_field(sites_layer.fields(), FIELD_TOWER)
        if not tower_field:
            raise ValueError("Unique sites layer is missing a Cell_Site field.")

        for idx, feat in enumerate(features):
            if cancel_fn and cancel_fn():
                return False

            if progress_callback:
                progress_callback(int(100 * idx / total))

            tower = _tower_key(feat[tower_field])
            file_stem = _safe_file_stem(tower, f"site_{idx}")
            min_radius = float(feat["min_radius"])
            max_radius = float(feat["max_radius"])
            observer_height = OBSERVER_HEIGHT
            if feat.fields().indexOf("observer_height") >= 0:
                val = feat["observer_height"]
                if val is not None:
                    try:
                        candidate = float(val)
                        # DEM ground elevation is often sampled here; keep default
                        # observer height above terrain unless a realistic AGL value exists.
                        if 0 < candidate < 200:
                            observer_height = candidate
                    except (TypeError, ValueError):
                        pass

            if max_radius <= 0:
                self.skipped_master_viewsheds.append(
                    (tower, f"invalid max_radius {max_radius}")
                )
                self.log(
                    f"Skipping tower '{tower}': invalid max_radius {max_radius}.",
                    Qgis.Warning,
                )
                continue
            if min_radius >= max_radius:
                min_radius = 0.0

            viewshed_path = os.path.normpath(
                os.path.join(output_dir, f"{file_stem}_viewshed.tif")
            )
            self._remove_output_files(viewshed_path)

            self._log_tower_dem_diagnostics(feat, dem_layer, tower, sites_crs)

            obs_pt, obs_crs = self._observer_point_for_dem(
                feat.geometry(), sites_crs, dem_layer
            )
            vp_input = None
            last_error = None
            radius_attempts = [min_radius]
            if min_radius > 0:
                radius_attempts.append(0.0)

            for attempt_min_r in radius_attempts:
                if attempt_min_r != min_radius:
                    self.log(
                        f"Retrying '{tower}' with RADIUS_IN=0 "
                        f"(original inner radius {min_radius} m failed).",
                        Qgis.Warning,
                    )

                attempt_layer = QgsVectorLayer(
                    f"Point?crs={obs_crs}", f"vp_{file_stem}_{attempt_min_r}", "memory"
                )
                attempt_provider = attempt_layer.dataProvider()
                attempt_provider.addAttributes([QgsField("radius_in", QVariant.Double)])
                attempt_layer.updateFields()
                attempt_feat = QgsFeature(attempt_layer.fields())
                attempt_feat.setGeometry(QgsGeometry.fromPointXY(obs_pt))
                attempt_feat.setAttributes([attempt_min_r])
                attempt_provider.addFeatures([attempt_feat])
                attempt_layer.updateExtents()

                try:
                    create_params = {
                        "OBSERVER_POINTS": attempt_layer,
                        "DEM": dem_layer,
                        "RADIUS": max_radius,
                        "OBS_HEIGHT": observer_height,
                        "TARGET_HEIGHT": TARGET_HEIGHT,
                        "OUTPUT": "memory:",
                    }
                    if attempt_min_r > 0:
                        create_params["RADIUS_IN_FIELD"] = "radius_in"

                    vp_result = processing.run(
                        "visibility:createviewpoints",
                        create_params,
                    )

                    candidate = self._load_processing_layer(
                        vp_result.get("OUTPUT"), f"viewpoints_{file_stem}"
                    )
                    if candidate.featureCount() == 0:
                        raise RuntimeError(
                            "No viewpoints in the chosen area (outside DEM or nodata)."
                        )
                    vp_input = candidate
                    break
                except Exception as exc:
                    last_error = exc

            try:
                if vp_input is None:
                    raise last_error or RuntimeError(
                        "Could not create viewpoints for this tower."
                    )

                processing.run(
                    "visibility:viewshed",
                    {
                        "DEM": dem_layer,
                        "OBSERVER_POINTS": vp_input,
                        "OUTPUT": viewshed_path,
                        "ANALYSIS_TYPE": 0,
                        "OPERATOR": 0,
                        "USE_CURVATURE": False,
                        "REFRACTION": 0.13,
                    },
                )
            except Exception as exc:
                self.skipped_master_viewsheds.append((tower, str(exc)))
                self.log(
                    f"Skipping tower '{tower}' "
                    f"(RADIUS_IN={min_radius}, RADIUS_OBS={max_radius}): {exc}",
                    Qgis.Warning,
                )
                continue

            if not os.path.isfile(viewshed_path):
                self.skipped_master_viewsheds.append(
                    (tower, "viewshed output file was not created")
                )
                self.log(
                    f"Skipping tower '{tower}': viewshed output was not created.",
                    Qgis.Warning,
                )
                continue

            self.master_viewshed_paths[tower] = viewshed_path
            self._add_raster_to_project(
                viewshed_path,
                f"Viewshed_{tower}",
                GROUP_MASTER_VIEWSHEDS,
            )
            self.log(
                f"Master viewshed for {tower}: RADIUS_IN={min_radius}, RADIUS_OBS={max_radius}"
            )

        if progress_callback:
            progress_callback(100)

        if not self.master_viewshed_paths:
            details = "; ".join(
                f"{name} ({reason})" for name, reason in self.skipped_master_viewsheds[:5]
            )
            if len(self.skipped_master_viewsheds) > 5:
                details += f"; …and {len(self.skipped_master_viewsheds) - 5} more"
            raise RuntimeError(
                "No master viewsheds were created. "
                f"All {len(self.skipped_master_viewsheds)} tower(s) were skipped. {details}"
            )

        if self.skipped_master_viewsheds:
            self.log(
                f"Step 2 finished with {len(self.master_viewshed_paths)} viewshed(s); "
                f"skipped {len(self.skipped_master_viewsheds)} tower(s).",
                Qgis.Warning,
            )
        self._hide_viewshed_group(GROUP_MASTER_VIEWSHEDS)
        self._apply_default_multicolour_symbology([GROUP_MASTER_VIEWSHEDS])
        return True

    # ----------------------------------------------------------- Step 3
    def run_cascade_polygons(self, ping_layer, sites_layer, progress_callback=None):
        """
        Translate CSV arcs to curved polygons and apply 3-tier cascade logic:

        Rule 1 — Only when no arc polygons overlap in the timestamp: keep every arc.
        Rule 2 — When at least one overlap exists: keep pairwise overlap pockets only.
        Rule 3 — When multiple overlap pockets intersect: add mutual-core pockets;
                 lone non-overlapping arc areas are discarded so overlap data is kept.
        """
        field_map = self._resolve_field_map(ping_layer)
        site_lookup = self._site_lookup(sites_layer)
        ping_count = ping_layer.featureCount()
        self.log(f"Step 3 input: {ping_count} ping feature(s) in '{ping_layer.name()}'.")

        arcs = []
        unknown_towers = set()
        total = ping_layer.featureCount() or 1
        for i, feat in enumerate(ping_layer.getFeatures()):
            if progress_callback and i % 25 == 0:
                progress_callback(int(30 * i / total))
            tower = _tower_key(feat[field_map["tower"]])
            if tower not in site_lookup:
                unknown_towers.add(str(feat[field_map["tower"]] or tower))
                continue
            arc = self._build_arc_from_ping(feat, field_map, site_lookup)
            if arc["geometry"] and not arc["geometry"].isEmpty():
                arc["geometry"] = _repair_geometry(arc["geometry"])
                if not arc["geometry"].isEmpty():
                    arcs.append(arc)

        if unknown_towers:
            sample = ", ".join(sorted(unknown_towers)[:8])
            extra = "" if len(unknown_towers) <= 8 else f" (+{len(unknown_towers) - 8} more)"
            raise ValueError(
                f"{len(unknown_towers)} ping(s) reference tower(s) not in Unique Cell Sites "
                f"({sample}{extra}). Re-run Prepare Cell Site Data (Step 1) after updating the CSV."
            )

        if not arcs:
            raise ValueError("No arc geometries could be built from ping layer.")

        arc_timestamps = sorted(
            {str(arc.get("timestamp") or "").strip() or "Unknown" for arc in arcs},
            key=_timestamp_sort_key,
        )
        self.log(
            f"Built {len(arcs)} arc(s); timestamp range "
            f"{arc_timestamps[0]} -> {arc_timestamps[-1]} ({len(arc_timestamps)} groups)."
        )

        if progress_callback:
            progress_callback(35)

        by_timestamp = defaultdict(list)
        for arc in arcs:
            ts = str(arc.get("timestamp") or "").strip() or "Unknown"
            by_timestamp[ts].append(arc)

        ts_groups = sorted(by_timestamp.items(), key=lambda item: _timestamp_sort_key(item[0]))
        self.log(
            f"Cascade analysis: {len(arcs)} arc(s) across {len(ts_groups)} timestamp group(s)."
        )

        pockets = []
        failed_groups = []
        for gi, (ts_label, ts_arcs) in enumerate(ts_groups):
            if progress_callback:
                progress_callback(40 + int(30 * (gi + 1) / max(len(ts_groups), 1)))
            try:
                group_pockets = self._apply_cascade_logic(ts_arcs)
                if not group_pockets:
                    self.log(
                        f"  {ts_label}: cascade returned 0 pockets for "
                        f"{len(ts_arcs)} arc(s); using fallback.",
                        Qgis.Warning,
                    )
                    group_pockets = _fallback_isolated_pockets(ts_arcs)
            except Exception as exc:
                failed_groups.append(ts_label)
                self.log(
                    f"  {ts_label}: cascade failed ({exc}); using fallback for "
                    f"{len(ts_arcs)} arc(s).",
                    Qgis.Critical,
                )
                self.log(traceback.format_exc(), Qgis.Warning)
                group_pockets = _fallback_isolated_pockets(ts_arcs)

            pockets.extend(group_pockets)
            self.log(
                f"  {ts_label}: {len(ts_arcs)} arc(s) -> {len(group_pockets)} pocket(s)."
            )

        if failed_groups:
            self.log(
                f"Cascade used fallback for {len(failed_groups)} timestamp group(s): "
                f"{', '.join(failed_groups[:5])}"
                + (f" (+{len(failed_groups) - 5} more)" if len(failed_groups) > 5 else ""),
                Qgis.Warning,
            )

        pocket_timestamps = sorted(
            {str(p.get("timestamp") or "").strip() or "Unknown" for p in pockets},
            key=_timestamp_sort_key,
        )
        if pocket_timestamps:
            self.log(
                f"Cascade output timestamps: {pocket_timestamps[0]} -> "
                f"{pocket_timestamps[-1]} ({len(pocket_timestamps)} distinct value(s))."
            )

        if progress_callback:
            progress_callback(70)

        for name in (LAYER_TA_POLYGONS, LAYER_CASCADE, LEGACY_LAYER_CASCADE, "Cascade_Polygons"):
            existing = self.find_layer_by_name(name)
            if existing:
                QgsProject.instance().removeMapLayer(existing.id())

        original_layer = self._build_original_ta_polygons_layer(arcs)
        original_path = self._ta_polygons_output_path()
        saved_original = self._save_vector_to_gpkg(
            original_layer, original_path, LAYER_TA_POLYGONS
        )
        self._add_layer_at_project_root(saved_original)
        self.log(f"Saved original TA polygons to: {original_path}")

        output_path = self._cascade_polygons_output_path()
        written, skipped = _write_cascade_pockets_to_gpkg(
            pockets, output_path, LAYER_CASCADE
        )
        if skipped:
            self.log(
                f"Skipped {skipped} cascade pocket(s) that could not be converted "
                "to exportable polygons.",
                Qgis.Warning,
            )

        saved_layer = QgsVectorLayer(output_path, LAYER_CASCADE, "ogr")
        if not saved_layer.isValid():
            raise RuntimeError(f"Failed to load saved cascade layer: {output_path}")

        loaded_count = saved_layer.featureCount()
        if loaded_count != written:
            raise RuntimeError(
                f"Cascade GeoPackage mismatch: wrote {written} feature(s) but "
                f"loaded {loaded_count} from {output_path}."
            )

        self._add_layer_at_project_root(saved_layer)
        self.log(f"Saved cascade polygons to: {output_path}")

        if progress_callback:
            progress_callback(100)

        self.log(
            f"Cascade polygons: {loaded_count} pocket(s) exported; "
            f"{len(arcs)} original arc polygon(s)."
        )
        return saved_layer

    def _build_original_ta_polygons_layer(self, arcs):
        """One feature per ping arc before cascade intersection."""
        layer = QgsVectorLayer(
            f"Polygon?crs={CRS_HK1980.authid()}", LAYER_TA_POLYGONS, "memory"
        )
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("Cell_Site", QVariant.String),
                QgsField("Timestamp", QVariant.String),
                QgsField("min_radius", QVariant.Double),
                QgsField("max_radius", QVariant.Double),
                QgsField("start_azimuth", QVariant.Double),
                QgsField("end_azimuth", QVariant.Double),
            ]
        )
        layer.updateFields()

        features = []
        for arc in arcs:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(arc["geometry"])
            feat.setAttributes(
                [
                    arc["tower"],
                    arc.get("timestamp", ""),
                    arc["min_r"],
                    arc["max_r"],
                    arc["start_az"],
                    arc["end_az"],
                ]
            )
            features.append(feat)

        provider.addFeatures(features)
        layer.updateExtents()
        return layer

    def _apply_cascade_logic(self, arcs):
        """
        Build pocket list from arc sectors using 3-tier cascade rules.

        Rule 1 applies only when no overlaps exist in the timestamp (keep all arcs).
        When any overlap exists, only Rule 2/3 overlap pockets are kept; lone arcs
        and non-overlapping arc remnants are discarded.
        """
        n = len(arcs)
        for arc in arcs:
            arc["geometry"] = _repair_geometry(arc["geometry"])

        def _intersection_geom(indices):
            geom = QgsGeometry(arcs[indices[0]]["geometry"])
            for idx in indices[1:]:
                geom = _repair_geometry(geom.intersection(arcs[idx]["geometry"]))
                if geom.isEmpty():
                    break
            return geom

        # --- Rule 2: primary pairwise intersections ---
        primary_pockets = []
        for i in range(n):
            for j in range(i + 1, n):
                inter = _repair_geometry(
                    arcs[i]["geometry"].intersection(arcs[j]["geometry"])
                )
                if inter.isEmpty():
                    continue
                towers = sorted({arcs[i]["tower"], arcs[j]["tower"]})
                primary_pockets.append(
                    {
                        "geometry": inter,
                        "towers": towers,
                        "rule": "Rule 2 - Primary Overlap",
                        "timestamp": self._merge_timestamps(arcs[i], arcs[j]),
                        "arc_indices": [i, j],
                    }
                )

        # --- Rule 3: overlapping primaries collapse to mutual core ---
        absorbed_primary = set()
        secondary_pockets = []

        for i in range(len(primary_pockets)):
            if i in absorbed_primary:
                continue
            cluster = {i}
            changed = True
            while changed:
                changed = False
                for j in range(len(primary_pockets)):
                    if j in cluster or j in absorbed_primary:
                        continue
                    for k in cluster:
                        if primary_pockets[j]["geometry"].intersects(
                            primary_pockets[k]["geometry"]
                        ):
                            cluster.add(j)
                            changed = True
                            break

            if len(cluster) < 2:
                continue

            # Collect all unique towers and arc indices in this primary cluster
            all_towers = set()
            arc_indices = set()
            timestamps = []
            for k in cluster:
                all_towers.update(primary_pockets[k]["towers"])
                arc_indices.update(primary_pockets[k]["arc_indices"])
                if primary_pockets[k]["timestamp"]:
                    timestamps.append(primary_pockets[k]["timestamp"])

            core = _intersection_geom(sorted(arc_indices))
            if core.isEmpty():
                continue

            secondary_pockets.append(
                {
                    "geometry": core,
                    "towers": sorted(all_towers),
                    "rule": "Rule 3 - Secondary Overlap",
                    "timestamp": "|".join(sorted(set(timestamps))),
                }
            )
            absorbed_primary.update(cluster)

        surviving_primaries = [
            p for idx, p in enumerate(primary_pockets) if idx not in absorbed_primary
        ]

        if not primary_pockets:
            # Rule 1 — no overlaps in this timestamp: keep every full arc.
            isolated_pockets = []
            for arc in arcs:
                geom = _repair_geometry(arc["geometry"])
                if geom.isEmpty():
                    continue
                isolated_pockets.append(
                    {
                        "geometry": geom,
                        "towers": [arc["tower"]],
                        "rule": "Rule 1 - No Overlap",
                        "timestamp": arc.get("timestamp", ""),
                    }
                )
            return isolated_pockets

        # Rule 2 + Rule 3 — overlaps exist: keep overlap pockets only, discard lone arcs.
        return surviving_primaries + secondary_pockets

    @staticmethod
    def _merge_timestamps(arc_a, arc_b):
        ts = sorted({arc_a.get("timestamp", ""), arc_b.get("timestamp", "")} - {""})
        return "|".join(ts)

    def _vector_mask_from_feature(self, feat, pocket_id):
        """In-memory clip mask — avoids Windows file-lock issues with GeoPackage."""
        mask = QgsVectorLayer(
            f"Polygon?crs={CRS_HK1980.authid()}", f"mask_{pocket_id}", "memory"
        )
        mask.dataProvider().addFeatures([QgsFeature(feat)])
        mask.updateExtents()
        if mask.featureCount() == 0:
            raise RuntimeError(f"Clip mask for pocket {pocket_id} has no geometry.")
        return mask

    def _run_processing(self, algorithm, params):
        """Run a processing algorithm and surface useful errors."""
        try:
            return processing.run(algorithm, params)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def _clip_raster_to_extent(self, input_path, projwin, output_path):
        self._run_processing(
            "gdal:cliprasterbyextent",
            {
                "INPUT": input_path,
                "PROJWIN": projwin,
                "NODATA": -9999,
                "OUTPUT": output_path,
            },
        )
        if not os.path.isfile(output_path):
            raise RuntimeError(f"Extent clip failed: {input_path}")

    def _normalize_to_binary(self, input_path, output_path):
        """Convert any viewshed raster to Float32 with values 0.0 or 1.0."""
        arr, meta = _read_raster_band(input_path)
        binary = _array_to_binary(arr, meta["nodata"])
        _write_float32_raster(output_path, binary, meta, nodata=RASTER_NODATA)
        if not os.path.isfile(output_path):
            raise RuntimeError(f"Binary normalization failed: {input_path}")

    def _combine_viewsheds(self, raster_paths, pocket_id, projwin, work_dir, output_path):
        """Intersect viewsheds within a pocket extent as Float32 0/1."""
        binary_layers = []
        meta = None
        for i, path in enumerate(raster_paths):
            clipped = os.path.join(work_dir, f"p{pocket_id}_src{i}.tif")
            self._clip_raster_to_extent(path, projwin, clipped)
            arr, clip_meta = _read_raster_band(clipped)
            binary = _array_to_binary(arr, clip_meta["nodata"])
            binary_layers.append(binary)
            if meta is None:
                meta = clip_meta

        if meta is None:
            raise RuntimeError("No viewshed rasters were clipped for combination.")

        combined = binary_layers[0].copy()
        for layer_arr in binary_layers[1:]:
            if layer_arr.shape != combined.shape:
                raise RuntimeError(
                    f"Pocket {pocket_id}: clipped viewshed dimensions do not match."
                )
            combined = combined * layer_arr

        _write_float32_raster(output_path, combined, meta, nodata=RASTER_NODATA)

    def _clip_viewshed_to_mask(self, input_path, mask_layer, output_path):
        clipped_temp = f"{output_path}.clip.tif"
        self._run_processing(
            "gdal:cliprasterbymasklayer",
            {
                "INPUT": input_path,
                "MASK": mask_layer,
                "SOURCE_CRS": CRS_HK1980,
                "TARGET_CRS": CRS_HK1980,
                "NODATA": -9999,
                "ALPHA_BAND": False,
                "CROP_TO_CUTLINE": True,
                "KEEP_RESOLUTION": True,
                "OPTIONS": "",
                "DATA_TYPE": 5,  # Float32
                "OUTPUT": clipped_temp,
            },
        )

        if not os.path.isfile(clipped_temp):
            raise RuntimeError(f"Clip did not create output: {clipped_temp}")

        self._normalize_to_binary(clipped_temp, output_path)
        if os.path.isfile(clipped_temp):
            try:
                os.remove(clipped_temp)
            except OSError:
                pass

        clipped = QgsRasterLayer(output_path, os.path.basename(output_path), "gdal")
        if not clipped.isValid():
            raise RuntimeError(f"Clipped raster is invalid: {output_path}")

    # ----------------------------------------------------------- Step 4
    def multiply_and_crop_rasters(
        self,
        ta_polygons_layer,
        cascade_layer,
        dem_layer,
        progress_callback=None,
        cancel_fn=None,
    ):
        """
        Run viewshed analysis twice:

        1. Clip/combine master viewsheds to each original TA polygon →
           group ``Viewshed with TA`` (timestamp subgroups).
        2. Clip/combine master viewsheds to each overlapped cascade pocket →
           group ``Combined Viewshed`` (timestamp subgroups).
        """
        del dem_layer  # retained for API compatibility with the dialog

        self.resolve_master_viewshed_paths()
        if not self.master_viewshed_paths:
            raise RuntimeError(
                f"No master viewshed rasters found. Run Step 2 or load rasters "
                f"from '{GROUP_MASTER_VIEWSHEDS}'."
            )

        self.viewshed_with_ta_output_dir = self._viewshed_with_ta_output_dir()
        self.combined_viewshed_output_dir = self._combined_viewshed_output_dir()
        self.timestamped_viewshed_output_dir = self.combined_viewshed_output_dir

        for group_name in (
            GROUP_VIEWSHED_WITH_TA,
            GROUP_COMBINED_VIEWSHED,
            GROUP_TIMESTAMPED_VIEWSHEDS,
        ):
            self._clear_layer_tree_group(group_name)
        self._clear_legacy_cascade_groups()

        self.log(
            f"Viewshed analysis: writing TA outputs to {self.viewshed_with_ta_output_dir} "
            f"and combined outputs to {self.combined_viewshed_output_dir}"
        )

        def _scaled_progress(offset, scale):
            def _emit(value):
                if progress_callback:
                    progress_callback(offset + int(value * scale / 100))

            return _emit

        ta_count = self._run_viewshed_analysis_pass(
            ta_polygons_layer,
            GROUP_VIEWSHED_WITH_TA,
            self.viewshed_with_ta_output_dir,
            mode="ta",
            work_subdir="_work_ta",
            progress_callback=_scaled_progress(0, 50),
            cancel_fn=cancel_fn,
        )
        if cancel_fn and cancel_fn():
            return False

        combined_count = self._run_viewshed_analysis_pass(
            cascade_layer,
            GROUP_COMBINED_VIEWSHED,
            self.combined_viewshed_output_dir,
            mode="cascade",
            work_subdir="_work_combined",
            progress_callback=_scaled_progress(50, 50),
            cancel_fn=cancel_fn,
        )

        if progress_callback:
            progress_callback(100)

        if ta_count == 0 and combined_count == 0:
            raise RuntimeError(
                "No viewshed layers were created. "
                "Check the QGIS message log for per-feature errors."
            )

        self.last_viewshed_ta_count = ta_count
        self.last_viewshed_combined_count = combined_count
        self.log(
            f"Viewshed analysis complete: {ta_count} layer(s) in "
            f"'{GROUP_VIEWSHED_WITH_TA}', {combined_count} layer(s) in "
            f"'{GROUP_COMBINED_VIEWSHED}'."
        )
        for group_name in (GROUP_VIEWSHED_WITH_TA, GROUP_COMBINED_VIEWSHED):
            self._hide_viewshed_group(group_name)
        self._apply_default_multicolour_symbology(
            [GROUP_VIEWSHED_WITH_TA, GROUP_COMBINED_VIEWSHED]
        )
        return True


# ====================================================================== Tasks
class ViewshedGenerationTask(QgsTask):
    """Background task wrapper for Step 2 viewshed generation."""

    def __init__(self, description, sites_layer, dem_layer, engine, progress_callback=None):
        super().__init__(description, QgsTask.CanCancel)
        self.sites_layer = sites_layer
        self.dem_layer = dem_layer
        self.engine = engine
        self.progress_callback = progress_callback
        self.success = False
        self.error_message = ""

    def run(self):
        try:
            self.success = self.engine.generate_master_viewsheds(
                self.sites_layer,
                self.dem_layer,
                progress_callback=self._emit_progress,
                cancel_fn=self.isCanceled,
            )
            return self.success
        except Exception as exc:
            self.error_message = str(exc)
            self.engine.log(traceback.format_exc(), Qgis.Critical)
            return False

    def _emit_progress(self, value):
        if self.progress_callback:
            self.progress_callback(value)

    def finished(self, result):
        if result and self.success:
            self.engine.log("Master viewshed generation completed.")
        elif not self.isCanceled():
            self.engine.log("Master viewshed generation failed.", Qgis.Warning)


class RasterMultiplyTask(QgsTask):
    """Background task wrapper for Step 4 raster multiply and clip."""

    def __init__(self, description, cascade_layer, dem_layer, engine, progress_callback=None, ta_polygons_layer=None):
        super().__init__(description, QgsTask.CanCancel)
        self.ta_polygons_layer = ta_polygons_layer
        self.cascade_layer = cascade_layer
        self.dem_layer = dem_layer
        self.engine = engine
        self.progress_callback = progress_callback
        self.success = False
        self.error_message = ""

    def run(self):
        try:
            self.success = self.engine.multiply_and_crop_rasters(
                self.ta_polygons_layer,
                self.cascade_layer,
                self.dem_layer,
                progress_callback=self._emit_progress,
                cancel_fn=self.isCanceled,
            )
            return self.success
        except Exception as exc:
            self.error_message = str(exc)
            self.engine.log(traceback.format_exc(), Qgis.Critical)
            return False

    def _emit_progress(self, value):
        if self.progress_callback:
            self.progress_callback(value)

    def finished(self, result):
        if result and self.success:
            self.engine.log("Raster multiply and crop completed.")
        elif not self.isCanceled():
            self.engine.log("Raster multiply and crop failed.", Qgis.Warning)
