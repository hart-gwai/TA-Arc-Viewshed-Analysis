import re

with open("csv_prep_dialog.py", "r", encoding="utf-8") as f:
    content = f.read()

# I am completely rebuilding the Column Mapping section in csv_prep_dialog.py to match the new exact order requested:
# i) Location Source (X/Y)
# ii) Time Stamp (Timestamp + Time bin size)
# iii) Tower Site ID (Tower + Fill from buildings)
# iv) Distance/Radius Source (TA available / No TA radio buttons, AOI, fixed radius)
# v) Azimuth (Start/End or Az/Delta)
# Observer height at the bottom

old_mapping_block = """        # --- Column mapping ---
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

        form.addRow("Timestamp (optional):", self.combo_timestamp)
        form.addRow("Observer height (optional):", self.combo_observer_h)
        dem_hint = QLabel("If observer height is blank, the DEM selected in the main window is sampled at each point.")
        dem_hint.setWordWrap(True)
        form.addRow(dem_hint)

        mapping_layout.addLayout(form)
        
        # --- Distance / Radius Setup ---
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
        mapping_layout.addWidget(dist_group)

        arc_row = QHBoxLayout()
        self.radio_start_end = QRadioButton("Start + End azimuth columns")
        self.radio_az_delta = QRadioButton("Bearing / Azimuth + Delta (A degrees)")
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
        self.delta_label = QLabel("Delta (A degrees):")
        self.spin_azimuth_delta = QDoubleSpinBox()
        self.spin_azimuth_delta.setRange(0.1, 180.0)
        self.spin_azimuth_delta.setDecimals(1)
        self.spin_azimuth_delta.setValue(45.0)
        self.spin_azimuth_delta.setSuffix("A")
        self.spin_azimuth_delta.setToolTip(
            "Arc spans from (bearing ^' delta) to (bearing + delta). "
            "Example: bearing 90A and delta 45A +' 45A to 135A."
        )
        arc_form.addRow(self.start_az_label, self.combo_start_az)
        arc_form.addRow(self.end_az_label, self.combo_end_az)
        arc_form.addRow(self.az_label, self.combo_azimuth)
        arc_form.addRow(self.delta_label, self.spin_azimuth_delta)
        mapping_layout.addLayout(arc_form)
        layout.addWidget(mapping_group)

        # --- Time Aggregation ---
        time_group = QGroupBox("Time Aggregation")
        time_layout = QFormLayout(time_group)
        self.spin_time_bin = QDoubleSpinBox()
        self.spin_time_bin.setDecimals(0)
        self.spin_time_bin.setRange(0, 1440)
        self.spin_time_bin.setValue(5)
        self.spin_time_bin.setSuffix(" minutes (0 to disable)")
        time_layout.addRow(QLabel("Aggregate ping timestamps and TA min/max radii by bin size:"))
        time_layout.addRow("Time Bin Size:", self.spin_time_bin)
        
        self.edit_suffix = QLineEdit()
        self.edit_suffix.setPlaceholderText("e.g. _North_AOI")
        time_layout.addRow("Scenario Suffix (Optional):", self.edit_suffix)
        layout.addWidget(time_group)"""

new_mapping_block = """        # --- Column mapping ---
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
        self.spin_time_bin.setValue(5)
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
        self.radio_az_delta = QRadioButton("Bearing / Azimuth + Delta (± degrees)")
        self.radio_start_end.setChecked(True)
        self.radio_start_end.toggled.connect(self._on_arc_mode_changed)
        arc_row.addWidget(self.radio_start_end)
        arc_row.addWidget(self.radio_az_delta)
        az_layout.addLayout(arc_row)

        arc_form = QFormLayout()
        self.start_az_label = QLabel("Start azimuth:")
        self.end_az_label = QLabel("End azimuth:")
        self.az_label = QLabel("Bearing / Azimuth column:")
        self.delta_label = QLabel("Delta (± degrees):")
        self.spin_azimuth_delta = QDoubleSpinBox()
        self.spin_azimuth_delta.setRange(0.1, 180.0)
        self.spin_azimuth_delta.setDecimals(1)
        self.spin_azimuth_delta.setValue(45.0)
        self.spin_azimuth_delta.setSuffix("°")
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
        layout.addWidget(obs_group)"""

content = content.replace(old_mapping_block, new_mapping_block)

# 2. Fix the auto-suffix generation in _run_prepare
old_run_prepare_suffix = """            suffix = self.edit_suffix.text().strip()
            
            from .csv_prep_engine import _prepared_ping_output_path"""

new_run_prepare_suffix = """            suffix = ""
            if not self.radio_ta_available.isChecked():
                if self.radio_no_ta_aoi.isChecked():
                    if aoi_layer and aoi_layer.name():
                        # sanitize layer name for filename
                        clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', aoi_layer.name())
                        suffix = f"_{clean_name}"
                else:
                    if fixed_max_r is not None:
                        suffix = f"_{int(fixed_max_r)}m cell site radius"

            from .csv_prep_engine import _prepared_ping_output_path"""

content = content.replace(old_run_prepare_suffix, new_run_prepare_suffix)

# 3. Clean up the degree symbols to prevent encoding issues
content = content.replace("A", "°").replace("^'", "-").replace("+'", "->").replace("?", "-")

with open("csv_prep_dialog.py", "w", encoding="utf-8") as f:
    f.write(content)
