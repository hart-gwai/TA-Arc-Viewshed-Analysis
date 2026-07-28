# -*- coding: utf-8 -*-
"""
Core spatial and processing logic for TA Arc & Viewshed Analysis.

Field names below match typical SAR cell-sector CSV exports. Adjust constants
if your CSV uses different column headers.
"""

import math
import os
import tempfile
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
FIELD_START_AZ = ("Start_Azimuth", "StartAz", "Azimuth_Start", "start_bearing")
FIELD_END_AZ = ("End_Azimuth", "EndAz", "Azimuth_End", "end_bearing")
FIELD_AZIMUTH = ("Azimuth", "Bearing", "Direction")
FIELD_BEAM_WIDTH = ("Beam_Width", "BeamWidth", "Sector_Width", "Angle")
FIELD_TIMESTAMP = ("Timestamp", "Event_Time", "DateTime", "Time", "Ping_Time")

LAYER_UNIQUE_SITES = "Unique_Cell_Sites"
LAYER_CASCADE = "Cascade_Polygons"
GROUP_MASTER_VIEWSHEDS = "Master Tower Viewsheds"

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
    return str(value).strip() if value is not None else ""


class TAArcViewshedEngine:
    """Stateful engine holding intermediate layers and master viewshed paths."""

    def __init__(self, iface):
        self.iface = iface
        self.master_viewshed_paths = {}  # tower_id -> raster file path
        self._transform_wgs84_to_hk = QgsCoordinateTransform(
            CRS_WGS84, CRS_HK1980, QgsProject.instance()
        )

    # ------------------------------------------------------------------ utils
    def log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def find_layer_by_name(self, name):
        layers = QgsProject.instance().mapLayersByName(name)
        return layers[0] if layers else None

    def _ensure_group(self, group_name):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)
        return group

    def _add_vector_to_project(self, layer, group_name=None):
        QgsProject.instance().addMapLayer(layer, addToLegend=False)
        if group_name:
            self._ensure_group(group_name).addLayer(layer)
        else:
            QgsProject.instance().layerTreeRoot().addLayer(layer)
        return layer

    def _add_raster_to_project(self, path, layer_name, group_name=None):
        layer = QgsRasterLayer(path, layer_name)
        if not layer.isValid():
            raise RuntimeError(f"Failed to load raster: {path}")
        QgsProject.instance().addMapLayer(layer, addToLegend=False)
        if group_name:
            self._ensure_group(group_name).addLayer(layer)
        else:
            QgsProject.instance().layerTreeRoot().addLayer(layer)
        return layer

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
        }
        missing = [k for k in ("tower", "min_r", "max_r") if field_map[k] is None]
        if missing:
            raise ValueError(
                "Ping layer is missing required fields. Expected tower ID and radius columns. "
                f"Could not resolve: {missing}. "
                f"Available fields: {[f.name() for f in fields]}"
            )
        return field_map

    def _site_lookup(self, sites_layer):
        """Build tower_id -> {point_hk, min_radius, max_radius} from unique sites layer."""
        lookup = {}
        for feat in sites_layer.getFeatures():
            tower = _tower_key(feat["tower_id"])
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
    def extract_unique_sites(self, ping_layer, progress_callback=None):
        """
        Parse ping CSV layer, transform tower coordinates to EPSG:2326, and
        aggregate global Min_Radius / Max_Radius per unique tower.
        """
        field_map = self._resolve_field_map(ping_layer)

        aggregates = {}
        total = ping_layer.featureCount() or 1
        for i, feat in enumerate(ping_layer.getFeatures()):
            if progress_callback and i % 50 == 0:
                progress_callback(int(100 * i / total))

            tower = _tower_key(feat[field_map["tower"]])
            if not tower:
                continue

            pt_wgs84 = feat.geometry().asPoint()
            pt_hk = self._transform_point_to_hk(pt_wgs84)
            min_r = _safe_float(feat[field_map["min_r"]])
            max_r = _safe_float(feat[field_map["max_r"]])

            if tower not in aggregates:
                aggregates[tower] = {
                    "point": pt_hk,
                    "min_radius": min_r,
                    "max_radius": max_r,
                }
            else:
                aggregates[tower]["min_radius"] = min(
                    aggregates[tower]["min_radius"], min_r
                )
                aggregates[tower]["max_radius"] = max(
                    aggregates[tower]["max_radius"], max_r
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
                QgsField("tower_id", QVariant.String),
                QgsField("min_radius", QVariant.Double),
                QgsField("max_radius", QVariant.Double),
            ]
        )
        layer.updateFields()

        features = []
        for tower, data in sorted(aggregates.items()):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(data["point"]))
            feat.setAttributes([tower, data["min_radius"], data["max_radius"]])
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
        output_dir = tempfile.mkdtemp(prefix="ta_master_viewsheds_")
        features = list(sites_layer.getFeatures())
        total = len(features) or 1

        for idx, feat in enumerate(features):
            if cancel_fn and cancel_fn():
                return False

            if progress_callback:
                progress_callback(int(100 * idx / total))

            tower = _tower_key(feat["tower_id"])
            min_radius = float(feat["min_radius"])
            max_radius = float(feat["max_radius"])

            # Ephemeral observer point for this tower
            vp_layer = QgsVectorLayer(
                f"Point?crs={CRS_HK1980.authid()}", f"vp_{tower}", "memory"
            )
            vp_provider = vp_layer.dataProvider()
            vp_provider.addFeatures([QgsFeature(feat.geometry())])
            vp_layer.updateExtents()

            viewpoints_path = os.path.join(output_dir, f"{tower}_viewpoints.gpkg")
            viewshed_path = os.path.join(output_dir, f"{tower}_viewshed.tif")

            vp_result = processing.run(
                "visibility:createviewpoints",
                {
                    "INPUT": vp_layer,
                    "OUTPUT": viewpoints_path,
                },
            )

            vp_source = vp_result["OUTPUT"]
            if isinstance(vp_source, str):
                vp_input = QgsVectorLayer(vp_source, f"viewpoints_{tower}", "ogr")
            else:
                vp_input = vp_source

            processing.run(
                "visibility:viewshed",
                {
                    "INPUT_DEM": dem_layer,
                    "INPUT_POINTS": vp_input,
                    "OUTPUT": viewshed_path,
                    "RADIUS_IN": min_radius,
                    "RADIUS_OBS": max_radius,
                    "OBSERVER_HEIGHT": OBSERVER_HEIGHT,
                    "TARGET_HEIGHT": TARGET_HEIGHT,
                },
            )

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
        self._add_vector_to_project(layer)

        if progress_callback:
            progress_callback(100)

        self.log(f"Cascade polygons: {len(out_features)} pockets created.")
        return layer

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

    # ----------------------------------------------------------- Step 4
    def multiply_and_crop_rasters(
        self, cascade_layer, dem_layer, progress_callback=None, cancel_fn=None
    ):
        """
        For each cascade pocket, multiply participating master viewsheds within
        the feature bounding box, then clip to the curved polygon mask.
        """
        features = list(cascade_layer.getFeatures())
        total = len(features) or 1
        timestamp_groups = defaultdict(list)

        work_dir = tempfile.mkdtemp(prefix="ta_cascade_rasters_")

        for idx, feat in enumerate(features):
            if cancel_fn and cancel_fn():
                return False

            if progress_callback:
                progress_callback(int(100 * idx / total))

            towers_raw = feat["Participating_Towers"] or ""
            towers = [t.strip() for t in towers_raw.split("|") if t.strip()]
            if not towers:
                continue

            bbox = feat.geometry().boundingBox()
            extent_str = f"{bbox.xMinimum()},{bbox.xMaximum()},{bbox.yMinimum()},{bbox.yMaximum()}"

            pocket_id = feat["pocket_id"]
            ts_key = feat["Timestamp"] or datetime.now().strftime("%Y%m%d_%H%M%S")
            out_prefix = os.path.join(work_dir, f"pocket_{pocket_id}")

            if len(towers) == 1:
                tower = towers[0]
                if tower not in self.master_viewshed_paths:
                    self.log(f"Missing master viewshed for tower {tower}", Qgis.Warning)
                    continue
                multiplied_path = self.master_viewshed_paths[tower]
            else:
                multiplied_path = f"{out_prefix}_multiplied.tif"
                raster_paths = [
                    self.master_viewshed_paths[t]
                    for t in towers
                    if t in self.master_viewshed_paths
                ]
                if not raster_paths:
                    continue

                expression = "*".join(f'("{path}@1" > 0)' for path in raster_paths)

                processing.run(
                    "qgis:rastercalculator",
                    {
                        "EXPRESSION": expression,
                        "LAYERS": raster_paths,
                        "CELLSIZE": 0,
                        "EXTENT": extent_str,
                        "CRS": CRS_HK1980,
                        "OUTPUT": multiplied_path,
                    },
                )

            clipped_path = f"{out_prefix}_clipped.tif"
            mask_layer = self._feature_to_temp_mask(feat, pocket_id, work_dir)

            processing.run(
                "gdal:cliprasterbymasklayer",
                {
                    "INPUT": multiplied_path,
                    "MASK": mask_layer,
                    "SOURCE_CRS": CRS_HK1980,
                    "TARGET_CRS": CRS_HK1980,
                    "NODATA": -9999,
                    "ALPHA_BAND": False,
                    "CROP_TO_CUTLINE": True,
                    "KEEP_RESOLUTION": True,
                    "OPTIONS": "",
                    "DATA_TYPE": 5,  # Float32
                    "OUTPUT": clipped_path,
                },
            )

            layer_name = f"Cascade_{pocket_id}_{'_'.join(towers)}"
            raster_layer = self._add_raster_to_project(clipped_path, layer_name)
            timestamp_groups[ts_key].append(raster_layer)

        # Group rasters by timestamp in layer tree
        root = QgsProject.instance().layerTreeRoot()
        for ts_key, layers in timestamp_groups.items():
            group_name = f"Cascade Viewsheds — {ts_key}"
            group = root.findGroup(group_name)
            if group is None:
                group = root.insertGroup(0, group_name)
            for rl in layers:
                node = root.findLayer(rl.id())
                if node:
                    clone = node.clone()
                    parent = node.parent()
                    parent.removeChildNode(node)
                    group.addChildNode(clone)

        if progress_callback:
            progress_callback(100)
        return True

    def _feature_to_temp_mask(self, feat, pocket_id, work_dir):
        path = os.path.join(work_dir, f"mask_{pocket_id}.gpkg")
        if os.path.exists(path):
            os.remove(path)

        mask = QgsVectorLayer(
            f"Polygon?crs={CRS_HK1980.authid()}", f"mask_{pocket_id}", "memory"
        )
        mask.dataProvider().addFeatures([QgsFeature(feat)])
        mask.updateExtents()

        processing.run(
            "native:savefeatures",
            {"INPUT": mask, "OUTPUT": path, "LAYER_NAME": "mask"},
        )
        return path


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
            self.engine.log(str(exc), Qgis.Critical)
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
            self.engine.log(str(exc), Qgis.Critical)
            return False

    def _emit_progress(self, value):
        if self.progress_callback:
            self.progress_callback(value)

    def finished(self, result):
        if result and self.success:
            self.engine.log("Raster multiply and crop completed.")
        elif not self.isCanceled():
            self.engine.log("Raster multiply and crop failed.", Qgis.Warning)
