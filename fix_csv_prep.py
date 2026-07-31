import re

with open("csv_prep_dialog.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Remove self._on_source_mode_changed() from _build_ui
content = content.replace("        self._on_source_mode_changed()\n", "")

# 2. Remove _on_source_mode_changed method entirely
pattern1 = r"    @pyqtSlot\(\)\n    def _on_source_mode_changed\(self\):\n(?:        .*?\n)+"
content = re.sub(pattern1, "", content)

# 3. Fix _reload_columns
old_reload = """    @pyqtSlot()
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
            self._column_names = self._fields_from_layer(layer)"""

new_reload = """    @pyqtSlot()
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
                return"""

content = content.replace(old_reload, new_reload)

# 4. Fix _resolve_source_layer
old_resolve = """    def _resolve_source_layer(self):
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
        return layer"""

new_resolve = """    def _resolve_source_layer(self):
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
        return layer"""

content = content.replace(old_resolve, new_resolve)

with open("csv_prep_dialog.py", "w", encoding="utf-8") as f:
    f.write(content)
