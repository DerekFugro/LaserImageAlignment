"""Left-side panels: run list and QC & alignment status (with Locate… flow)."""
from __future__ import annotations


from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import discovery as disc
from core.qc import QCCheck, QCReport, Severity
from . import styles


class RunListPanel(QWidget):
    """List of discovered runs."""

    run_selected = Signal(str)  # run_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.title_label = QLabel("Runs")
        self.run_list = QListWidget()

    # --- Layout ---
    def _create_layouts(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.title_label)
        self.main_layout.addWidget(self.run_list)

    # --- Signals & Slots ---
    def _connect_signals(self):
        self.run_list.currentItemChanged.connect(self._on_run_list_current_item_changed)

    # --- Styling ---
    def _apply_styles(self):
        self.title_label.setStyleSheet(styles.GROUP_TITLE)
        self.run_list.setStyleSheet(styles.RUN_LIST)

    # --- Initial State ---
    def _load_initial_state(self):
        pass

    # --- Business Logic ---
    def populate_runs(self, runs: list[disc.RunPaths]):
        self.run_list.clear()
        for run in runs:
            n_missing = len(run.missing())
            label = run.run_id + (f"   ({n_missing} missing)" if n_missing else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, run.run_id)
            self.run_list.addItem(item)

    def _on_run_list_current_item_changed(self, current, _previous):
        if current is not None:
            self.run_selected.emit(current.data(Qt.UserRole))


class QCPanel(QWidget):
    """QC & alignment status rows; missing inputs get a Locate… button."""

    locate_requested = Signal(str, str)  # (input key, chosen path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._create_widgets()
        self._create_layouts()
        self._connect_signals()
        self._apply_styles()
        self._load_initial_state()

    # --- Widget Construction ---
    def _create_widgets(self):
        self.title_label = QLabel("QC & alignment status")
        self.rows_container = QWidget()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.rows_container)

    # --- Layout ---
    def _create_layouts(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.title_label)
        self.main_layout.addWidget(self.scroll_area)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(3)
        self.rows_layout.addStretch()

    # --- Signals & Slots ---
    def _connect_signals(self):
        pass

    # --- Styling ---
    def _apply_styles(self):
        self.title_label.setStyleSheet(styles.GROUP_TITLE)
        self.rows_container.setStyleSheet(styles.QC_PANEL)

    # --- Initial State ---
    def _load_initial_state(self):
        pass

    # --- Business Logic ---
    def _create_qc_row_widget(self, check: QCCheck) -> QWidget:
        """Factory method — creates and returns ONE QC status row."""
        row = QFrame()
        row.setObjectName("qc_row")
        severity_label = QLabel(check.severity.value)
        severity_label.setFixedWidth(44)
        severity_label.setAlignment(Qt.AlignCenter)
        severity_label.setStyleSheet(
            f"background-color: {styles.SEVERITY_COLORS[check.severity.value]};"
            "color: white; border-radius: 3px; font-weight: bold; padding: 2px;"
        )
        text_label = QLabel(f"{check.name}\n{check.message}")
        text_label.setWordWrap(True)
        text_label.setToolTip(check.message)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(severity_label, alignment=Qt.AlignTop)
        layout.addWidget(text_label, stretch=1)
        if check.check_id.startswith("presence.") and check.severity == Severity.FAIL:
            key = check.values.get("key", "")
            locate_button = QPushButton("Locate…")
            locate_button.setStyleSheet(styles.BUTTON_SMALL)
            locate_button.clicked.connect(lambda _=False, k=key: self._on_locate_button_clicked(k))
            layout.addWidget(locate_button, alignment=Qt.AlignTop)
        return row

    def populate_report(self, report: QCReport | None):
        """Clears and repopulates the QC rows (failures first)."""
        self._clear_rows()
        if report is None:
            return
        order = {Severity.FAIL: 0, Severity.WARN: 1, Severity.INFO: 2, Severity.PASS: 3}
        for check in sorted(report.checks, key=lambda c: order[c.severity]):
            row = self._create_qc_row_widget(check)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)

    def _clear_rows(self):
        """Remove all dynamically generated rows safely."""
        while self.rows_layout.count() > 1:  # keep the trailing stretch
            child = self.rows_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _on_locate_button_clicked(self, key: str):
        description = disc.KEY_DESCRIPTIONS.get(key, key)
        if key == disc.KEY_IMAGES:
            chosen = QFileDialog.getExistingDirectory(self, f"Locate: {description}")
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, f"Locate: {description}")
        if chosen:
            self.locate_requested.emit(key, chosen)
