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
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsApplication, QgsMapLayerProxyModel, QgsMessageLog, QgsProject, Qgis

from .logic_engine import (
    TAArcViewshedEngine,
    GROUP_COMBINED_VIEWSHED,
    GROUP_MASTER_VIEWSHEDS,
    GROUP_VIEWSHED_WITH_TA,
    LAYER_CASCADE,
    LAYER_TA_POLYGONS,
    LAYER_UNIQUE_SITES,
    LOG_TAG,
)
from .csv_prep_dialog import CsvPrepDialog
from .csv_prep_engine import PREPARED_LAYER_NAME

STEP_PREPARE = "Prepare Cell Site Data"
STEP_TA_POLYGON = "TA Polygon Analysis"
STEP_VIEWSHED = "Viewshed Analysis"


class TAArcViewshedDialog(QDialog):
    """Main workflow dialog for the TA Arc & Viewshed Analysis plugin."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.engine = TAArcViewshedEngine(iface)
        self._viewshed_task = None
        self._raster_task = None

        self.setWindowTitle("Cell Site Data Analyser")
        self.setMinimumWidth(520)
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)

    def _prepared_ping_layer(self):
        layers = QgsProject.instance().mapLayersByName(PREPARED_LAYER_NAME)
        return layers[0] if layers else None

    def _build_ui(self):
        layout = QVBoxLayout(self)

        input_group = QGroupBox("Input Setup")
        input_layout = QVBoxLayout(input_group)

        dem_row = QHBoxLayout()
        dem_row.addWidget(QLabel("DEM Layer:"))
        self.dem_layer_combo = QgsMapLayerComboBox()
        self.dem_layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        dem_row.addWidget(self.dem_layer_combo, stretch=1)
        input_layout.addLayout(dem_row)

        self.progress_label = QLabel("")
        input_layout.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        input_layout.addWidget(self.progress_bar)

        layout.addWidget(input_group)

        step1_group = QGroupBox(f"Step 1 — {STEP_PREPARE}")
        step1_layout = QVBoxLayout(step1_group)
        step1_layout.addWidget(
            QLabel(
                f"Import a CSV, map columns, and create '{PREPARED_LAYER_NAME}' (EPSG:4326). "
                f"This layer is saved to a scenario folder next to the project file."
            )
        )
        self.btn_step1 = QPushButton(STEP_PREPARE)
        self.btn_step1.clicked.connect(self._open_prepare_cell_site_data)
        step1_layout.addWidget(self.btn_step1)
        layout.addWidget(step1_group)

        step2_group = QGroupBox("Step 2 — Run Full Analysis")
        step2_layout = QVBoxLayout(step2_group)
        step2_layout.addWidget(
            QLabel(
                "Extract unique cell sites, generate master viewsheds, build TA polygons, "
                "apply cascade overlap logic, and run viewshed analysis."
            )
        )
        
        # Add Input layer selection
        from qgis.PyQt.QtWidgets import QCheckBox, QDoubleSpinBox, QFormLayout, QWidget
        step2_form = QFormLayout()
        self.combo_step2_pings = QgsMapLayerComboBox()
        self.combo_step2_pings.setFilters(QgsMapLayerProxyModel.PointLayer)
        step2_form.addRow("Target Ping Layer:", self.combo_step2_pings)
        step2_layout.addLayout(step2_form)

        # --- Rolling Window Settings ---
        self.check_rolling_window = QCheckBox("Apply rolling time window across sequential timestamps")
        self.check_rolling_window.toggled.connect(self._on_rolling_toggled)
        step2_layout.addWidget(self.check_rolling_window)

        self.rolling_options = QWidget()
        rolling_form = QFormLayout(self.rolling_options)
        rolling_form.setContentsMargins(20, 0, 0, 0)
        self.spin_window_size = QDoubleSpinBox()
        self.spin_window_size.setDecimals(0)
        self.spin_window_size.setRange(1, 100)
        self.spin_window_size.setValue(3)
        self.spin_window_size.setSuffix(" sequential timestamps")
        rolling_form.addRow("Window Size:", self.spin_window_size)
        self.spin_step_size = QDoubleSpinBox()
        self.spin_step_size.setDecimals(0)
        self.spin_step_size.setRange(1, 100)
        self.spin_step_size.setValue(1)
        self.spin_step_size.setSuffix(" timestamp(s) forward")
        rolling_form.addRow("Step Size:", self.spin_step_size)
        step2_layout.addWidget(self.rolling_options)
        self.rolling_options.setVisible(False)

        self.btn_step2 = QPushButton("Run Full Analysis")
        self.btn_step2.clicked.connect(self._run_step2)
        step2_layout.addWidget(self.btn_step2)
        layout.addWidget(step2_group)

        layout.addStretch()

    def _log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    def _on_rolling_toggled(self, checked):
        self.rolling_options.setVisible(checked)

    def _set_busy(self, busy):
        for btn in (self.btn_step1, self.btn_step2):
            btn.setEnabled(not busy)
        self.dem_layer_combo.setEnabled(not busy)

    def _validate_prepared_ping(self):
        ping_layer = self._prepared_ping_layer()
        if ping_layer is None:
            QMessageBox.warning(
                self,
                "Missing Input",
                f"Run {STEP_PREPARE} first to create '{PREPARED_LAYER_NAME}'.",
            )
            return None
        return ping_layer

    def _validate_cell_site_data(self):
        ping_layer = self._validate_prepared_ping()
        if ping_layer is None:
            return None, None

        sites_layer = self.engine.find_layer_by_name(LAYER_UNIQUE_SITES)
        if sites_layer is None:
            QMessageBox.warning(
                self,
                "Missing Input",
                f"Run {STEP_PREPARE} first to create '{LAYER_UNIQUE_SITES}'.",
            )
            return None, None
        return ping_layer, sites_layer

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
    def _open_prepare_cell_site_data(self):
        dem_layer = self.dem_layer_combo.currentLayer()
        if not dem_layer:
            QMessageBox.warning(self, "Missing Input", "Please select a DEM Layer in the main window before running Step 1.")
            return
            
        dialog = CsvPrepDialog(self.iface, viewshed_engine=self.engine, dem_layer=dem_layer, parent=self)
        dialog.exec_()

    @pyqtSlot()
    def _run_step2(self):
        dem_layer = self._validate_dem()
        if dem_layer is None:
            return

        sites_layer = self.combo_step2_sites.currentLayer()
        if sites_layer is None:
            QMessageBox.warning(
                self,
                "Missing Input",
                "Please select a Target Unique Sites layer.",
            )
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        try:
            # Try to grab the suffix from the layer name (e.g. "_AOI_test_1")
            suffix = ""
            if "Unique_Cell_Sites" in sites_layer.name():
                suffix = sites_layer.name().replace("Unique_Cell_Sites", "")

            success = self.engine.generate_master_viewsheds(
                sites_layer,
                dem_layer,
                progress_callback=self._on_progress,
                suffix=suffix
            )
            self.progress_bar.setValue(100)
            count = len(self.engine.master_viewshed_paths)
            if success and count > 0:
                self._log(f"Step 2 complete: {count} master viewshed(s).")
                folder = self.engine.master_viewshed_output_dir or GROUP_MASTER_VIEWSHEDS
                skipped = getattr(self.engine, "skipped_master_viewsheds", [])
                skip_note = ""
                if skipped:
                    skip_note = (
                        f"\n\nSkipped {len(skipped)} tower(s) (outside DEM or failed):\n"
                        + "\n".join(f"  • {name}" for name, _ in skipped[:8])
                    )
                    if len(skipped) > 8:
                        skip_note += f"\n  …and {len(skipped) - 8} more (see QGIS log)"
                QMessageBox.information(
                    self,
                    "Step 2 Complete",
                    f"Generated {count} master viewshed raster(s).\n\n"
                    f"Folder: {folder}\n"
                    f"Layer group: {GROUP_MASTER_VIEWSHEDS}"
                    f"{skip_note}",
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
    def _run_step2(self):
        ping_layer = self.combo_step2_pings.currentLayer()
        if ping_layer is None:
            QMessageBox.warning(self, "Missing Input", "Please select a Target Ping Layer.")
            return

        dem_layer = self._validate_dem()
        if dem_layer is None:
            return

        suffix = ping_layer.name().replace(PREPARED_LAYER_NAME, "")

        if self._task_is_active(self._raster_task):
            QMessageBox.information(self, "Busy", "Analysis is already running.")
            return

        self._set_busy(True)
        self.progress_bar.setValue(0)

        rolling_window_opts = None
        if self.check_rolling_window.isChecked():
            rolling_window_opts = {
                "window_size": int(self.spin_window_size.value()),
                "step_size": int(self.spin_step_size.value()),
            }

        try:
            self.progress_label.setText("Extracting Unique Cell Sites...")
            QgsApplication.processEvents()
            
            sites_layer = self.engine.extract_unique_sites(
                ping_layer,
                dem_layer=dem_layer,
                suffix=suffix,
            )

            self.progress_label.setText("Generating Master Viewsheds...")
            QgsApplication.processEvents()
            
            self.engine.generate_master_viewsheds(
                sites_layer,
                dem_layer,
                progress_callback=self._on_progress,
                suffix=suffix
            )

            self.progress_label.setText("Building Cascade Polygons...")
            QgsApplication.processEvents()
            self._log(f"Starting cascade polygons generation with suffix '{suffix}'...")
            
            self.engine.run_cascade_polygons(
                ping_layer,
                sites_layer,
                rolling_window=rolling_window_opts,
                progress_callback=self._on_progress,
                suffix=suffix,
            )
            
            # Re-fetch based on suffix for Viewshed analysis
            from .logic_engine import LAYER_TA_POLYGONS, LAYER_CASCADE
            
            ta_polygons_layer = None
            cascade_layer = None
            
            # Check actual layer objects for matching suffix (handles timestamp fallback names)
            for layer in QgsProject.instance().mapLayers().values():
                if layer.name().startswith(LAYER_TA_POLYGONS) and layer.name().endswith(suffix):
                    ta_polygons_layer = layer
                if layer.name().startswith(LAYER_CASCADE) and layer.name().endswith(suffix):
                    cascade_layer = layer

            if ta_polygons_layer and cascade_layer:
                self.progress_bar.setValue(0)
                self.progress_label.setText("Multiplying and Cropping Viewsheds...")
                QgsApplication.processEvents()
                self._log("Cascade complete. Starting viewshed analysis...")
                
                success = self.engine.multiply_and_crop_rasters(
                    ta_polygons_layer,
                    cascade_layer,
                    dem_layer,
                    progress_callback=self._on_progress,
                    suffix=suffix,
                )
                
                if success:
                    QMessageBox.information(
                        self,
                        "Analysis Complete",
                        f"Generated Arc Polygons and clipped Viewsheds successfully using suffix '{suffix}'."
                    )
                else:
                    QMessageBox.critical(
                        self,
                        "Analysis Failed",
                        "Raster combination failed. Check message log.",
                    )
            else:
                QMessageBox.critical(
                    self,
                    "Analysis Failed",
                    "Could not find output polygon layers. Check message log.",
                )

        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Analysis Failed", str(exc))
        finally:
            self.progress_label.setText("")
            self._set_busy(False)
            self.progress_bar.setValue(100)

    @pyqtSlot()
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
