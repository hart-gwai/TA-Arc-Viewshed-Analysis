# -*- coding: utf-8 -*-
"""Apply consistent viewshed raster symbology across plugin layer groups."""

from collections import defaultdict
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsMapLayerType,
    QgsMessageLog,
    QgsPalettedRasterRenderer,
    QgsProject,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

from .logic_engine import (
    GROUP_COMBINED_VIEWSHED,
    GROUP_MASTER_VIEWSHEDS,
    GROUP_VIEWSHED_WITH_TA,
    LEGACY_MASTER_VIEWSHED_GROUPS,
    LOG_TAG,
)

SYMBOL_MODE_MONO = "mono"
SYMBOL_MODE_MULTI = "multi"

MANAGED_GROUPS = (
    GROUP_MASTER_VIEWSHEDS,
    GROUP_VIEWSHED_WITH_TA,
    GROUP_COMBINED_VIEWSHED,
)

VISIBLE_OPACITY = 0.6
VISIBLE_OPACITY_ALPHA = 153  # 60% of 255

# Timestamp ramp for Viewshed with TA / Combined Viewshed (oldest → newest).
# Tuned for OSM Standard at 60% opacity: vivid cool blues/violets for older
# viewsheds (all clearly visible); warm coral/orange/magenta for recent.
_OSM_TIMESTAMP_STOPS = (
    (0.00, (70, 110, 240)),   # vivid royal blue
    (0.20, (85, 155, 255)),   # bright sky blue
    (0.40, (140, 95, 255)),   # vivid periwinkle / violet
    (0.60, (255, 110, 90)),   # coral
    (0.80, (255, 145, 30)),   # bright orange
    (1.00, (255, 0, 200)),    # hot magenta / fuchsia
)


def _log(message, level=Qgis.Info):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def _parse_timestamp_label(label):
    """Parse a legend subgroup name back into a datetime when possible."""
    text = str(label or "").strip()
    if not text:
        return None
    normalized = text.replace("_", ":")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def _timestamp_sort_key(label):
    parsed = _parse_timestamp_label(label)
    if parsed is not None:
        return (0, parsed)
    return (1, str(label or ""))


def _interpolate_stops(stops, t):
    t = max(0.0, min(1.0, float(t)))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            if t1 == t0:
                ratio = 0.0
            else:
                ratio = (t - t0) / (t1 - t0)
            r = int(c0[0] + (c1[0] - c0[0]) * ratio)
            g = int(c0[1] + (c1[1] - c0[1]) * ratio)
            b = int(c0[2] + (c1[2] - c0[2]) * ratio)
            return QColor(r, g, b, VISIBLE_OPACITY_ALPHA)
    _, last = stops[-1]
    return QColor(last[0], last[1], last[2], VISIBLE_OPACITY_ALPHA)


def _distinct_colors(count):
    """Generate visually distinct colours at configured opacity."""
    if count <= 0:
        return []
    colors = []
    golden = 0.618033988749895
    for i in range(count):
        hue = (i * golden) % 1.0
        color = QColor()
        color.setHsvF(hue, 0.65, 0.95, VISIBLE_OPACITY)
        colors.append(color)
    return colors


def _group_names_for_lookup(group_name):
    if group_name == GROUP_MASTER_VIEWSHEDS:
        return (GROUP_MASTER_VIEWSHEDS,) + LEGACY_MASTER_VIEWSHED_GROUPS
    return (group_name,)


