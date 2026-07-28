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
LAYER_CASCADE = "TA polygons overlaped"
GROUP_MASTER_VIEWSHEDS = "Master Cell Site Viewshed"
GROUP_TIMESTAMPED_VIEWSHEDS = "Timestamped Viewshed Layers"
LEGACY_GROUP_MASTER_VIEWSHEDS = "Master Tower Viewsheds"
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


def _viewshed_layer_names(towers, pocket_id):
    """Layer name: SITE or SITE1 x SITE2; file stem includes pocket id."""
    ordered = sorted(towers)
    if len(ordered) == 1:
        layer_name = ordered[0]
    else:
        layer_name = " x ".join(ordered)
    file_stem = _safe_file_stem(f"{layer_name}_p{pocket_id}", f"pocket_{pocket_id}")
    return layer_name, file_stem


def _timestamp_subgroup_key(timestamp):
    """Legend subgroup from the pocket timestamp (first value if merged)."""
    raw = str(timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if "|" in raw:
        raw = raw.split("|", 1)[0].strip()
    return _safe_group_name(raw)


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
        self.timestamped_viewshed_output_dir = ""
        self._transform_wgs84_to_hk = QgsCoordinateTransform(
            CRS_WGS84, CRS_HK1980, QgsProject.instance()
        )

    # ------------------------------------------------------------------ utils
    def log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def find_layer_by_name(self, name):
        layers = QgsProject.instance().mapLayersByName(name)
        return layers[0] if layers else None

    def _master_viewshed_output_dir(self):
        """Folder next to the saved QGIS project file for Step 2 rasters."""
        project_dir = QgsProject.instance().absolutePath()
        if not project_dir:
            raise RuntimeError(
                "Save the QGIS project before running Step 2. "
                f"Viewshed rasters are written next to the project file in "
                f"'{GROUP_MASTER_VIEWSHEDS}'."
            )
        output_dir = os.path.join(project_dir, GROUP_MASTER_VIEWSHEDS)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _cascade_polygons_output_path(self):
        """GeoPackage next to the saved QGIS project file for Step 3 polygons."""
        project_dir = QgsProject.instance().absolutePath()
        if not project_dir:
            raise RuntimeError(
                "Save the QGIS project before running Step 3. "
                f"'{LAYER_CASCADE}' is written next to the project file."
            )
        return os.path.join(project_dir, f"{LAYER_CASCADE}.gpkg")

    def _timestamped_viewshed_output_dir(self):
        """Folder next to the saved QGIS project file for Step 4 rasters."""
        project_dir = QgsProject.instance().absolutePath()
        if not project_dir:
            raise RuntimeError(
                "Save the QGIS project before running Step 4. "
                f"Outputs are written next to the project file in "
                f"'{GROUP_TIMESTAMPED_VIEWSHEDS}'."
            )
        output_dir = os.path.join(project_dir, GROUP_TIMESTAMPED_VIEWSHEDS)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def _ensure_group(self, group_name):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)
        return group

    def _ensure_subgroup(self, parent_name, child_name):
        """Return a nested subgroup, creating parent/child groups if needed."""
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup(parent_name)
        if parent is None:
            parent = root.insertGroup(0, parent_name)
        child = parent.findGroup(child_name)
        if child is None:
            child = parent.addGroup(child_name)
        return child

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
            if child.name().startswith("Cascade Viewsheds"):
                root.removeChildNode(child)

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
        from the Master Cell Site Viewshed group or output folder on disk.
        """
        if self.master_viewshed_paths:
            return self.master_viewshed_paths

        resolved = {}
        root = QgsProject.instance().layerTreeRoot()
        for group_name in (GROUP_MASTER_VIEWSHEDS, LEGACY_GROUP_MASTER_VIEWSHEDS):
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
                output_dir = self._master_viewshed_output_dir()
            except RuntimeError:
                output_dir = ""

            if output_dir and os.path.isdir(output_dir):
                sites_layer = self.find_layer_by_name(LAYER_UNIQUE_SITES)
                if sites_layer:
                    tower_field = _first_matching_field(
                        sites_layer.fields(), FIELD_TOWER
                    )
                    if tower_field:
                        for idx, feat in enumerate(sites_layer.getFeatures()):
                            tower = _tower_key(feat[tower_field])
                            if not tower:
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
            raise ValueError(f"Ping references unknown tower '{tower}'. Run Step 1 first.")

        site = site_lookup[tower]
        min_r = _safe_float(feat[field_map["min_r"]], site["min_radius"])
        max_r = _safe_float(feat[field_map["max_r"]], site["max_radius"])
        start_az, end_az = self._arc_angles_from_feature(feat, field_map)

        geom = self.build_arc_sector_polygon(
            site["point"], min_r, max_r, start_az, end_az
        )
        timestamp = ""
        if field_map["timestamp"]:
            timestamp = str(feat[field_map["timestamp"]] or "")

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
        self._add_vector_to_project(layer)

        if progress_callback:
            progress_callback(100)

        self.log(
            f"Unique sites: {len(features)} towers; "
            f"global min radius range computed per tower."
        )
        return layer

    # ----------------------------------------------------------- Step 2
    def generate_master_viewsheds(self, sites_layer, dem_layer, progress_callback=None, cancel_fn=None):
        """
        Generate one 360° donut viewshed per tower using visibility algorithms.
        RADIUS_IN = min_radius (inner dead-zone), RADIUS_OBS = max_radius.
        """
        self.master_viewshed_paths.clear()
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
                raise ValueError(f"Tower '{tower}' has invalid max_radius {max_radius}.")
            if min_radius >= max_radius:
                min_radius = 0.0

            vp_layer = QgsVectorLayer(
                f"Point?crs={CRS_HK1980.authid()}", f"vp_{file_stem}", "memory"
            )
            vp_provider = vp_layer.dataProvider()
            vp_provider.addAttributes([QgsField("radius_in", QVariant.Double)])
            vp_layer.updateFields()
            vp_feat = QgsFeature(vp_layer.fields())
            vp_feat.setGeometry(feat.geometry())
            vp_feat.setAttributes([min_radius])
            vp_provider.addFeatures([vp_feat])
            vp_layer.updateExtents()

            viewpoints_path = os.path.join(output_dir, f"{file_stem}_viewpoints.gpkg")
            viewshed_path = os.path.join(output_dir, f"{file_stem}_viewshed.tif")

            try:
                create_params = {
                    "OBSERVER_POINTS": vp_layer,
                    "DEM": dem_layer,
                    "RADIUS": max_radius,
                    "OBS_HEIGHT": observer_height,
                    "TARGET_HEIGHT": TARGET_HEIGHT,
                    "OUTPUT": viewpoints_path,
                }
                if min_radius > 0:
                    create_params["RADIUS_IN_FIELD"] = "radius_in"

                vp_result = processing.run(
                    "visibility:createviewpoints",
                    create_params,
                )

                vp_source = vp_result.get("OUTPUT", viewpoints_path)
                if isinstance(vp_source, str):
                    vp_input = QgsVectorLayer(vp_source, f"viewpoints_{file_stem}", "ogr")
                    if not vp_input.isValid():
                        vp_input = QgsVectorLayer(viewpoints_path, f"viewpoints_{file_stem}", "ogr")
                    if not vp_input.isValid():
                        raise RuntimeError(f"Failed to load viewpoints layer: {vp_source}")
                else:
                    vp_input = vp_source

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
                raise RuntimeError(
                    f"Viewshed failed for tower '{tower}' "
                    f"(RADIUS_IN={min_radius}, RADIUS_OBS={max_radius}): {exc}"
                ) from exc

            if not os.path.isfile(viewshed_path):
                raise RuntimeError(f"Viewshed output was not created for tower '{tower}'.")

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
        return True

    # ----------------------------------------------------------- Step 3
    def run_cascade_polygons(self, ping_layer, sites_layer, progress_callback=None):
        """
        Translate CSV arcs to curved polygons and apply 3-tier cascade logic:

        Rule 1 — No overlap: retain isolated arc remnants.
        Rule 2 — Primary overlap: keep pairwise intersection A ∩ B.
        Rule 3 — Secondary overlap: when primary pockets overlap, keep only the
                 mutual core where all participating sectors meet.
        """
        field_map = self._resolve_field_map(ping_layer)
        site_lookup = self._site_lookup(sites_layer)

        arcs = []
        total = ping_layer.featureCount() or 1
        for i, feat in enumerate(ping_layer.getFeatures()):
            if progress_callback and i % 25 == 0:
                progress_callback(int(30 * i / total))
            arc = self._build_arc_from_ping(feat, field_map, site_lookup)
            if arc["geometry"] and not arc["geometry"].isEmpty():
                arcs.append(arc)

        if not arcs:
            raise ValueError("No arc geometries could be built from ping layer.")

        pockets = self._apply_cascade_logic(arcs)

        if progress_callback:
            progress_callback(70)

        existing = self.find_layer_by_name(LAYER_CASCADE)
        if existing:
            QgsProject.instance().removeMapLayer(existing.id())
        legacy = self.find_layer_by_name("Cascade_Polygons")
        if legacy:
            QgsProject.instance().removeMapLayer(legacy.id())

        layer = QgsVectorLayer(
            f"Polygon?crs={CRS_HK1980.authid()}", LAYER_CASCADE, "memory"
        )
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("pocket_id", QVariant.Int),
                QgsField("cascade_rule", QVariant.String),
                QgsField("Participating_Towers", QVariant.String),
                QgsField("Timestamp", QVariant.String),
            ]
        )
        layer.updateFields()

        out_features = []
        for pocket_id, pocket in enumerate(pockets, start=1):
            geom = pocket["geometry"]
            if geom is None or geom.isEmpty():
                continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes(
                [
                    pocket_id,
                    pocket["rule"],
                    "|".join(pocket["towers"]),
                    pocket.get("timestamp", ""),
                ]
            )
            out_features.append(feat)

        provider.addFeatures(out_features)
        layer.updateExtents()

        output_path = self._cascade_polygons_output_path()
        processing.run(
            "native:savefeatures",
            {"INPUT": layer, "OUTPUT": output_path, "LAYER_NAME": LAYER_CASCADE},
        )

        saved_layer = QgsVectorLayer(output_path, LAYER_CASCADE, "ogr")
        if not saved_layer.isValid():
            raise RuntimeError(f"Failed to load saved cascade polygons: {output_path}")

        self._add_layer_at_project_root(saved_layer)
        self.log(f"Saved cascade polygons to: {output_path}")

        if progress_callback:
            progress_callback(100)

        self.log(f"Cascade polygons: {len(out_features)} pockets created.")
        return saved_layer

    def _apply_cascade_logic(self, arcs):
        """
        Build pocket list from arc sectors using 3-tier cascade rules.
        """
        n = len(arcs)

        def _intersection_geom(indices):
            geom = QgsGeometry(arcs[indices[0]]["geometry"])
            for idx in indices[1:]:
                geom = geom.intersection(arcs[idx]["geometry"])
                if geom.isEmpty():
                    break
            return geom

        # --- Rule 2: primary pairwise intersections ---
        primary_pockets = []
        for i in range(n):
            for j in range(i + 1, n):
                inter = arcs[i]["geometry"].intersection(arcs[j]["geometry"])
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

        # --- Rule 1: isolated remnants ---
        overlap_union_by_arc = [QgsGeometry() for _ in range(n)]
        for pocket in surviving_primaries + secondary_pockets:
            for arc_idx in range(n):
                if arcs[arc_idx]["tower"] in pocket["towers"]:
                    g = pocket["geometry"]
                    overlap_union_by_arc[arc_idx] = (
                        overlap_union_by_arc[arc_idx].combine(g)
                        if not overlap_union_by_arc[arc_idx].isEmpty()
                        else QgsGeometry(g)
                    )

        isolated_pockets = []
        for i, arc in enumerate(arcs):
            remainder = arc["geometry"]
            if not overlap_union_by_arc[i].isEmpty():
                remainder = remainder.difference(overlap_union_by_arc[i])
            if remainder.isEmpty():
                continue
            isolated_pockets.append(
                {
                    "geometry": remainder,
                    "towers": [arc["tower"]],
                    "rule": "Rule 1 - No Overlap",
                    "timestamp": arc["timestamp"],
                }
            )

        return isolated_pockets + surviving_primaries + secondary_pockets

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
        calc_temp = f"{output_path}.calc.tif"
        last_error = None
        for formula in ("numpy.where(A>0,1,0)", "(A>0)"):
            try:
                self._gdal_raster_calc([input_path], formula, calc_temp)
                last_error = None
                break
            except RuntimeError as exc:
                last_error = exc
        if last_error:
            raise last_error

        self._run_processing(
            "gdal:translate",
            {
                "INPUT": calc_temp,
                "NODATA": -9999,
                "COPY_SUBDATASETS": False,
                "OPTIONS": "COMPRESS=LZW",
                "EXTRA": "-ot Float32 -scale 0 255 0 1",
                "DATA_TYPE": 5,
                "OUTPUT": output_path,
            },
        )
        if os.path.isfile(calc_temp):
            try:
                os.remove(calc_temp)
            except OSError:
                pass
        if not os.path.isfile(output_path):
            raise RuntimeError(f"Binary normalization failed: {input_path}")

    def _gdal_raster_calc(self, raster_paths, formula, output_path):
        letters = "ABCDEF"
        params = {
            "FORMULA": formula,
            "NO_DATA": -9999,
            "RTYPE": 5,  # Float32
            "OPTIONS": "",
            "EXTRA": "",
            "OUTPUT": output_path,
        }
        for letter in letters:
            params[f"INPUT_{letter}"] = None
            params[f"BAND_{letter}"] = None

        if len(raster_paths) > len(letters):
            raise RuntimeError(
                f"Too many viewsheds ({len(raster_paths)}) for one GDAL calculation."
            )

        for i, path in enumerate(raster_paths):
            letter = letters[i]
            params[f"INPUT_{letter}"] = path
            params[f"BAND_{letter}"] = 1

        result = self._run_processing("gdal:rastercalculator", params)
        out_path = result.get("OUTPUT", output_path) if isinstance(result, dict) else output_path
        if not os.path.isfile(out_path):
            raise RuntimeError(f"GDAL raster calculator failed: {formula}")

    def _combine_viewsheds(self, raster_paths, pocket_id, projwin, work_dir, output_path):
        """Intersect viewsheds within a pocket extent as Float32 0/1."""
        clipped_inputs = []
        for i, path in enumerate(raster_paths):
            clipped = os.path.join(work_dir, f"p{pocket_id}_src{i}.tif")
            self._clip_raster_to_extent(path, projwin, clipped)
            clipped_inputs.append(clipped)

        combined_temp = f"{output_path}.combined.tif"
        if len(clipped_inputs) == 1:
            self._gdal_raster_calc(clipped_inputs, "(A>0)", combined_temp)
        elif len(clipped_inputs) <= 6:
            letters = "ABCDEF"[: len(clipped_inputs)]
            formula = "*".join(f"({letter}>0)" for letter in letters)
            self._gdal_raster_calc(clipped_inputs, formula, combined_temp)
        else:
            current_path = os.path.join(work_dir, f"p{pocket_id}_acc1.tif")
            self._gdal_raster_calc(
                clipped_inputs[:2], "(A>0)*(B>0)", current_path
            )
            for i, next_path in enumerate(clipped_inputs[2:], start=2):
                out_path = (
                    combined_temp
                    if i == len(clipped_inputs) - 1
                    else os.path.join(work_dir, f"p{pocket_id}_acc{i}.tif")
                )
                self._gdal_raster_calc(
                    [current_path, next_path], "(A>0)*(B>0)", out_path
                )
                current_path = out_path

        self._normalize_to_binary(combined_temp, output_path)
        if os.path.isfile(combined_temp):
            try:
                os.remove(combined_temp)
            except OSError:
                pass

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
        self, cascade_layer, dem_layer, progress_callback=None, cancel_fn=None
    ):
        """
        For each cascade pocket, multiply participating master viewsheds within
        the feature bounding box, then clip to the curved polygon mask.
        """
        self.resolve_master_viewshed_paths()
        if not self.master_viewshed_paths:
            raise RuntimeError(
                f"No master viewshed rasters found. Run Step 2 or load rasters "
                f"from '{GROUP_MASTER_VIEWSHEDS}'."
            )

        output_dir = self._timestamped_viewshed_output_dir()
        self.timestamped_viewshed_output_dir = output_dir
        self._clear_layer_tree_group(GROUP_TIMESTAMPED_VIEWSHEDS)
        self._clear_legacy_cascade_groups()
        self.log(f"Writing timestamped viewsheds to: {output_dir}")

        features = list(cascade_layer.getFeatures())
        total = len(features) or 1
        created_count = 0
        work_dir = os.path.join(output_dir, "_work")
        os.makedirs(work_dir, exist_ok=True)

        for idx, feat in enumerate(features):
            if cancel_fn and cancel_fn():
                return False

            if progress_callback:
                progress_callback(int(100 * idx / total))

            towers_raw = feat["Participating_Towers"] or ""
            towers = [
                _tower_key(t.strip()) for t in towers_raw.split("|") if t.strip()
            ]
            if not towers:
                continue

            pocket_id = feat["pocket_id"]
            ts_key = _timestamp_subgroup_key(feat["Timestamp"])

            raster_paths = [
                self.master_viewshed_paths[t]
                for t in towers
                if t in self.master_viewshed_paths
            ]
            missing = [t for t in towers if t not in self.master_viewshed_paths]
            for tower in missing:
                self.log(
                    f"Pocket {pocket_id}: missing master viewshed for {tower}",
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
            layer_name, file_stem = _viewshed_layer_names(towers, pocket_id)
            multiplied_path = os.path.join(work_dir, f"{file_stem}_multiplied.tif")
            clipped_path = os.path.join(ts_subdir, f"{file_stem}.tif")

            try:
                self._combine_viewsheds(
                    raster_paths, pocket_id, projwin, work_dir, multiplied_path
                )
                mask_layer = self._vector_mask_from_feature(feat, pocket_id)
                self._clip_viewshed_to_mask(
                    multiplied_path, mask_layer, clipped_path
                )

                subgroup = self._ensure_subgroup(
                    GROUP_TIMESTAMPED_VIEWSHEDS, ts_key
                )
                raster_layer = QgsRasterLayer(clipped_path, layer_name, "gdal")
                if not raster_layer.isValid():
                    raise RuntimeError(
                        f"Failed to load clipped viewshed: {clipped_path}"
                    )
                QgsProject.instance().addMapLayer(raster_layer, False)
                subgroup.addLayer(raster_layer)
                created_count += 1
                self.log(
                    f"{layer_name}: saved to {clipped_path} and added under "
                    f"{GROUP_TIMESTAMPED_VIEWSHEDS}/{ts_key}"
                )
            except Exception as exc:
                self.log(
                    f"Pocket {pocket_id} failed: {exc}",
                    Qgis.Warning,
                )
                self.log(traceback.format_exc(), Qgis.Warning)

        if progress_callback:
            progress_callback(100)

        if created_count == 0:
            raise RuntimeError(
                "No timestamped viewshed layers were created. "
                "Check the QGIS message log for per-pocket errors."
            )

        self.log(f"Step 4 complete: {created_count} timestamped viewshed layer(s).")
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

    def __init__(self, description, cascade_layer, dem_layer, engine, progress_callback=None):
        super().__init__(description, QgsTask.CanCancel)
        self.cascade_layer = cascade_layer
        self.dem_layer = dem_layer
        self.engine = engine
        self.progress_callback = progress_callback
        self.success = False
        self.error_message = ""

    def run(self):
        try:
            self.success = self.engine.multiply_and_crop_rasters(
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
