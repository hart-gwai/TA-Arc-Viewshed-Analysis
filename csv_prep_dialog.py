# -*- coding: utf-8 -*-
"""
Prepare Cell Site Data dialog: map CSV columns and build unique cell sites.
"""

import os
import re
from qgis.PyQt.QtCore import pyqtSlot, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QDoubleSpinBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import (
    Qgis,
    QgsMapLayerProxyModel,
    QgsMessageLog,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .csv_prep_engine import (
    CsvPrepEngine,
    DEFAULT_BUILDING_NAME_FIELD,
    DEFAULT_CRS,
    NONE_CHOICE,
    PREPARED_LAYER_NAME,
    auto_guess_mapping,
    load_csv_as_layer,
    read_csv_column_names,
)
from .logic_engine import LAYER_UNIQUE_SITES, LOG_TAG


class CsvPrepDialog(QDialog):
    """Import CSV, map columns, and build prepared ping + unique cell site layers."""

    def __init__(self, iface, viewshed_engine=None, dem_layer=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.viewshed_engine = viewshed_engine
        self.dem_layer = dem_layer
        self.engine = CsvPrepEngine()
        self._column_names = []
        self._csv_path = ""

        self.setWindowTitle("Prepare Cell Site Data")
        self.setMinimumWidth(750)
        self.setMinimumHeight(650)
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)

        # --- Source ---
        source_group = QGroupBox("Data Source")
        source_layout = QVBoxLayout(source_group)

        csv_form = QFormLayout()
        csv_row = QHBoxLayout()
        self.csv_path_edit = QLineEdit()
        self.csv_path_edit.setPlaceholderText("Select a CSV file...")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_csv)
        csv_row.addWidget(self.csv_path_edit, stretch=1)
        csv_row.addWidget(browse_btn)
        csv_form.addRow("CSV file:", csv_row)
        source_layout.addLayout(csv_form)
        layout.addWidget(source_group)

        # Base combos
        self.combo_x = self._make_combo()
        self.combo_y = self._make_combo()
        self.combo_tower = self._make_combo()
        self.combo_min_r = self._make_combo()
        self.combo_max_r = self._make_combo()
        self.combo_timestamp = self._make_combo()
        self.combo_observer_h = self._make_combo()
        self.combo_start_az = self._make_combo()
        self.combo_end_az = self._make_combo()
        self.combo_azimuth = self._make_combo()

        # i) Location Source
        loc_group = QGroupBox("Location Source")
        loc_form = QFormLayout(loc_group)
        loc_form.addRow("X / Longitude:", self.combo_x)
        loc_form.addRow("Y / Latitude:", self.combo_y)
        layout.addWidget(loc_group)

        # ii) Time Stamp
        time_group = QGroupBox("Time Stamp")
        time_form = QFormLayout(time_group)
        time_form.addRow("Timestamp column (optional):", self.combo_timestamp)
        self.spin_time_bin = QDoubleSpinBox()
        self.spin_time_bin.setDecimals(0)
        self.spin_time_bin.setRange(0, 1440)
        self.spin_time_bin.setValue(1)
        self.spin_time_bin.setSuffix(" minutes (0 to disable)")
        time_form.addRow("Time Bin Size:", self.spin_time_bin)
        layout.addWidget(time_group)

        # iii) Tower Site ID
        tower_group = QGroupBox("Tower Site ID")
        tower_form = QFormLayout(tower_group)
        tower_form.addRow("Tower / Site ID column:", self.combo_tower)

        self.check_fill_from_buildings = QCheckBox(
            "Fill Cell_Site from building polygons (when Tower / Site ID is unmapped or empty)"
        )
        self.check_fill_from_buildings.toggled.connect(self._on_building_fill_changed)
        tower_form.addRow(self.check_fill_from_buildings)

        self.building_options = QWidget()
        building_form = QFormLayout(self.building_options)
        building_form.setContentsMargins(20, 0, 0, 0)
        self.building_layer_combo = QgsMapLayerComboBox()
        self.building_layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.building_layer_combo.layerChanged.connect(self._on_building_layer_changed)
        building_form.addRow("Building layer:", self.building_layer_combo)
        self.combo_building_field = self._make_combo()
        building_form.addRow("Building name field:", self.combo_building_field)
        building_hint = QLabel(
            f"Cell_Site uses the selected name field (default {DEFAULT_BUILDING_NAME_FIELD}). "
            "If empty or no intersection, an UNLOCATED ID is assigned."
        )
        building_hint.setWordWrap(True)
        building_form.addRow(building_hint)
        tower_form.addRow(self.building_options)
        layout.addWidget(tower_group)

        # iv) Distance / Radius Source
        dist_group = QGroupBox("Distance / Radius Source")
        dist_layout = QVBoxLayout(dist_group)

        self.radio_ta_available = QRadioButton("1) Timing Advance data available (map columns)")
        self.radio_no_ta = QRadioButton("2) No Timing Advance data")
        self.radio_ta_available.setChecked(True)
        self.radio_ta_available.toggled.connect(self._on_dist_mode_changed)

        dist_layout.addWidget(self.radio_ta_available)

        self.ta_options = QWidget()
        ta_form = QFormLayout(self.ta_options)
        ta_form.setContentsMargins(20, 0, 0, 0)
        ta_form.addRow("Min radius (inner):", self.combo_min_r)
        ta_form.addRow("Max radius (outer):", self.combo_max_r)
        dist_layout.addWidget(self.ta_options)

        dist_layout.addWidget(self.radio_no_ta)

        self.no_ta_options = QWidget()
        no_ta_layout = QVBoxLayout(self.no_ta_options)
        no_ta_layout.setContentsMargins(20, 0, 0, 0)

        self.radio_no_ta_aoi = QRadioButton("Calculate from Search Area (AOI) polygon")
        self.radio_no_ta_fixed = QRadioButton("Input a max radius from cell sites (min = 0)")
        self.radio_no_ta_aoi.setChecked(True)
        self.radio_no_ta_aoi.toggled.connect(self._on_no_ta_mode_changed)
        no_ta_layout.addWidget(self.radio_no_ta_aoi)

        self.aoi_options = QWidget()
        aoi_form = QFormLayout(self.aoi_options)
        aoi_form.setContentsMargins(20, 0, 0, 0)
        self.aoi_layer_combo = QgsMapLayerComboBox()
        self.aoi_layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.aoi_layer_combo.setAllowEmptyLayer(True)
        aoi_form.addRow("Search Area (AOI):", self.aoi_layer_combo)
        no_ta_layout.addWidget(self.aoi_options)

        no_ta_layout.addWidget(self.radio_no_ta_fixed)

        self.fixed_r_options = QWidget()
        fixed_r_form = QFormLayout(self.fixed_r_options)
        fixed_r_form.setContentsMargins(20, 0, 0, 0)
        self.spin_fixed_max_r = QDoubleSpinBox()
        self.spin_fixed_max_r.setRange(1, 100000)
        self.spin_fixed_max_r.setValue(5000)
        self.spin_fixed_max_r.setSuffix(" meters")
        fixed_r_form.addRow("Fixed Max Radius:", self.spin_fixed_max_r)
        no_ta_layout.addWidget(self.fixed_r_options)

        dist_layout.addWidget(self.no_ta_options)
        layout.addWidget(dist_group)

        # v) Azimuth
        az_group = QGroupBox("Azimuth")
        az_layout = QVBoxLayout(az_group)
        arc_row = QHBoxLayout()
        self.radio_start_end = QRadioButton("Start + End azimuth columns")
        self.radio_az_delta = QRadioButton("Bearing / Azimuth + Delta (+/- degrees)")
        self.radio_start_end.setChecked(True)
        self.radio_start_end.toggled.connect(self._on_arc_mode_changed)
        arc_row.addWidget(self.radio_start_end)
        arc_row.addWidget(self.radio_az_delta)
        az_layout.addLayout(arc_row)

        arc_form = QFormLayout()
        self.start_az_label = QLabel("Start azimuth:")
        self.end_az_label = QLabel("End azimuth:")
        self.az_label = QLabel("Bearing / Azimuth column:")
        self.delta_label = QLabel("Delta (+/- degrees):")
        self.spin_azimuth_delta = QDoubleSpinBox()
        self.spin_azimuth_delta.setRange(0.1, 180.0)
        self.spin_azimuth_delta.setDecimals(1)
        self.spin_azimuth_delta.setValue(45.0)
        self.spin_azimuth_delta.setSuffix(" deg")
        arc_form.addRow(self.start_az_label, self.combo_start_az)
        arc_form.addRow(self.end_az_label, self.combo_end_az)
        arc_form.addRow(self.az_label, self.combo_azimuth)
        arc_form.addRow(self.delta_label, self.spin_azimuth_delta)
        az_layout.addLayout(arc_form)
        layout.addWidget(az_group)

        # Observer Height (at bottom)
        obs_group = QGroupBox("Observer Height")
        obs_form = QFormLayout(obs_group)
        obs_form.addRow("Observer height (optional):", self.combo_observer_h)
        dem_hint = QLabel("If observer height is blank, the DEM selected in the main window is sampled at each point.")
        dem_hint.setWordWrap(True)
        obs_form.addRow(dem_hint)
        layout.addWidget(obs_group)

        scroll_area.setWidget(scroll_widget)
        main_layout.addWidget(scroll_area)

        # Buttons outside scroll area
        refresh_btn = QPushButton("Reload columns from source")
        refresh_btn.clicked.connect(self._reload_columns)
        main_layout.addWidget(refresh_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Prepare Cell Site Data")
        buttons.accepted.connect(self._run_prepare)
        buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)

        self._on_arc_mode_changed()
        self._on_dist_mode_changed()
        self._on_no_ta_mode_changed()
        self._on_building_fill_changed(False)

    @pyqtSlot(bool)
    def _on_building_fill_changed(self, enabled):
        self.building_options.setVisible(enabled)
        if enabled:
            self._on_building_layer_changed()

    def _make_combo(self):
        combo = QComboBox()
        combo.setEditable(False)
        return combo

    def _log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(message, LOG_TAG, level)

    @pyqtSlot()
    def _on_dist_mode_changed(self):
        is_ta = self.radio_ta_available.isChecked()
        self.ta_options.setVisible(is_ta)
        self.no_ta_options.setVisible(not is_ta)

    @pyqtSlot()
    def _on_no_ta_mode_changed(self):
        is_aoi = self.radio_no_ta_aoi.isChecked()
        self.aoi_options.setVisible(is_aoi)
        self.fixed_r_options.setVisible(not is_aoi)

    @pyqtSlot()
    def _on_arc_mode_changed(self):
        use_az_delta = self.radio_az_delta.isChecked()
        self.start_az_label.setVisible(not use_az_delta)
        self.combo_start_az.setVisible(not use_az_delta)
        self.end_az_label.setVisible(not use_az_delta)
        self.combo_end_az.setVisible(not use_az_delta)
        self.az_label.setVisible(use_az_delta)
        self.combo_azimuth.setVisible(use_az_delta)
        self.delta_label.setVisible(use_az_delta)
        self.spin_azimuth_delta.setVisible(use_az_delta)

    @pyqtSlot()
    def _browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select CSV file",
            "",
            "CSV files (*.csv *.txt);;All files (*.*)",
        )
        if path:
            self._csv_path = path
            self.csv_path_edit.setText(path)
            self._reload_columns()

    @pyqtSlot()
    def _on_building_layer_changed(self):
        layer = self.building_layer_combo.currentLayer()
        fields = self._fields_from_layer(layer)
        guess = DEFAULT_BUILDING_NAME_FIELD
        if guess not in fields:
            for candidate in ("BuildingNameEN", "BuildingNameTC", "BuildingName"):
                if candidate in fields:
                    guess = candidate
                    break
            else:
                guess = NONE_CHOICE
        self._populate_combo(self.combo_building_field, fields, guess)

    def _fields_from_layer(self, layer):
        if layer is None:
            return []
        return [field.name() for field in layer.fields()]

    def _populate_combo(self, combo, names, guess=None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(NONE_CHOICE)
        for name in names:
            combo.addItem(name)
        if guess and guess in names:
            combo.setCurrentText(guess)
        elif guess == NONE_CHOICE:
            combo.setCurrentText(NONE_CHOICE)
        combo.blockSignals(False)

    @pyqtSlot()
    def _reload_columns(self):
        path = self.csv_path_edit.text().strip()
        if not path or not os.path.isfile(path):
            self._column_names = []
        else:
            try:
                self._column_names = read_csv_column_names(path)
                self._csv_path = path
            except OSError as exc:
                QMessageBox.warning(self, "CSV Error", str(exc))
                return

        guesses = auto_guess_mapping(self._column_names) if self._column_names else {}

        for combo, key in (
            (self.combo_x, "x"),
            (self.combo_y, "y"),
            (self.combo_tower, "tower"),
            (self.combo_min_r, "min_r"),
            (self.combo_max_r, "max_r"),
            (self.combo_start_az, "start_az"),
            (self.combo_end_az, "end_az"),
            (self.combo_azimuth, "azimuth"),
            (self.combo_timestamp, "timestamp"),
            (self.combo_observer_h, "observer_h"),
        ):
            self._populate_combo(combo, self._column_names, guesses.get(key, NONE_CHOICE))

        self._on_building_layer_changed()

    def _resolve_source_layer(self):
        path = self.csv_path_edit.text().strip()
        if not path or not os.path.isfile(path):
            raise ValueError("Select a valid CSV file.")

        x_field = self.combo_x.currentText()
        y_field = self.combo_y.currentText()
        if _is_none(x_field) or _is_none(y_field):
            raise ValueError("X / Longitude and Y / Latitude columns are required for CSV import.")

        layer = load_csv_as_layer(path, x_field, y_field, DEFAULT_CRS.authid())
        if not layer.isValid():
            raise ValueError(
                "Could not load CSV as a point layer. Check file path, delimiter, "
                "and X/Y column names."
            )
        return layer

    def _current_mapping(self):
        return {
            "tower": self.combo_tower.currentText(),
            "min_r": self.combo_min_r.currentText(),
            "max_r": self.combo_max_r.currentText(),
            "start_az": self.combo_start_az.currentText(),
            "end_az": self.combo_end_az.currentText(),
            "azimuth": self.combo_azimuth.currentText(),
            "timestamp": self.combo_timestamp.currentText(),
            "observer_h": self.combo_observer_h.currentText(),
        }

    @pyqtSlot()
    def _run_prepare(self):
        try:
            source = self._resolve_source_layer()
            mapping = self._current_mapping()

            building = None
            building_field = None
            fill_from_buildings = self.check_fill_from_buildings.isChecked()
            if fill_from_buildings:
                building = self.building_layer_combo.currentLayer()
                building_field = self.combo_building_field.currentText()
                if building is None:
                    raise ValueError("Select a building polygon layer.")
                if _is_none(building_field):
                    raise ValueError("Select a building name field.")

            aoi_layer = None
            fixed_max_r = None
            suffix = "_with TA"

            if not self.radio_ta_available.isChecked():
                mapping["min_r"] = NONE_CHOICE
                mapping["max_r"] = NONE_CHOICE
                if self.radio_no_ta_aoi.isChecked():
                    aoi_layer = self.aoi_layer_combo.currentLayer()
                    if aoi_layer is None:
                        raise ValueError("Please select a Search Area (AOI) polygon layer.")
                    if aoi_layer.name():
                        clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', aoi_layer.name())
                        suffix = f"_{clean_name}"
                else:
                    fixed_max_r = self.spin_fixed_max_r.value()
                    suffix = f"_{int(fixed_max_r)}m cell site radius"

            ping_layer = self.engine.prepare_layer(
                source,
                mapping,
                use_azimuth_mode=self.radio_az_delta.isChecked(),
                building_layer=building,
                building_field=building_field,
                fill_tower_from_buildings=fill_from_buildings,
                dem_layer=self.dem_layer,
                azimuth_delta=self.spin_azimuth_delta.value(),
                time_bin_minutes=int(self.spin_time_bin.value()),
                aoi_layer=aoi_layer,
                suffix=suffix,
                fixed_max_r=fixed_max_r,
            )

            from .csv_prep_engine import _prepared_ping_output_path

            ping_count = ping_layer.featureCount()
            self._log(
                f"Prepare Cell Site Data complete: {ping_count} ping(s)."
            )

            QMessageBox.information(
                self,
                "Prepare Cell Site Data Complete",
                f"Created '{PREPARED_LAYER_NAME}{suffix}' with {ping_count} feature(s).\n\n"
                f"Saved to scenario folder:\n{_prepared_ping_output_path(suffix)}",
            )
            self.accept()

        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Prepare Failed", str(exc))


def _is_none(value):
    return not value or value == NONE_CHOICE