def collect_viewshed_rasters(group_name):
    """
    Return list of (timestamp_subgroup or None, QgsRasterLayer) within *group_name*.
    """
    root = QgsProject.instance().layerTreeRoot()
    collected = []
    seen_ids = set()

    for lookup_name in _group_names_for_lookup(group_name):
        tree_group = root.findGroup(lookup_name)
        if tree_group is None:
            continue

        for child in tree_group.children():
            if hasattr(child, "layer"):
                layer = child.layer()
                if (
                    layer
                    and layer.type() == QgsMapLayerType.RasterLayer
                    and layer.id() not in seen_ids
                ):
                    seen_ids.add(layer.id())
                    collected.append((None, layer))
                continue

            subgroup_name = child.name() if hasattr(child, "name") else None
            if not hasattr(child, "findLayers"):
                continue
            for node in child.findLayers():
                layer = node.layer()
                if (
                    layer
                    and layer.type() == QgsMapLayerType.RasterLayer
                    and layer.id() not in seen_ids
                ):
                    seen_ids.add(layer.id())
                    collected.append((subgroup_name, layer))

    return collected


def _apply_paletted_renderer(layer, visible_color):
    """Set band 1 to paletted/unique values: 0 transparent, 1 coloured."""
    transparent = QColor(0, 0, 0, 0)
    try:
        classes = [
            QgsPalettedRasterRenderer.Class(0, transparent, "Not visible"),
            QgsPalettedRasterRenderer.Class(1, visible_color, "Visible"),
        ]
        renderer = QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)
    except (TypeError, AttributeError):
        shader = QgsRasterShader()
        ramp_shader = QgsColorRampShader()
        ramp_shader.setColorRampType(QgsColorRampShader.Exact)
        ramp_shader.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(0, transparent, "0"),
                QgsColorRampShader.ColorRampItem(1, visible_color, "1"),
            ]
        )
        shader.setRasterShaderFunction(ramp_shader)
        renderer = QgsSingleBandPseudoColorRenderer(
            layer.dataProvider(), 1, shader
        )

    layer.setRenderer(renderer)
    layer.triggerRepaint()


def apply_viewshed_symbology(group_name, mode):
    """
    Apply symbology to all viewshed rasters in *group_name*.

    mode: SYMBOL_MODE_MONO or SYMBOL_MODE_MULTI
    """
    is_managed = False
    is_master = False
    for managed_name in MANAGED_GROUPS:
        if group_name.startswith(managed_name):
            is_managed = True
            if managed_name == GROUP_MASTER_VIEWSHEDS:
                is_master = True
            break
            
    if not is_managed:
        raise ValueError(f"Unsupported group: {group_name}")
    if mode not in (SYMBOL_MODE_MONO, SYMBOL_MODE_MULTI):
        raise ValueError(f"Unsupported symbology mode: {mode}")

    rasters = collect_viewshed_rasters(group_name)
    if not rasters:
        raise ValueError(
            f"No raster layers found in '{group_name}'. "
            "Run the relevant workflow step first."
        )

    styled = 0

    if is_master:
        mono_color = QColor(0, 0, 255, VISIBLE_OPACITY_ALPHA)
        if mode == SYMBOL_MODE_MONO:
            for _, layer in rasters:
                _apply_paletted_renderer(layer, mono_color)
                styled += 1
        else:
            colors = _distinct_colors(len(rasters))
            for (_, layer), color in zip(rasters, colors):
                _apply_paletted_renderer(layer, color)
                styled += 1
    else:
        mono_color = QColor(255, 0, 0, VISIBLE_OPACITY_ALPHA)
        if mode == SYMBOL_MODE_MONO:
            for _, layer in rasters:
                _apply_paletted_renderer(layer, mono_color)
                styled += 1
        else:
            by_timestamp = defaultdict(list)
            for ts_label, layer in rasters:
                by_timestamp[ts_label or "Unknown"].append(layer)

            timestamps = sorted(by_timestamp.keys(), key=_timestamp_sort_key)
            denom = max(len(timestamps) - 1, 1)
            for index, ts_label in enumerate(timestamps):
                color = _interpolate_stops(_OSM_TIMESTAMP_STOPS, index / denom)
                for layer in by_timestamp[ts_label]:
                    _apply_paletted_renderer(layer, color)
                    styled += 1

    _log(
        f"Applied {mode} symbology to {styled} raster(s) in '{group_name}'."
    )
    return styled
