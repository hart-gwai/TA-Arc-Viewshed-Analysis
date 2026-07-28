# -*- coding: utf-8 -*-
"""
TA Arc & Viewshed Analysis QGIS plugin package.
"""


def classFactory(iface):  # pylint: disable=invalid-name
    """Load plugin class from plugin.py."""
    from .plugin import TAArcViewshedAnalysisPlugin

    return TAArcViewshedAnalysisPlugin(iface)
