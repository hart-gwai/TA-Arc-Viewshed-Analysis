# -*- coding: utf-8 -*-
"""
PyQt5 dialog with four sequential workflow sections.
"""

from qgis.PyQt import sip
from qgis.PyQt.QtCore import pyqtSlot
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
from qgis.core import QgsApplication, QgsMapLayerProxyModel, QgsMessageLog, QgsProject, Qgis

from .logic_engine import (
    TAArcViewshedEngine,
    GROUP_MASTER_VIEWSHEDS,
    GROUP_TIMESTAMPED_VIEWSHEDS,
    LAYER_CASCADE,
    LOG_TAG,
)
from .csv_prep_dialog import CsvPrepDialog
from .csv_prep_engine import PREPARED_LAYER_NAME


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
        self._refresh_prepared_layer_status()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_prepared_layer_status()

    def _prepared_ping_layer(self):
        layers = QgsProject.instance().mapLayersByName(PREPARED_LAYER_NAME)
        return layers[0] if layers else None

    def _refresh_prepared_layer_status(self):
        layer = self._prepared_ping_layer()
        if layer is None:
            self.prepared_layer_status.setText(
                f"{PREPARED_LAYER_NAME}: not created — run Step 0 first"
            )
            return

        self.prepared_layer_status.setText(
            f"{PREPARED_LAYER_NAME}: ready ({layer.featureCount()} ping features)"
        )

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Input Setup ---
        input_group = QGroupBox("Input Setup")
        input_layout = QVBoxLayout(input_group)

        self.prepared_layer_status = QLabel()
        self.prepared_layer_status.setWordWrap(True)
        input_layout.addWidget(self.prepared_layer_status)

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

        # --- Step 0 ---
        step0_group = QGroupBox("Step 0 — Prepare CSV / Map Columns")
        step0_layout = QVBoxLayout(step0_group)
        step0_layout.addWidget(
            QLabel(
                "Import a raw CSV and map its columns (tower ID, radii, azimuth, etc.) "
                f"to create '{PREPARED_LAYER_NAME}'."
            )
        )
        self.btn_step0 = QPushButton("Open CSV Preparation…")
        self.btn_step0.clicked.connect(self._open_csv_prep)
        step0_layout.addWidget(self.btn_step0)
        layout.addWidget(step0_group)

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
                f"and save to '{GROUP_MASTER_VIEWSHEDS}' next to the project file."
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
                f"to produce {LAYER_CASCADE} with Participating_Towers."
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
                "Multiply master viewsheds per cascade pocket, clip to arc mask, "
                f"save to '{GROUP_TIMESTAMPED_VIEWSHEDS}' next to the project file, "
                "and group layers by timestamp."
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
        for btn in (self.btn_step0, self.btn_step1, self.btn_step2, self.btn_step3, self.btn_step4):
            btn.setEnabled(not busy)
        self.dem_layer_combo.setEnabled(not busy)

    def _validate_prepared_ping(self):
        ping_layer = self._prepared_ping_layer()
        if ping_layer is None:
            QMessageBox.warning(
                self,
                "Missing Input",
                f"Run Step 0 first to create '{PREPARED_LAYER_NAME}'.",
            )
            return None
        return ping_layer

    def _validate_dem(self):
        dem_layer = self.dem_layer_combo.currentLayer()
        if dem_layer is None:
            QMessageBox.warning(self, "Missing Input", "Select a DEM raster layer.")
            return None
        return dem_layer

    def _task_is_active(self, task):
        if task is None:
            return False
        try:
            if sip.isdeleted(task):
                return False
            return task.isActive()
        except RuntimeError:
            return False

    def _take_viewshed_task(self):
        task = self._viewshed_task
        self._viewshed_task = None
        if task is None or sip.isdeleted(task):
            return None
        return task

    def _take_raster_task(self):
        task = self._raster_task
        self._raster_task = None
        if task is None or sip.isdeleted(task):
            return None
        return task

    @pyqtSlot(int)
    def _on_progress(self, value):
        self.progress_bar.setValue(max(0, min(100, value)))
        QgsApplication.processEvents()

    @pyqtSlot()
    def _open_csv_prep(self):
        dialog = CsvPrepDialog(self.iface, self)
        if self.dem_layer_combo.currentLayer():
            dialog.dem_layer_combo.setLayer(self.dem_layer_combo.currentLayer())
        if dialog.exec_() == dialog.Accepted:
            self._refresh_prepared_layer_status()

    @pyqtSlot()
    def _run_step1(self):
        ping_layer = self._validate_prepared_ping()
        if ping_layer is None:
            return

        dem_layer = self.dem_layer_combo.currentLayer()

        self._set_busy(True)
        self.progress_bar.setValue(0)

        try:
            layer = self.engine.extract_unique_sites(
                ping_layer,
                dem_layer=dem_layer,
                progress_callback=self._on_progress,
            )
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
        dem_layer = self._validate_dem()
        if dem_layer is None:
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
            success = self.engine.generate_master_viewsheds(
                sites_layer,
                dem_layer,
                progress_callback=self._on_progress,
            )
            self.progress_bar.setValue(100)
            count = len(self.engine.master_viewshed_paths)
            if success and count > 0:
                self._log(f"Step 2 complete: {count} master viewshed(s).")
                folder = self.engine.master_viewshed_output_dir or GROUP_MASTER_VIEWSHEDS
                QMessageBox.information(
                    self,
                    "Step 2 Complete",
                    f"Generated {count} master viewshed raster(s).\n\n"
                    f"Folder: {folder}\n"
                    f"Layer group: {GROUP_MASTER_VIEWSHEDS}",
                )
            else:
                QMessageBox.critical(
                    self,
                    "Step 2 Failed",
                    "Viewshed generation failed. Check the QGIS message log.",
                )
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Step 2 Failed", str(exc))
        finally:
            self._set_busy(False)
            self._viewshed_task = None

    @pyqtSlot()
    def _on_viewshed_finished(self):
        self._take_viewshed_task()
        self._set_busy(False)

    @pyqtSlot()
    def _on_viewshed_terminated(self):
        self._take_viewshed_task()
        self._set_busy(False)

    @pyqtSlot()
    def _run_step3(self):
        ping_layer = self._validate_prepared_ping()
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
                f"Created {LAYER_CASCADE} with {layer.featureCount()} feature(s).",
            )
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Step 3 Failed", str(exc))
        finally:
            self._set_busy(False)

    @pyqtSlot()
    def _run_step4(self):
        dem_layer = self._validate_dem()
        if dem_layer is None:
            return

        cascade_layer = self.engine.find_layer_by_name(LAYER_CASCADE)
        if cascade_layer is None:
            QMessageBox.warning(
                self,
                "Prerequisite",
                f"Run Step 3 first to create {LAYER_CASCADE}.",
            )
            return

        if not self.engine.resolve_master_viewshed_paths():
            QMessageBox.warning(
                self,
                "Prerequisite",
                f"Run Step 2 first to generate master viewsheds in "
                f"'{GROUP_MASTER_VIEWSHEDS}'.",
            )
            return

        if self._task_is_active(self._raster_task):
            QMessageBox.information(self, "Busy", "Raster multiply/crop is already running.")
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        try:
            success = self.engine.multiply_and_crop_rasters(
                cascade_layer,
                dem_layer,
                progress_callback=self._on_progress,
            )
            self.progress_bar.setValue(100)
            if success:
                folder = (
                    self.engine.timestamped_viewshed_output_dir
                    or GROUP_TIMESTAMPED_VIEWSHEDS
                )
                QMessageBox.information(
                    self,
                    "Step 4 Complete",
                    f"Timestamped viewshed layers created.\n\n"
                    f"Folder: {folder}\n"
                    f"Layer group: {GROUP_TIMESTAMPED_VIEWSHEDS}",
                )
            else:
                QMessageBox.critical(
                    self,
                    "Step 4 Failed",
                    "Raster multiply/crop failed. Check the QGIS message log.",
                )
        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Step 4 Failed", str(exc))
        finally:
            self._set_busy(False)
            self._raster_task = None

    @pyqtSlot()
    def _on_raster_finished(self):
        """Legacy task callback — Step 4 now runs on the main thread."""
        self._take_raster_task()
        self._set_busy(False)

    @pyqtSlot()
    def _on_raster_terminated(self):
        """Legacy task callback — Step 4 now runs on the main thread."""
        self._take_raster_task()
        self._set_busy(False)
