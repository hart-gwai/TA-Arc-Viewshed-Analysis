# -*- coding: utf-8 -*-
"""
PyQt5 dialog with four sequential workflow sections.
"""

from qgis.PyQt.QtCore import Qt, pyqtSlot
from qgis.PyQt.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel, QgsMessageLog, Qgis

from .logic_engine import (
    TAArcViewshedEngine,
    ViewshedGenerationTask,
    RasterMultiplyTask,
    LOG_TAG,
)


class TAArcViewshedDialog(QDialog):
    """Main workflow dialog for the TA Arc & Viewshed Analysis plugin."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.engine = TAArcViewshedEngine(iface)
        self._viewshed_task = None
        self._raster_task = None

        self.setWindowTitle("TA Arc & Viewshed Analysis")
        self.setMinimumWidth(520)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Input Setup ---
        input_group = QGroupBox("Input Setup")
        input_layout = QVBoxLayout(input_group)

        ping_row = QHBoxLayout()
        ping_row.addWidget(QLabel("CSV Ping Layer:"))
        self.ping_layer_combo = QgsMapLayerComboBox()
        self.ping_layer_combo.setFilters(
            QgsMapLayerProxyModel.PointLayer | QgsMapLayerProxyModel.VectorLayer
        )
        ping_row.addWidget(self.ping_layer_combo, stretch=1)
        input_layout.addLayout(ping_row)

        dem_row = QHBoxLayout()
        dem_row.addWidget(QLabel("DEM Layer:"))
        self.dem_layer_combo = QgsMapLayerComboBox()
        self.dem_layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        dem_row.addWidget(self.dem_layer_combo, stretch=1)
        input_layout.addLayout(dem_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        input_layout.addWidget(self.progress_bar)

        layout.addWidget(input_group)

        # --- Step 1 ---
        step1_group = QGroupBox("Step 1 — Extract Unique Sites")
        step1_layout = QVBoxLayout(step1_group)
        step1_layout.addWidget(
            QLabel(
                "Parse ping CSV, transform to EPSG:2326, and build Unique_Cell_Sites "
                "with global Min_Radius / Max_Radius per tower."
            )
        )
        self.btn_step1 = QPushButton("Extract Unique Sites")
        self.btn_step1.clicked.connect(self._run_step1)
        step1_layout.addWidget(self.btn_step1)
        layout.addWidget(step1_group)

        # --- Step 2 ---
        step2_group = QGroupBox("Step 2 — Generate Master Viewsheds")
        step2_layout = QVBoxLayout(step2_group)
        step2_layout.addWidget(
            QLabel(
                "Create 360° donut viewsheds (RADIUS_IN = Min_Radius, RADIUS_OBS = Max_Radius) "
                "and group under Master Tower Viewsheds."
            )
        )
        self.btn_step2 = QPushButton("Generate Master Viewsheds")
        self.btn_step2.clicked.connect(self._run_step2)
        step2_layout.addWidget(self.btn_step2)
        layout.addWidget(step2_group)

        # --- Step 3 ---
        step3_group = QGroupBox("Step 3 — Run Cascade Polygons")
        step3_layout = QVBoxLayout(step3_group)
        step3_layout.addWidget(
            QLabel(
                "Build curved arc polygons and apply 3-tier cascade intersection logic "
                "to produce Cascade_Polygons with Participating_Towers."
            )
        )
        self.btn_step3 = QPushButton("Run Cascade Polygons")
        self.btn_step3.clicked.connect(self._run_step3)
        step3_layout.addWidget(self.btn_step3)
        layout.addWidget(step3_group)

        # --- Step 4 ---
        step4_group = QGroupBox("Step 4 — Multiply & Crop Rasters")
        step4_layout = QVBoxLayout(step4_group)
        step4_layout.addWidget(
            QLabel(
                "Multiply master viewsheds per cascade pocket (bbox-limited), clip to arc mask, "
                "and group outputs by timestamp."
            )
        )
        self.btn_step4 = QPushButton("Multiply & Crop Rasters")
        self.btn_step4.clicked.connect(self._run_step4)
        step4_layout.addWidget(self.btn_step4)
        layout.addWidget(step4_group)

        layout.addStretch()

    def _log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def _set_busy(self, busy):
        for btn in (self.btn_step1, self.btn_step2, self.btn_step3, self.btn_step4):
            btn.setEnabled(not busy)
        self.ping_layer_combo.setEnabled(not busy)
        self.dem_layer_combo.setEnabled(not busy)

    def _validate_inputs(self, require_dem=False):
        ping_layer = self.ping_layer_combo.currentLayer()
        dem_layer = self.dem_layer_combo.currentLayer()

        if ping_layer is None:
            QMessageBox.warning(self, "Missing Input", "Select a CSV Ping point layer.")
            return None, None

        if require_dem and dem_layer is None:
            QMessageBox.warning(self, "Missing Input", "Select a DEM raster layer.")
            return None, None

        return ping_layer, dem_layer

    @pyqtSlot(int)
    def _on_progress(self, value):
        self.progress_bar.setValue(max(0, min(100, value)))

    @pyqtSlot()
    def _run_step1(self):
        ping_layer, _ = self._validate_inputs()
        if ping_layer is None:
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        try:
            layer = self.engine.extract_unique_sites(ping_layer, progress_callback=self._on_progress)
            self.progress_bar.setValue(100)
            self._log(f"Step 1 complete: {layer.featureCount()} unique cell sites.")
            QMessageBox.information(
                self,
                "Step 1 Complete",
                f"Created Unique_Cell_Sites with {layer.featureCount()} tower(s).",
            )
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Step 1 Failed", str(exc))
        finally:
            self._set_busy(False)

    @pyqtSlot()
    def _run_step2(self):
        ping_layer, dem_layer = self._validate_inputs(require_dem=True)
        if ping_layer is None or dem_layer is None:
            return

        sites_layer = self.engine.find_layer_by_name("Unique_Cell_Sites")
        if sites_layer is None:
            QMessageBox.warning(
                self,
                "Prerequisite",
                "Run Step 1 first to create Unique_Cell_Sites.",
            )
            return

        if self._viewshed_task is not None and self._viewshed_task.isActive():
            QMessageBox.information(self, "Busy", "Viewshed generation is already running.")
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        self._viewshed_task = ViewshedGenerationTask(
            "Generate Master Viewsheds",
            sites_layer,
            dem_layer,
            self.engine,
            progress_callback=self._on_progress,
        )
        self._viewshed_task.taskCompleted.connect(self._on_viewshed_finished)
        self._viewshed_task.taskTerminated.connect(self._on_viewshed_terminated)
        QgsApplication.taskManager().addTask(self._viewshed_task)

    @pyqtSlot()
    def _on_viewshed_finished(self):
        self.progress_bar.setValue(100)
        self._set_busy(False)
        count = len(self.engine.master_viewshed_paths)
        QMessageBox.information(
            self,
            "Step 2 Complete",
            f"Generated {count} master viewshed raster(s) in group 'Master Tower Viewsheds'.",
        )

    @pyqtSlot()
    def _on_viewshed_terminated(self):
        self._set_busy(False)
        QMessageBox.warning(self, "Step 2 Cancelled", "Viewshed generation was cancelled.")

    @pyqtSlot()
    def _run_step3(self):
        ping_layer, _ = self._validate_inputs()
        if ping_layer is None:
            return

        sites_layer = self.engine.find_layer_by_name("Unique_Cell_Sites")
        if sites_layer is None:
            QMessageBox.warning(
                self,
                "Prerequisite",
                "Run Step 1 first to create Unique_Cell_Sites.",
            )
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        try:
            layer = self.engine.run_cascade_polygons(
                ping_layer,
                sites_layer,
                progress_callback=self._on_progress,
            )
            self.progress_bar.setValue(100)
            self._log(f"Step 3 complete: {layer.featureCount()} cascade pocket(s).")
            QMessageBox.information(
                self,
                "Step 3 Complete",
                f"Created Cascade_Polygons with {layer.featureCount()} feature(s).",
            )
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Step 3 Failed", str(exc))
        finally:
            self._set_busy(False)

    @pyqtSlot()
    def _run_step4(self):
        _, dem_layer = self._validate_inputs(require_dem=True)
        if dem_layer is None:
            return

        cascade_layer = self.engine.find_layer_by_name("Cascade_Polygons")
        if cascade_layer is None:
            QMessageBox.warning(
                self,
                "Prerequisite",
                "Run Step 3 first to create Cascade_Polygons.",
            )
            return

        if not self.engine.master_viewshed_paths:
            QMessageBox.warning(
                self,
                "Prerequisite",
                "Run Step 2 first to generate master tower viewsheds.",
            )
            return

        if self._raster_task is not None and self._raster_task.isActive():
            QMessageBox.information(self, "Busy", "Raster multiply/crop is already running.")
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        self._raster_task = RasterMultiplyTask(
            "Multiply & Crop Rasters",
            cascade_layer,
            dem_layer,
            self.engine,
            progress_callback=self._on_progress,
        )
        self._raster_task.taskCompleted.connect(self._on_raster_finished)
        self._raster_task.taskTerminated.connect(self._on_raster_terminated)
        QgsApplication.taskManager().addTask(self._raster_task)

    @pyqtSlot()
    def _on_raster_finished(self):
        self.progress_bar.setValue(100)
        self._set_busy(False)
        QMessageBox.information(
            self,
            "Step 4 Complete",
            "Cascade viewshed rasters multiplied, clipped, and grouped by timestamp.",
        )

    @pyqtSlot()
    def _on_raster_terminated(self):
        self._set_busy(False)
        QMessageBox.warning(self, "Step 4 Cancelled", "Raster processing was cancelled.")


# Late import avoids circular dependency at module load time.
from qgis.core import QgsApplication  # noqa: E402  pylint: disable=wrong-import-position
