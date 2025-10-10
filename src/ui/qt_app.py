"""Qt-based graphical shell for LPM.

This module provides a PySide6 user interface that mirrors the behaviour
of the legacy Tkinter front-end while offering a more contemporary and
visually appealing experience.  It communicates with the LPM backend via
``src.ui.backend.LPMBackend`` and executes CLI operations in background
threads so the interface remains responsive.
"""

from __future__ import annotations

import sys
import traceback
from subprocess import CompletedProcess
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .backend import InstalledPackage, LPMBackend, PackageDetails, PackageSummary


class _WorkerSignals(QObject):
    """Signals emitted by ``_Worker`` instances."""

    result = Signal(object)
    error = Signal(str, str)
    finished = Signal()


class _Worker(QRunnable):
    """Utility class that executes a callable on a worker thread."""

    def __init__(self, fn: Callable, *args, **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self) -> None:  # pragma: no cover - Qt threads not exercised in tests
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # pylint: disable=broad-except
            tb = traceback.format_exc()
            self.signals.error.emit(str(exc), tb)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class LPMWindow(QMainWindow):
    """Main window of the Qt user interface."""

    def __init__(self, backend: Optional[LPMBackend] = None) -> None:
        super().__init__()

        self.backend = backend or LPMBackend()
        self.thread_pool = QThreadPool.globalInstance()

        self._search_items: dict[str, PackageSummary] = {}
        self._installed_items: dict[str, InstalledPackage] = {}

        self.setWindowTitle("LPM Control Center")
        self.resize(1280, 768)
        self.setMinimumSize(1024, 640)

        self._build_ui()
        self._bind_events()

        # Populate UI with data.
        self._perform_search()
        self._refresh_installed()

    # ------------------------------------------------------------------
    # UI construction helpers
    def _build_ui(self) -> None:
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(10, 10, 10, 10)
        central_layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Vertical)
        central_layout.addWidget(splitter)

        top_panel = QWidget()
        top_layout = QHBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)

        self.search_group = QGroupBox("Repository packages")
        self.installed_group = QGroupBox("Installed packages")
        top_layout.addWidget(self.search_group)
        top_layout.addWidget(self.installed_group)
        splitter.addWidget(top_panel)

        self.log_group = QGroupBox("Command log")
        splitter.addWidget(self.log_group)
        splitter.setSizes([600, 200])

        self._build_search_panel()
        self._build_installed_panel()
        self._build_log_panel()

        self.setCentralWidget(central)
        self._build_toolbar()

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize() * 1.2)

        refresh_action = QAction("Refresh repositories", self)
        refresh_action.triggered.connect(self._force_refresh)
        toolbar.addAction(refresh_action)

        installed_action = QAction("Refresh installed", self)
        installed_action.triggered.connect(self._refresh_installed)
        toolbar.addAction(installed_action)

        toolbar.addSeparator()

        clear_log_action = QAction("Clear log", self)
        clear_log_action.triggered.connect(self._clear_log)
        toolbar.addAction(clear_log_action)

        self.addToolBar(toolbar)

    def _build_search_panel(self) -> None:
        layout = QGridLayout(self.search_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        search_label = QLabel("Search pattern:")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Use shell-style wildcards, e.g. 'python*'")

        self.search_button = QPushButton("Search")
        self.refresh_button = QPushButton("Refresh repos")

        layout.addWidget(search_label, 0, 0)
        layout.addWidget(self.search_input, 0, 1)
        layout.addWidget(self.search_button, 0, 2)
        layout.addWidget(self.refresh_button, 0, 3)

        self.search_table = QTableWidget(0, 4)
        self.search_table.setHorizontalHeaderLabels(["Name", "Version", "Summary", "Repository"])
        self.search_table.horizontalHeader().setStretchLastSection(True)
        self.search_table.verticalHeader().setVisible(False)
        self.search_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.search_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.search_table.setAlternatingRowColors(True)
        self.search_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout.addWidget(self.search_table, 1, 0, 1, 4)

        self.install_button = QPushButton("Install")
        self.details_button = QPushButton("Package info")
        self.clear_log_button = QPushButton("Clear log")

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addWidget(self.install_button)
        button_row.addWidget(self.details_button)
        button_row.addWidget(self.clear_log_button)
        button_row.addStretch()

        layout.addLayout(button_row, 2, 0, 1, 4)

        details_label = QLabel("Details")
        layout.addWidget(details_label, 3, 0, 1, 4)

        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setMinimumHeight(160)
        layout.addWidget(self.details_text, 4, 0, 1, 4)

    def _build_installed_panel(self) -> None:
        layout = QGridLayout(self.installed_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.refresh_installed_button = QPushButton("Refresh")
        self.upgrade_button = QPushButton("Upgrade selected")
        self.remove_button = QPushButton("Remove selected")

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addWidget(self.refresh_installed_button)
        button_row.addWidget(self.upgrade_button)
        button_row.addWidget(self.remove_button)
        button_row.addStretch()

        layout.addLayout(button_row, 0, 0, 1, 1)

        self.installed_table = QTableWidget(0, 4)
        self.installed_table.setHorizontalHeaderLabels(["Name", "Version", "Installed", "Origin"])
        self.installed_table.horizontalHeader().setStretchLastSection(True)
        self.installed_table.verticalHeader().setVisible(False)
        self.installed_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.installed_table.setSelectionMode(QTableWidget.SelectionMode.MultiSelection)
        self.installed_table.setAlternatingRowColors(True)
        self.installed_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout.addWidget(self.installed_table, 1, 0)

    def _build_log_panel(self) -> None:
        layout = QVBoxLayout(self.log_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setSpacing(8)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("Command output will appear here…")

        layout.addWidget(self.log_text)

    # ------------------------------------------------------------------
    # Event handling
    def _bind_events(self) -> None:
        self.search_button.clicked.connect(self._perform_search)
        self.refresh_button.clicked.connect(self._force_refresh)
        self.clear_log_button.clicked.connect(self._clear_log)
        self.install_button.clicked.connect(self._install_selected)
        self.details_button.clicked.connect(self._show_selected_details)

        self.refresh_installed_button.clicked.connect(self._refresh_installed)
        self.upgrade_button.clicked.connect(self._upgrade_selected)
        self.remove_button.clicked.connect(self._remove_selected)

        self.search_input.returnPressed.connect(self._perform_search)
        self.search_table.itemSelectionChanged.connect(self._handle_search_selection)

    # ------------------------------------------------------------------
    # Worker helpers
    def _run_task(
        self,
        func: Callable,
        *args,
        on_result: Optional[Callable[[object], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        status: Optional[str] = None,
    ) -> None:
        worker = _Worker(func, *args)
        if status:
            self._set_status(status)
        if on_result:
            worker.signals.result.connect(on_result)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(self._handle_error)
        worker.signals.finished.connect(lambda: self._set_status("Ready"))
        self.thread_pool.start(worker)

    def _set_status(self, message: str) -> None:
        self.status_bar.showMessage(message)

    def _handle_error(self, message: str, traceback_text: str) -> None:
        self._append_log(f"Error: {message}\n{traceback_text}\n")
        QMessageBox.critical(self, "Operation failed", message)

    # ------------------------------------------------------------------
    # Search helpers
    def _perform_search(self) -> None:
        pattern = self.search_input.text().strip()
        self._run_task(
            self.backend.search,
            pattern,
            on_result=self._populate_search_results,
            status="Searching packages…",
        )

    def _force_refresh(self) -> None:
        self._run_task(
            self.backend.refresh_universe,
            on_result=self._populate_search_results,
            status="Refreshing repository metadata…",
        )

    def _populate_search_results(self, results: Sequence[PackageSummary]) -> None:
        self.search_table.setRowCount(len(results))
        self._search_items = {pkg.name: pkg for pkg in results}

        for row, pkg in enumerate(results):
            self.search_table.setItem(row, 0, QTableWidgetItem(pkg.name))
            self.search_table.setItem(row, 1, QTableWidgetItem(pkg.display_version))

            summary_item = QTableWidgetItem(pkg.summary or "")
            summary_item.setToolTip(pkg.summary or "")
            self.search_table.setItem(row, 2, summary_item)

            repo_item = QTableWidgetItem(pkg.repo or "")
            repo_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.search_table.setItem(row, 3, repo_item)

        self.search_table.resizeColumnsToContents()
        if results:
            self.search_table.selectRow(0)
        else:
            self.details_text.clear()

    def _handle_search_selection(self) -> None:
        current_row = self.search_table.currentRow()
        if current_row < 0:
            return
        name_item = self.search_table.item(current_row, 0)
        if not name_item:
            return
        name = name_item.text()
        self._run_task(
            self.backend.get_details,
            name,
            on_result=self._display_package_details,
            status=f"Loading details for {name}…",
        )

    def _display_package_details(self, details: PackageDetails) -> None:
        lines = [
            f"Name: {details.name}",
            f"Version: {details.display_version}",
            f"Repository: {details.repo}",
            f"Summary: {details.summary}",
            f"Homepage: {details.homepage}",
            f"License: {details.license}",
            "",
        ]

        def _format_list(title: str, items: Sequence[str]) -> None:
            if items:
                lines.append(f"{title}:")
                for item in items:
                    lines.append(f"  • {item}")
                lines.append("")

        _format_list("Provides", details.provides)
        _format_list("Requires", details.requires)
        _format_list("Conflicts", details.conflicts)
        _format_list("Obsoletes", details.obsoletes)
        _format_list("Recommends", details.recommends)
        _format_list("Suggests", details.suggests)

        if details.blob:
            lines.append("Metadata:")
            lines.append(details.blob)

        self.details_text.setPlainText("\n".join(lines))

    def _show_selected_details(self) -> None:
        current_row = self.search_table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "Package info", "Select a package first.")
            return
        self._handle_search_selection()

    # ------------------------------------------------------------------
    # Installed packages helpers
    def _refresh_installed(self) -> None:
        self._run_task(
            self.backend.list_installed,
            on_result=self._populate_installed_packages,
            status="Loading installed packages…",
        )

    def _populate_installed_packages(self, packages: Sequence[InstalledPackage]) -> None:
        self.installed_table.setRowCount(len(packages))
        self._installed_items = {pkg.name: pkg for pkg in packages}

        for row, pkg in enumerate(packages):
            self.installed_table.setItem(row, 0, QTableWidgetItem(pkg.name))
            self.installed_table.setItem(row, 1, QTableWidgetItem(pkg.display_version))
            self.installed_table.setItem(row, 2, QTableWidgetItem(pkg.installed_at))
            origin_item = QTableWidgetItem(pkg.origin)
            origin_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.installed_table.setItem(row, 3, origin_item)

        self.installed_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Mutating operations
    def _install_selected(self) -> None:
        pkg = self._current_search_package()
        if not pkg:
            QMessageBox.information(self, "Install", "Select a package to install.")
            return
        self._run_task(
            self.backend.install,
            [pkg.name],
            on_result=self._handle_cli_result,
            on_finished=self._refresh_installed,
            status=f"Installing {pkg.name}…",
        )

    def _upgrade_selected(self) -> None:
        names = self._selected_installed_packages()
        if not names:
            QMessageBox.information(
                self,
                "Upgrade",
                "Select one or more installed packages to upgrade.",
            )
            return
        self._run_task(
            self.backend.upgrade,
            names,
            on_result=self._handle_cli_result,
            on_finished=self._refresh_installed,
            status="Upgrading selected packages…",
        )

    def _remove_selected(self) -> None:
        names = self._selected_installed_packages()
        if not names:
            QMessageBox.information(
                self,
                "Remove",
                "Select one or more installed packages to remove.",
            )
            return
        confirmation = QMessageBox.question(
            self,
            "Confirm removal",
            "Remove the selected packages?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        self._run_task(
            self.backend.remove,
            names,
            on_result=self._handle_cli_result,
            on_finished=self._refresh_installed,
            status="Removing packages…",
        )

    def _current_search_package(self) -> Optional[PackageSummary]:
        current_row = self.search_table.currentRow()
        if current_row < 0:
            return None
        name_item = self.search_table.item(current_row, 0)
        if not name_item:
            return None
        return self._search_items.get(name_item.text())

    def _selected_installed_packages(self) -> list[str]:
        selected = []
        for item in self.installed_table.selectedItems():
            if item.column() == 0:
                selected.append(item.text())
        if selected:
            return sorted(set(selected))
        current_row = self.installed_table.currentRow()
        if current_row >= 0:
            name_item = self.installed_table.item(current_row, 0)
            if name_item:
                return [name_item.text()]
        return []

    def _handle_cli_result(self, process: CompletedProcess[str]) -> None:
        command = " ".join(str(arg) for arg in process.args)
        lines = [f"$ {command}"]
        if process.stdout:
            lines.append(process.stdout.strip())
        if process.stderr:
            lines.append(process.stderr.strip())
        lines.append(f"Exit status: {process.returncode}")
        lines.append("")
        self._append_log("\n".join(lines))
        if process.returncode != 0:
            QMessageBox.warning(
                self,
                "Command failed",
                "The command exited with a non-zero status. Check the log for details.",
            )

    def _append_log(self, text: str) -> None:
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _clear_log(self) -> None:
        self.log_text.clear()


def main() -> None:
    """Entry point for launching the Qt interface."""

    app = QApplication(sys.argv)
    window = LPMWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    main()
