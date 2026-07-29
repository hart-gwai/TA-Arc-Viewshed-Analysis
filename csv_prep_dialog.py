# -*- coding: utf-8 -*-
"""
Prepare Cell Site Data dialog: map CSV columns and build unique cell sites.
"""

import os

from qgis.PyQt.QtCore import pyqtSlot
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
    QStackedWidget,
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

    def __init__(self, iface, viewshed_engine=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.viewshed_engine = viewshed_engine
        self.engine = CsvPrepEngine()
        self._column_names = []
        self._csv_path = ""

        self.setWindowTitle("Prepare Cell Site Data")
        self.setMinimumWidth(560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Source ---
        source_group = QGroupBox("Data Source")
        source_layout = QVBoxLayout(source_group)

        self.radio_csv_file = QRadioButton("Import CSV file")
        self.radio_existing = QRadioButton("Use layer already in QGIS")
        self.radio_csv_file.setChecked(True)
        self.radio_csv_file.toggled.connect(self._on_source_mode_changed)
        source_layout.addWidget(self.radio_csv_file)
        source_layout.addWidget(self.radio_existing)

        self.source_stack = QStackedWidget()

        # Page 0: CSV file
        csv_page = QWidget()
        csv_form = QFormLayout(csv_page)
        csv_row = QHBoxLayout()
        self.csv_path_edit = QLineEdit()
        self.csv_path_edit.setPlaceholderText("Select a CSV file…")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_csv)
        csv_row.addWidget(self.csv_path_edit, stretch=1)
        csv_row.addWidget(browse_btn)
        csv_form.addRow("CSV file:", csv_row)
        self.source_stack.addWidget(csv_page)

        # Page 1: Existing layer
        layer_page = QWidget()
        layer_form = QFormLayout(layer_page)
        self.source_layer_combo = QgsMapLayerComboBox()
        self.source_layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.source_layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_form.addRow("Input layer:", self.source_layer_combo)
        self.source_stack.addWidget(layer_page)

        source_layout.addWidget(self.source_stack)
        layout.addWidget(source_group)

        # --- Column mapping ---
        mapping_group = QGroupBox("Column Mapping")
        mapping_layout = QVBoxLayout(mapping_group)
        mapping_layout.addWidget(
            QLabel(
                "Choose which CSV columns match each required field. "
                f"Creates <b>{PREPARED_LAYER_NAME}</b> and <b>{LAYER_UNIQUE_SITES}</b> "
                "saved next to the project file."
            )
        )

        form = QFormLayout()
        self.combo_x = self._make_combo()
        self.combo_y = self._make_combo()
        self.combo_tower = self._make_combo()
        self.combo_min_r = self._make_combo()
        self.combo_max_r = self._make_combo()
        self.combo_timestamp = self._make_combo()
        self.combo_observer_h = self._make_combo()

        self.x_label = QLabel("X / Longitude:")
        self.y_label = QLabel("Y / Latitude:")
        form.addRow(self.x_label, self.combo_x)
        form.addRow(self.y_label, self.combo_y)
        form.addRow("Tower / Site ID:", self.combo_tower)

        self.check_fill_from_buildings = QCheckBox(
            "Fill Cell_Site from building polygons (when Tower / Site ID is unmapped or empty)"
        )
        self.check_fill_from_buildings.toggled.connect(self._on_building_fill_changed)
        form.addRow(self.check_fill_from_buildings)

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
            "If the name is empty or the ping does not intersect a building, "
            "a coordinate-based UNLOCATED ID is assigned."
        )
        building_hint.setWordWrap(True)
        building_form.addRow(building_hint)
        form.addRow(self.building_options)

        form.addRow("Min radius (inner):", self.combo_min_r)
        form.addRow("Max radius (outer):", self.combo_max_r)
        form.addRow("Timestamp (optional):", self.combo_timestamp)
        form.addRow("Observer height (optional):", self.combo_observer_h)
        dem_hint = QLabel("If observer height is blank, the DEM below is sampled at each point.")
        dem_hint.setWordWrap(True)
        form.addRow(dem_hint)

        self.dem_layer_combo = QgsMapLayerComboBox()
        self.dem_layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        form.addRow("DEM (for observer height):", self.dem_layer_combo)

        mapping_layout.addLayout(form)

        arc_row = QHBoxLayout()
        self.radio_start_end = QRadioButton("Start + End azimuth columns")
        self.radio_az_delta = QRadioButton("Bearing / Azimuth + Delta (± degrees)")
        self.radio_start_end.setChecked(True)
        self.radio_start_end.toggled.connect(self._on_arc_mode_changed)
        arc_row.addWidget(self.radio_start_end)
        arc_row.addWidget(self.radio_az_delta)
        mapping_layout.addLayout(arc_row)

        arc_form = QFormLayout()
        self.combo_start_az = self._make_combo()
        self.combo_end_az = self._make_combo()
        self.combo_azimuth = self._make_combo()
        self.start_az_label = QLabel("Start azimuth:")
        self.end_az_label = QLabel("End azimuth:")
        self.az_label = QLabel("Bearing / Azimuth column:")
        self.delta_label = QLabel("Delta (± degrees):")
        self.spin_azimuth_delta = QDoubleSpinBox()
        self.spin_azimuth_delta.setRange(0.1, 180.0)
        self.spin_azimuth_delta.setDecimals(1)
        self.spin_azimuth_delta.setValue(45.0)
        self.spin_azimuth_delta.setSuffix("°")
        self.spin_azimuth_delta.setToolTip(
            "Arc spans from (bearing − delta) to (bearing + delta). "
            "Example: bearing 90° and delta 45° → 45° to 135°."
        )
        arc_form.addRow(self.start_az_label, self.combo_start_az)
        arc_form.addRow(self.end_az_label, self.combo_end_az)
        arc_form.addRow(self.az_label, self.combo_azimuth)
        arc_form.addRow(self.delta_label, self.spin_azimuth_delta)
        mapping_layout.addLayout(arc_form)
        layout.addWidget(mapping_group)

        refresh_btn = QPushButton("Reload columns from source")
        refresh_btn.clicked.connect(self._reload_columns)
        layout.addWidget(refresh_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Prepare Cell Site Data")
        buttons.accepted.connect(self._run_prepare)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_source_mode_changed()
        self._on_arc_mode_changed()
        self._on_building_fill_changed(self.check_fill_from_buildings.isChecked())

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
    def _on_source_mode_changed(self):
        self.source_stack.setCurrentIndex(0 if self.radio_csv_file.isChecked() else 1)
        show_xy = self.radio_csv_file.isChecked()
        self.x_label.setVisible(show_xy)
        self.combo_x.setVisible(show_xy)
        self.y_label.setVisible(show_xy)
        self.combo_y.setVisible(show_xy)
        self._reload_columns()

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
    def _on_layer_changed(self):
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

    def _set_combo_value(self, combo, value):
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setCurrentText(NONE_CHOICE)

    @pyqtSlot()
    def _reload_columns(self):
        if self.radio_csv_file.isChecked():
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
        else:
            layer = self.source_layer_combo.currentLayer()
            self._column_names = self._fields_from_layer(layer)

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
        if self.radio_csv_file.isChecked():
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

        layer = self.source_layer_combo.currentLayer()
        if layer is None:
            raise ValueError("Select an input layer.")
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            raise ValueError("Input layer must contain point geometries.")
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

            ping_layer = self.engine.prepare_layer(
                source,
                mapping,
                use_azimuth_mode=self.radio_az_delta.isChecked(),
                building_layer=building,
                building_field=building_field,
                fill_tower_from_buildings=fill_from_buildings,
                dem_layer=self.dem_layer_combo.currentLayer(),
                azimuth_delta=self.spin_azimuth_delta.value(),
            )

            sites_layer = None
            if self.viewshed_engine is not None:
                sites_layer = self.viewshed_engine.extract_unique_sites(
                    ping_layer,
                    dem_layer=self.dem_layer_combo.currentLayer(),
                )

            from .csv_prep_engine import _prepared_ping_output_path

            ping_count = ping_layer.featureCount()
            sites_count = sites_layer.featureCount() if sites_layer else 0
            self._log(
                f"Prepare Cell Site Data complete: {ping_count} ping(s), "
                f"{sites_count} unique site(s)."
            )

            if sites_layer is not None:
                QMessageBox.information(
                    self,
                    "Prepare Cell Site Data Complete",
                    f"Created '{PREPARED_LAYER_NAME}' ({ping_count} ping feature(s)) and "
                    f"'{LAYER_UNIQUE_SITES}' ({sites_count} tower(s)).\n\n"
                    f"Saved next to the project file:\n"
                    f"  {_prepared_ping_output_path()}\n"
                    f"  {self.viewshed_engine._unique_sites_output_path()}",
                )
            else:
                QMessageBox.information(
                    self,
                    "Prepare Cell Site Data Complete",
                    f"Created '{PREPARED_LAYER_NAME}' with {ping_count} feature(s).\n\n"
                    f"Saved to:\n{_prepared_ping_output_path()}",
                )
            self.accept()

        except Exception as exc:
            self._log(str(exc), Qgis.Critical)
            QMessageBox.critical(self, "Prepare Failed", str(exc))


def _is_none(value):
    return not value or value == NONE_CHOICE
