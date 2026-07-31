# -*- coding: utf-8 -*-
"""Dialog for managing viewshed raster symbology."""

from qgis.PyQt.QtCore import pyqtSlot
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
)
from qgis.core import Qgis, QgsMessageLog

from .logic_engine import LOG_TAG
from .symbology_engine import (
    MANAGED_GROUPS,
    SYMBOL_MODE_MONO,
    SYMBOL_MODE_MULTI,
    apply_viewshed_symbology,
    collect_viewshed_rasters,
)


class ViewshedSymbologyDialog(QDialog):
    """Choose a viewshed group and mono/multi colour scheme."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Manage Viewshed Symbology")
        self.setMinimumWidth(420)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Apply paletted symbology to viewshed rasters: value 0 is transparent, "
            "value 1 is visible at 60% opacity."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.group_combo = QComboBox()
        
        from qgis.core import QgsProject
        root = QgsProject.instance().layerTreeRoot()
        found_groups = []
        
        # Dynamically find suffixed viewshed groups in the current project
        for child in root.children():
            if child.nodeType() == 0:  # 0 is QgsLayerTree.Group
                name = child.name()
                for managed_name in MANAGED_GROUPS:
                    if name.startswith(managed_name):
                        found_groups.append(name)
                        break
        
        # Fallback if no matching groups exist yet
        if not found_groups:
            found_groups = MANAGED_GROUPS
            
        for group_name in found_groups:
            self.group_combo.addItem(group_name, group_name)
            
        form.addRow("Viewshed group:", self.group_combo)
        layout.addLayout(form)

        mode_group = QGroupBox("Colour pattern")
        mode_layout = QVBoxLayout(mode_group)
        self.radio_mono = QRadioButton("Monocoloured")
        self.radio_multi = QRadioButton("Multicoloured")
        self.radio_mono.setChecked(True)
        mode_layout.addWidget(self.radio_mono)
        mode_layout.addWidget(self.radio_multi)
        layout.addWidget(mode_group)

        hints = QLabel(
            "Master Viewshed: mono = blue, multi = distinct colour per layer.\n"
            "Viewshed with TA / Combined Viewshed: mono = red, multi = timestamp "
            "ramp (bright blue = oldest, hot magenta = newest; warm end pops most)."
        )
        hints.setWordWrap(True)
        layout.addWidget(hints)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._apply_symbology)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.group_combo.currentIndexChanged.connect(self._update_mode_hint)

    def _log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def _selected_group(self):
        return self.group_combo.currentData()

    def _selected_mode(self):
        return SYMBOL_MODE_MONO if self.radio_mono.isChecked() else SYMBOL_MODE_MULTI

    @pyqtSlot()
    def _update_mode_hint(self):
        group = self._selected_group()
        count = len(collect_viewshed_rasters(group))
        if count:
            self.setWindowTitle(
                f"Manage Viewshed Symbology ({count} raster layer(s))"
            )
        else:
            self.setWindowTitle("Manage Viewshed Symbology")

    @pyqtSlot()
    def _apply_symbology(self):
        group = self._selected_group()
        mode = self._selected_mode()
        try:
            styled = apply_viewshed_symbology(group, mode)
            self.iface.mapCanvas().refresh()
            mode_label = "Monocoloured" if mode == SYMBOL_MODE_MONO else "Multicoloured"
            QMessageBox.information(
                self,
                "Symbology Applied",
                f"Updated {styled} raster layer(s) in '{group}' "
                f"using {mode_label} symbology.",
            )
            self._log(
                f"Symbology applied to {styled} layer(s) in '{group}' ({mode})."
            )
            self.accept()
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Symbology Failed", str(exc))
