"""Qt-based graphical shell for LPM.

This module provides a PySide6 user interface that mirrors the behaviour
of the legacy Tkinter front-end while offering a more contemporary and
visually appealing experience.  It communicates with the LPM backend via
``ui.backend.LPMBackend`` and executes CLI operations in background
threads so the interface remains responsive.
"""

from __future__ import annotations

import re
import sys
import traceback
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
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

from .backend import (
    InstalledPackage,
    LPMBackend,
    PackageDetails,
    PackageSummary,
    Repository,
)


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
        self._repositories: dict[str, Repository] = {}
        self._pending_repo_selection: Optional[str] = None

        self.setWindowTitle("LPM Control Center")
        self.resize(1280, 768)
        self.setMinimumSize(1024, 640)

        self._build_ui()
        self._bind_events()
        self._apply_styles()

        # Populate UI with data.
        self._perform_search()
        self._refresh_installed()
        self._refresh_repositories()

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
        self.repository_group = QGroupBox("Repositories")
        top_layout.addWidget(self.search_group, 3)
        top_layout.addWidget(self.installed_group, 2)
        top_layout.addWidget(self.repository_group, 2)
        splitter.addWidget(top_panel)

        self.log_group = QGroupBox("Command log")
        self.build_group = QGroupBox("Build & install packages")

        bottom_panel = QWidget()
        bottom_layout = QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(10)
        bottom_layout.addWidget(self.build_group)
        bottom_layout.addWidget(self.log_group)
        splitter.addWidget(bottom_panel)
        splitter.setSizes([650, 280])

        self._build_search_panel()
        self._build_installed_panel()
        self._build_repository_panel()
        self._build_build_panel()
        self._build_log_panel()

        self.setCentralWidget(central)
        self._build_toolbar()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #ffffff;
            }
            QWidget {
                color: #1f2933;
            }
            QToolBar {
                background-color: #f3f0ff;
                spacing: 6px;
                border-bottom: 1px solid #d8b4fe;
            }
            QToolBar QToolButton {
                color: #4c1d95;
            }
            QStatusBar {
                background-color: #f3f0ff;
                color: #4c1d95;
                border-top: 1px solid #d8b4fe;
            }
            QGroupBox {
                border: 1px solid #d8b4fe;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 16px;
                background-color: #ffffff;
                color: #1f2933;
            }
            QGroupBox::title {
                color: #4c1d95;
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
                background-color: transparent;
            }
            QLabel {
                color: #1f2933;
            }
            QLineEdit,
            QSpinBox,
            QDoubleSpinBox,
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #cbd5f5;
                border-radius: 4px;
                padding: 6px;
                color: #1f2933;
                selection-background-color: #7c3aed;
                selection-color: #ffffff;
            }
            QLineEdit:focus,
            QSpinBox:focus,
            QDoubleSpinBox:focus,
            QTextEdit:focus {
                border: 1px solid #7c3aed;
            }
            QTableWidget {
                background-color: #ffffff;
                alternate-background-color: #f5f3ff;
                gridline-color: #d8b4fe;
                color: #1f2933;
                selection-background-color: #7c3aed;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #ede9fe;
                color: #4c1d95;
                border: 1px solid #d8b4fe;
                padding: 4px;
            }
            QPushButton {
                background-color: #7c3aed;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #6d28d9;
            }
            QPushButton:pressed {
                background-color: #5b21b6;
            }
            QPushButton:disabled {
                background-color: #ede9fe;
                color: #a78bfa;
            }
            QCheckBox {
                color: #1f2933;
                spacing: 6px;
            }
            QSplitter::handle {
                background-color: #ede9fe;
            }
            QTextEdit {
                selection-background-color: #7c3aed;
                selection-color: #ffffff;
            }
            """
        )

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

    def _build_repository_panel(self) -> None:
        layout = QGridLayout(self.repository_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.repo_table = QTableWidget(0, 3)
        self.repo_table.setHorizontalHeaderLabels(["Name", "URL", "Priority"])
        header = self.repo_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_table.verticalHeader().setVisible(False)
        self.repo_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.repo_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.repo_table.setAlternatingRowColors(True)
        self.repo_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout.addWidget(self.repo_table, 0, 0, 1, 4)

        name_label = QLabel("Name:")
        self.repo_name_input = QLineEdit()
        url_label = QLabel("URL:")
        self.repo_url_input = QLineEdit()

        priority_label = QLabel("Priority:")
        self.repo_priority_input = QSpinBox()
        self.repo_priority_input.setRange(0, 100)
        self.repo_priority_input.setValue(10)

        bias_label = QLabel("Bias:")
        self.repo_bias_input = QDoubleSpinBox()
        self.repo_bias_input.setDecimals(2)
        self.repo_bias_input.setRange(0.1, 10.0)
        self.repo_bias_input.setSingleStep(0.1)
        self.repo_bias_input.setValue(1.0)

        decay_label = QLabel("Decay:")
        self.repo_decay_input = QDoubleSpinBox()
        self.repo_decay_input.setDecimals(2)
        self.repo_decay_input.setRange(0.1, 1.0)
        self.repo_decay_input.setSingleStep(0.01)
        self.repo_decay_input.setValue(0.95)

        layout.addWidget(name_label, 1, 0)
        layout.addWidget(self.repo_name_input, 1, 1)
        layout.addWidget(url_label, 1, 2)
        layout.addWidget(self.repo_url_input, 1, 3)
        layout.addWidget(priority_label, 2, 0)
        layout.addWidget(self.repo_priority_input, 2, 1)
        layout.addWidget(bias_label, 2, 2)
        layout.addWidget(self.repo_bias_input, 2, 3)
        layout.addWidget(decay_label, 3, 2)
        layout.addWidget(self.repo_decay_input, 3, 3)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self.repo_new_button = QPushButton("New")
        self.repo_save_button = QPushButton("Save")
        self.repo_delete_button = QPushButton("Delete")
        self.repo_add_lpmbuild_button = QPushButton("Add LPMBuild repo")

        button_row.addWidget(self.repo_new_button)
        button_row.addWidget(self.repo_save_button)
        button_row.addWidget(self.repo_delete_button)
        button_row.addWidget(self.repo_add_lpmbuild_button)
        button_row.addStretch()

        layout.addLayout(button_row, 4, 0, 1, 4)

        self._clear_repository_form()

    def _build_build_panel(self) -> None:
        layout = QGridLayout(self.build_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        script_label = QLabel(".lpmbuild script:")
        self.build_script_input = QLineEdit()
        self.build_browse_script_button = QPushButton("Browse…")

        outdir_label = QLabel("Output directory (optional):")
        self.build_outdir_input = QLineEdit()
        self.build_outdir_button = QPushButton("Select…")

        options_row = QHBoxLayout()
        options_row.setSpacing(12)
        self.build_no_deps_check = QCheckBox("Skip dependency builds")
        self.build_force_check = QCheckBox("Force rebuild")
        options_row.addWidget(self.build_no_deps_check)
        options_row.addWidget(self.build_force_check)
        options_row.addStretch()

        self.buildpkg_button = QPushButton("Run buildpkg")

        pkg_label = QLabel("Local package file(s):")
        self.package_file_input = QLineEdit()
        self.package_file_button = QPushButton("Browse…")
        self.installpkg_button = QPushButton("Run installpkg")

        layout.addWidget(script_label, 0, 0)
        layout.addWidget(self.build_script_input, 0, 1)
        layout.addWidget(self.build_browse_script_button, 0, 2)
        layout.addWidget(outdir_label, 1, 0)
        layout.addWidget(self.build_outdir_input, 1, 1)
        layout.addWidget(self.build_outdir_button, 1, 2)
        layout.addLayout(options_row, 2, 0, 1, 3)
        layout.addWidget(self.buildpkg_button, 3, 0, 1, 3)

        layout.addWidget(pkg_label, 4, 0)
        layout.addWidget(self.package_file_input, 4, 1)
        layout.addWidget(self.package_file_button, 4, 2)
        layout.addWidget(self.installpkg_button, 5, 0, 1, 3)

    def _build_log_panel(self) -> None:
        layout = QVBoxLayout(self.log_group)
        layout.setContentsMargins(10, 15, 10, 10)
        layout.setSpacing(8)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("Command output will appear here…")

        layout.addWidget(self.log_text)

    # ------------------------------------------------------------------
    # Repository helpers
    def _clear_repository_form(self) -> None:
        self.repo_name_input.clear()
        self.repo_url_input.clear()
        self.repo_priority_input.setValue(10)
        self.repo_bias_input.setValue(1.0)
        self.repo_decay_input.setValue(0.95)

    def _set_repository_form(self, repo: Repository) -> None:
        self.repo_name_input.setText(repo.name)
        self.repo_url_input.setText(repo.url)
        self.repo_priority_input.setValue(int(repo.priority))
        self.repo_bias_input.setValue(float(repo.bias))
        self.repo_decay_input.setValue(float(repo.decay))

    def _refresh_repositories(self) -> None:
        self._run_task(
            self.backend.list_repositories,
            on_result=self._populate_repositories,
            status="Loading repositories…",
        )

    def _populate_repositories(self, repos: Sequence[Repository]) -> None:
        ordered = sorted(repos, key=lambda item: (item.priority, item.name.lower()))
        self.repo_table.setRowCount(len(ordered))
        self._repositories = {repo.name: repo for repo in ordered}

        for row, repo in enumerate(ordered):
            name_item = QTableWidgetItem(repo.name)
            url_item = QTableWidgetItem(repo.url)
            url_item.setToolTip(repo.url)
            priority_item = QTableWidgetItem(str(repo.priority))
            priority_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.repo_table.setItem(row, 0, name_item)
            self.repo_table.setItem(row, 1, url_item)
            self.repo_table.setItem(row, 2, priority_item)

        self.repo_table.resizeRowsToContents()

        if not ordered:
            self.repo_table.clearSelection()
            self._clear_repository_form()
        else:
            target = self._pending_repo_selection
            if target and target in self._repositories:
                for row, repo in enumerate(ordered):
                    if repo.name == target:
                        self.repo_table.selectRow(row)
                        break
                else:
                    self.repo_table.selectRow(0)
            else:
                self.repo_table.selectRow(0)
        self._pending_repo_selection = None

    def _current_repository(self) -> Optional[Repository]:
        current_row = self.repo_table.currentRow()
        if current_row < 0:
            return None
        name_item = self.repo_table.item(current_row, 0)
        if not name_item:
            return None
        return self._repositories.get(name_item.text())

    def _handle_repo_selection(self) -> None:
        repo = self._current_repository()
        if repo:
            self._set_repository_form(repo)

    def _gather_repository_from_form(self) -> Optional[Repository]:
        name = self.repo_name_input.text().strip()
        url = self.repo_url_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Repository", "Enter a repository name.")
            return None
        if not url:
            QMessageBox.warning(self, "Repository", "Enter a repository URL.")
            return None
        return Repository(
            name=name,
            url=url,
            priority=int(self.repo_priority_input.value()),
            bias=float(self.repo_bias_input.value()),
            decay=float(self.repo_decay_input.value()),
        )

    def _new_repository(self) -> None:
        self.repo_table.clearSelection()
        self._clear_repository_form()

    def _save_repository(self) -> None:
        repo = self._gather_repository_from_form()
        if not repo:
            return

        is_update = repo.name in self._repositories
        status = "Updating repository…" if is_update else "Adding repository…"
        action_text = "Updated" if is_update else "Added"

        def _after(_result: object) -> None:
            self._pending_repo_selection = repo.name
            self._append_log(f"{action_text} repository '{repo.name}'.")
            self._refresh_repositories()

        handler = self.backend.update_repository if is_update else self.backend.add_repository
        self._run_task(handler, repo, on_result=_after, status=status)

    def _delete_repository(self) -> None:
        repo = self._current_repository()
        if not repo:
            QMessageBox.information(self, "Repositories", "Select a repository first.")
            return
        confirmation = QMessageBox.question(
            self,
            "Remove repository",
            f"Remove repository '{repo.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        def _after(_result: object) -> None:
            self._append_log(f"Removed repository '{repo.name}'.")
            self._pending_repo_selection = None
            self._clear_repository_form()
            self._refresh_repositories()

        self._run_task(
            self.backend.remove_repository,
            repo.name,
            on_result=_after,
            status=f"Removing repository {repo.name}…",
        )

    def _add_lpmbuild_repository(self) -> None:
        self._run_task(
            self.backend.ensure_lpmbuild_repository,
            on_result=self._handle_lpmbuild_added,
            status="Ensuring lpmbuild repository…",
        )

    def _handle_lpmbuild_added(self, repo: Repository) -> None:
        self._append_log(f"Ensured lpmbuild repository at {repo.url}.")
        self._pending_repo_selection = repo.name
        self._set_repository_form(repo)
        self._refresh_repositories()

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
        self.repo_table.itemSelectionChanged.connect(self._handle_repo_selection)
        self.repo_new_button.clicked.connect(self._new_repository)
        self.repo_save_button.clicked.connect(self._save_repository)
        self.repo_delete_button.clicked.connect(self._delete_repository)
        self.repo_add_lpmbuild_button.clicked.connect(self._add_lpmbuild_repository)

        self.build_browse_script_button.clicked.connect(self._browse_lpmbuild_script)
        self.build_outdir_button.clicked.connect(self._browse_output_directory)
        self.buildpkg_button.clicked.connect(self._run_buildpkg)
        self.package_file_button.clicked.connect(self._browse_package_files)
        self.installpkg_button.clicked.connect(self._run_installpkg)

    # ------------------------------------------------------------------
    # Worker helpers
    def _run_task(
        self,
        func: Callable,
        *args,
        on_result: Optional[Callable[[object], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        status: Optional[str] = None,
        **worker_kwargs,
    ) -> None:
        worker = _Worker(func, *args, **worker_kwargs)
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

    # ------------------------------------------------------------------
    # Build helpers
    def _browse_lpmbuild_script(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select .lpmbuild script",
            "",
            "lpmbuild scripts (*.lpmbuild);;All files (*)",
        )
        if filename:
            self.build_script_input.setText(filename)

    def _browse_output_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select output directory")
        if directory:
            self.build_outdir_input.setText(directory)

    def _browse_package_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select package file(s)",
            "",
            "LPM packages (*.lpm);;All files (*)",
        )
        if files:
            self.package_file_input.setText("; ".join(files))

    def _run_buildpkg(self) -> None:
        script = self.build_script_input.text().strip()
        if not script:
            QMessageBox.information(self, "buildpkg", "Select a .lpmbuild script to run.")
            return
        outdir = self.build_outdir_input.text().strip() or None
        status = f"Building {Path(script).name}…"
        self._run_task(
            self.backend.build_package,
            script,
            on_result=self._handle_cli_result,
            status=status,
            outdir=outdir,
            no_deps=self.build_no_deps_check.isChecked(),
            force_rebuild=self.build_force_check.isChecked(),
        )

    def _run_installpkg(self) -> None:
        entries = self.package_file_input.text().strip()
        if not entries:
            QMessageBox.information(self, "installpkg", "Select one or more package files.")
            return
        files = [part.strip() for part in re.split(r"[;\n,]+", entries) if part.strip()]
        if not files:
            QMessageBox.information(self, "installpkg", "Select one or more package files.")
            return
        self._run_task(
            self.backend.install_local_packages,
            files,
            on_result=self._handle_cli_result,
            on_finished=self._refresh_installed,
            status="Installing local packages…",
        )


def main() -> None:
    """Entry point for launching the Qt interface."""

    app = QApplication(sys.argv)
    window = LPMWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    main()
