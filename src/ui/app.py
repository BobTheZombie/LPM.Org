"""Tkinter based graphical shell for LPM."""
from __future__ import annotations

import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from subprocess import CompletedProcess
from tkinter import messagebox, ttk
from typing import Iterable, Optional

from .backend import (
    InstalledPackage,
    LPMBackend,
    PackageDetails,
    PackageSummary,
)


class LPMApplication(tk.Tk):
    """Simple graphical front-end around the existing CLI commands."""

    def __init__(self, backend: Optional[LPMBackend] = None) -> None:
        super().__init__()
        self.backend = backend or LPMBackend()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._search_items: dict[str, PackageSummary] = {}
        self._installed_items: dict[str, InstalledPackage] = {}

        self.title("LPM Control Center")
        self.geometry("1100x720")
        self.minsize(900, 600)

        self.status_var = tk.StringVar(value="Ready")
        self.search_var = tk.StringVar()

        self._build_layout()
        self._bind_events()
        self._perform_search()
        self._refresh_installed()

    # ------------------------------------------------------------------
    # UI construction helpers
    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=4)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 10, 10, 5))
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        top.rowconfigure(0, weight=1)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.grid(row=1, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=0)

        self._build_search_panel(top)
        self._build_installed_panel(top)
        self._build_log_panel(bottom)

    def _build_search_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Repository packages")
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Search pattern:").grid(row=0, column=0, padx=(0, 5), pady=5)
        entry = ttk.Entry(controls, textvariable=self.search_var)
        entry.grid(row=0, column=1, sticky="ew", pady=5)
        entry.focus_set()

        ttk.Button(controls, text="Search", command=self._perform_search).grid(
            row=0, column=2, padx=5, pady=5
        )
        ttk.Button(controls, text="Refresh repos", command=self._force_refresh).grid(
            row=0, column=3, padx=5, pady=5
        )

        self.search_tree = ttk.Treeview(
            frame,
            columns=("version", "summary", "repo"),
            show="headings",
            selectmode="browse",
        )
        self.search_tree.heading("version", text="Version")
        self.search_tree.heading("summary", text="Summary")
        self.search_tree.heading("repo", text="Repository")
        self.search_tree.column("version", width=160, stretch=False)
        self.search_tree.column("summary", width=360, stretch=True)
        self.search_tree.column("repo", width=120, stretch=False)
        self.search_tree.grid(row=1, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.search_tree.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        self.search_tree.configure(yscrollcommand=yscroll.set)

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)

        ttk.Button(btns, text="Install", command=self._install_selected).grid(
            row=0, column=0, padx=5
        )
        ttk.Button(btns, text="Package info", command=self._show_selected_details).grid(
            row=0, column=1, padx=5
        )
        ttk.Button(btns, text="Clear log", command=self._clear_log).grid(row=0, column=2, padx=5)

        info_label = ttk.Label(frame, text="Details")
        info_label.grid(row=3, column=0, sticky="w", pady=(10, 2))

        self.info_text = tk.Text(frame, height=12, wrap="word")
        self.info_text.grid(row=4, column=0, sticky="nsew")
        self.info_text.configure(state="disabled")

        info_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.info_text.yview)
        info_scroll.grid(row=4, column=1, sticky="ns")
        self.info_text.configure(yscrollcommand=info_scroll.set)

    def _build_installed_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Installed packages")
        frame.grid(row=0, column=1, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)

        ttk.Button(controls, text="Refresh installed", command=self._refresh_installed).grid(
            row=0, column=0, padx=5, pady=5, sticky="w"
        )
        ttk.Button(controls, text="Upgrade selected", command=self._upgrade_selected).grid(
            row=0, column=1, padx=5, pady=5
        )
        ttk.Button(controls, text="Remove selected", command=self._remove_selected).grid(
            row=0, column=2, padx=5, pady=5
        )

        self.installed_tree = ttk.Treeview(
            frame,
            columns=("version", "installed", "origin"),
            show="headings",
            selectmode="extended",
        )
        self.installed_tree.heading("version", text="Version")
        self.installed_tree.heading("installed", text="Installed")
        self.installed_tree.heading("origin", text="Origin")
        self.installed_tree.column("version", width=170, stretch=False)
        self.installed_tree.column("installed", width=160, stretch=False)
        self.installed_tree.column("origin", width=100, stretch=False)
        self.installed_tree.grid(row=1, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.installed_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.installed_tree.configure(yscrollcommand=scroll.set)

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, height=8, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        status = ttk.Label(parent, textvariable=self.status_var)
        status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _bind_events(self) -> None:
        self.bind("<Return>", lambda _event: self._perform_search())
        self.search_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected_details())

    # ------------------------------------------------------------------
    # Background execution helpers
    def _run_async(self, func, *args, on_success=None, description: str | None = None):
        if description:
            self._set_status(description)

        future = self.executor.submit(func, *args)

        def _callback(fut):
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - UI feedback
                self.after(0, lambda: self._handle_error(exc))
            else:
                if on_success:
                    self.after(0, lambda: on_success(result))
                else:
                    self.after(0, lambda: self._set_status("Ready"))

        future.add_done_callback(_callback)

    def _handle_error(self, exc: Exception) -> None:
        self._set_status("Error")
        messagebox.showerror("LPM UI", str(exc))
        self._append_log(f"Error: {exc}")

    # ------------------------------------------------------------------
    # Logging helpers
    def _append_log(self, text: str) -> None:
        if not text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ------------------------------------------------------------------
    # Search panel callbacks
    def _perform_search(self) -> None:
        pattern = self.search_var.get().strip()
        desc = f"Searching for '{pattern or '*'}'"
        self._run_async(self.backend.search, pattern, on_success=self._update_search_results, description=desc)

    def _force_refresh(self) -> None:
        self._run_async(
            self.backend.refresh_universe,
            on_success=self._update_search_results,
            description="Refreshing repository metadata",
        )

    def _update_search_results(self, results: Iterable[PackageSummary]) -> None:
        self._search_items.clear()
        for item in self.search_tree.get_children():
            self.search_tree.delete(item)
        for pkg in results:
            iid = self.search_tree.insert(
                "",
                "end",
                values=(pkg.display_version, pkg.summary, pkg.repo),
                text=pkg.name,
            )
            self._search_items[iid] = pkg
        self._set_status(f"{len(self._search_items)} packages listed")

    def _show_selected_details(self) -> None:
        selected = self.search_tree.selection()
        if not selected:
            return
        pkg = self._search_items.get(selected[0])
        if not pkg:
            return
        self._run_async(
            self.backend.get_details,
            pkg.name,
            on_success=self._display_details,
            description=f"Loading {pkg.name} details",
        )

    def _display_details(self, details: PackageDetails) -> None:
        lines = [
            f"Name:       {details.name}",
            f"Version:    {details.display_version}",
            f"Summary:    {details.summary or '-'}",
            f"Repository: {details.repo or '-'}",
            f"Homepage:   {details.homepage or '-'}",
            f"License:    {details.license or '-'}",
            f"Provides:   {', '.join(details.provides) or '-'}",
            f"Requires:   {', '.join(details.requires) or '-'}",
            f"Conflicts:  {', '.join(details.conflicts) or '-'}",
            f"Obsoletes:  {', '.join(details.obsoletes) or '-'}",
            f"Recommends: {', '.join(details.recommends) or '-'}",
            f"Suggests:   {', '.join(details.suggests) or '-'}",
            f"Blob:       {details.blob or '-'}",
        ]
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("end", "\n".join(lines))
        self.info_text.configure(state="disabled")
        self._set_status("Ready")

    # ------------------------------------------------------------------
    # Installed panel callbacks
    def _refresh_installed(self) -> None:
        self._run_async(
            self.backend.list_installed,
            on_success=self._update_installed,
            description="Loading installed packages",
        )

    def _update_installed(self, packages: Iterable[InstalledPackage]) -> None:
        self._installed_items.clear()
        for item in self.installed_tree.get_children():
            self.installed_tree.delete(item)
        for pkg in packages:
            iid = self.installed_tree.insert(
                "",
                "end",
                values=(pkg.display_version, pkg.installed_at, pkg.origin),
                text=pkg.name,
            )
            self._installed_items[iid] = pkg
        self._set_status(f"{len(self._installed_items)} installed packages")

    def _selected_installed_names(self) -> list[str]:
        names: list[str] = []
        for iid in self.installed_tree.selection():
            pkg = self._installed_items.get(iid)
            if pkg:
                names.append(pkg.name)
        return names

    def _remove_selected(self) -> None:
        names = self._selected_installed_names()
        if not names:
            messagebox.showinfo("LPM UI", "Select at least one installed package to remove.")
            return
        self._run_async(
            self.backend.remove,
            names,
            on_success=self._handle_cli_result,
            description=f"Removing {', '.join(names)}",
        )

    def _upgrade_selected(self) -> None:
        names = self._selected_installed_names()
        if not names:
            self._run_async(
                self.backend.upgrade,
                None,
                on_success=self._handle_cli_result,
                description="Upgrading all packages",
            )
            return
        self._run_async(
            self.backend.upgrade,
            names,
            on_success=self._handle_cli_result,
            description=f"Upgrading {', '.join(names)}",
        )

    # ------------------------------------------------------------------
    # CLI results
    def _install_selected(self) -> None:
        selected = self.search_tree.selection()
        if not selected:
            messagebox.showinfo("LPM UI", "Select a package to install from the repository list.")
            return
        pkg = self._search_items.get(selected[0])
        if not pkg:
            return
        self._run_async(
            self.backend.install,
            [pkg.name],
            on_success=self._handle_cli_result,
            description=f"Installing {pkg.name}",
        )

    def _handle_cli_result(self, result: CompletedProcess[str]) -> None:
        self._set_status(f"Command finished with exit code {result.returncode}")
        output = []
        if result.stdout:
            output.append(result.stdout.strip())
        if result.stderr:
            output.append(result.stderr.strip())
        self._append_log("\n\n".join(filter(None, output)))
        if result.returncode != 0:
            messagebox.showwarning("LPM UI", f"Command exited with {result.returncode}. See log for details.")
        self._refresh_installed()

    # ------------------------------------------------------------------
    def destroy(self) -> None:  # pragma: no cover - UI lifecycle
        self.executor.shutdown(wait=False)
        super().destroy()


def main() -> None:
    app = LPMApplication()
    app.mainloop()


if __name__ == "__main__":
    main()
