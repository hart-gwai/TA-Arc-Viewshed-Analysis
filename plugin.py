# -*- coding: utf-8 -*-
"""
Main plugin entry: toolbar button and dockable dialog launcher.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .ui_dialog import TAArcViewshedDialog
from .symbology_dialog import ViewshedSymbologyDialog


class TAArcViewshedAnalysisPlugin:
    """QGIS plugin interface for Cell Site Data Analyser."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "&Cell Site Data Analyser"
        self.toolbar = self.iface.addToolBar("Cell Site Analyser")
        self.toolbar.setObjectName("CellSiteAnalyserToolbar")
        self.dialog = None
        self.symbology_dialog = None

    def tr(self, message):
        return self.iface.mapCanvas().tr(message) if hasattr(self.iface.mapCanvas(), "tr") else message

    def add_action(
        self,
        icon,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        action = QAction(icon, text, parent or self.iface.mainWindow())
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip:
            action.setStatusTip(status_tip)
        if whats_this:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):
        icon_path_main = os.path.join(self.plugin_dir, "icons", "icon_analyser.svg")
        icon_path_symb = os.path.join(self.plugin_dir, "icons", "icon_symbology.svg")
        
        icon_main = QIcon(icon_path_main) if os.path.exists(icon_path_main) else QIcon()
        icon_symb = QIcon(icon_path_symb) if os.path.exists(icon_path_symb) else QIcon()

        self.add_action(
            icon_main,
            text=self.tr("Cell Site Data Analyser"),
            callback=self.run,
            parent=self.iface.mainWindow(),
            status_tip=self.tr("Open Cell Site Data Analyser workflow"),
        )

        self.add_action(
            icon_symb,
            text=self.tr("Manage Viewshed Symbology"),
            callback=self.run_symbology,
            add_to_toolbar=False,
            parent=self.iface.mainWindow(),
            status_tip=self.tr("Apply viewshed raster symbology to layer groups"),
        )

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

        if self.symbology_dialog is not None:
            self.symbology_dialog.close()
            self.symbology_dialog = None

    def run(self):
        if self.dialog is None:
            self.dialog = TAArcViewshedDialog(self.iface, self.iface.mainWindow())
            self.dialog.setWindowModality(Qt.NonModal)

        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def run_symbology(self):
        if self.symbology_dialog is None:
            self.symbology_dialog = ViewshedSymbologyDialog(
                self.iface, self.iface.mainWindow()
            )

        self.symbology_dialog.show()
        self.symbology_dialog.raise_()
        self.symbology_dialog.activateWindow()
