from __future__ import annotations

import threading
import time
from typing import Any

from auto_slicer.browse import gcode_root, model_root
from auto_slicer.cura_config import PrinterProfile, discover_printers
from auto_slicer.engine import EngineError, locate_cura
from auto_slicer.machine_sync import (
    apply_filament,
    filter_owned_orca,
    machine_for_engine,
    merge_owned,
    owned_for_printer,
    with_synced_bed,
)
from auto_slicer.orca_config import active_orca_dir, discover_filaments, discover_orca_printers
from auto_slicer.paths import safe_segment
from auto_slicer.slicing import (
    layout_for_group,
    layout_for_model,
    list_models,
    output_group_path,
    output_model_path,
    prepare_slice,
    run_prepared_slice,
)
from auto_slicer.store import Store


class JobRunner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._printer_lock = threading.Lock()
        self._printer_cache: dict[str, tuple[float, list[PrinterProfile]]] = {}
        self._filament_cache: tuple[float, list[dict[str, Any]]] | None = None

    def printers(self, *, catalog: bool = False) -> list[PrinterProfile]:
        engine_name = self.store.load_settings().slicer_engine
        found = self._discovered(engine_name)
        if engine_name == "orca" and not catalog:
            return filter_owned_orca(found, self.ensure_owned())
        return found

    def ensure_owned(self) -> list[dict[str, Any]]:
        existing = self.store.load_owned()
        merged = merge_owned(existing, self._discovered("cura"), self._discovered("orca"))
        if merged != existing:
            self.store.save_owned(merged)
        return merged

    def _discovered(self, engine_name: str) -> list[PrinterProfile]:
        with self._printer_lock:
            now = time.monotonic()
            cached = self._printer_cache.get(engine_name)
            if cached is not None and now - cached[0] < 5:
                return cached[1]
            if engine_name == "orca":
                found = discover_orca_printers(active_orca_dir())
            else:
                _engine, resources, _libs = locate_cura(self.store.paths)
                found = discover_printers(self.store.paths.cura_config_dir, resources)
            self._printer_cache[engine_name] = (now, found)
            return found

    def filament_catalog(self) -> list[dict[str, Any]]:
        with self._printer_lock:
            now = time.monotonic()
            if self._filament_cache is not None and now - self._filament_cache[0] < 30:
                return self._filament_cache[1]
            found = discover_filaments(active_orca_dir())
            self._filament_cache = (now, found)
            return found

    def start_batch(self) -> dict[str, Any]:
        settings = self.store.load_settings()
        if not settings.output_folder_name:
            raise ValueError("output folder name is required")
        return self._start("batch", None, None)

    def start_test(self, printer_id: str, item_type: str, item_name: str) -> dict[str, Any]:
        settings = self.store.load_settings()
        if not settings.output_folder_name:
            raise ValueError("output folder name is required")
        if item_type not in {"model", "group"}:
            raise ValueError("item_type must be model or group")
        return self._start("test", printer_id, (item_type, item_name))

    def _start(
        self,
        kind: str,
        printer_id: str | None,
        selection: tuple[str, str] | None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a slice is already running")
            settings = self.store.load_settings()
            folder = safe_segment(settings.output_folder_name)
            printers = [printer for printer in self.printers() if self._included(printer, printer_id)]
            if not printers:
                raise ValueError("no enabled printer is available")
            models = list_models(model_root(self.store))
            included = self.store.load_locations().get("included")
            if included is not None and selection is None:
                chosen = {str(item) for item in included}
                models = [model for model in models if str(model["path"]) in chosen]
            groups = [group for group in self.store.load_groups() if group.get("files")]
            if selection is not None:
                item_type, item_name = selection
                if item_type == "model":
                    models = [model for model in models if model["path"] == item_name]
                    groups = []
                    if not models:
                        raise ValueError("selected model was not found")
                else:
                    groups = [group for group in groups if group.get("id") == item_name or group.get("name") == item_name]
                    models = []
                    if not groups:
                        raise ValueError("selected group was not found")
            if not models and not groups:
                raise ValueError("there are no models to slice")
            job_id = self.store.create_job(kind, folder)
            names = _printer_directories(printers)
            group_labels = _group_labels(groups)
            test = kind == "test"
            work: list[tuple[int, PrinterProfile, str, str, str]] = []
            for printer in printers:
                printer_dir = names[printer.id]
                for model in models:
                    relative = str(model["path"])
                    output = output_model_path(printer_dir, folder, relative, test)
                    item_id = self.store.add_item(
                        job_id, printer.id, printer.name, "model", relative, output.as_posix()
                    )
                    work.append((item_id, printer, "model", relative, output.as_posix()))
                for group in groups:
                    label = group_labels[str(group["id"])]
                    output = output_group_path(printer_dir, folder, label, test)
                    item_id = self.store.add_item(
                        job_id, printer.id, printer.name, "group", label, output.as_posix()
                    )
                    work.append((item_id, printer, "group", str(group["id"]), output.as_posix()))
            self.store.update_job(job_id, "running")
            self._thread = threading.Thread(
                target=self._run, args=(job_id, work), daemon=True
            )
            self._thread.start()
        record = self.store.job_record(job_id)
        assert record is not None
        return record

    def _included(self, printer: PrinterProfile, printer_id: str | None) -> bool:
        if printer_id is not None and printer.id != printer_id:
            return False
        if not printer.slicable:
            return False
        enabled = self.store.load_enabled()
        return enabled.get(printer.id, True)

    def _run(self, job_id: str, work: list[tuple[int, PrinterProfile, str, str, str]]) -> None:
        settings = self.store.load_settings()
        owned = self.store.load_owned()
        shared_filament = _selected_filament(self.store.load_filaments())
        poses = self.store.load_poses()
        groups = {str(group["id"]): group for group in self.store.load_groups()}
        failures = 0
        for item_id, printer, item_type, item_key, output_rel in work:
            self.store.update_item(item_id, status="running", progress="starting")
            prepared = None
            try:
                record = owned_for_printer(printer, owned)
                machine = machine_for_engine(record, printer.engine) if record else None
                if machine is not None:
                    printer = with_synced_bed(printer, machine)
                    machine = apply_filament(machine, shared_filament)
                if item_type == "model":
                    layout = layout_for_model(
                        model_root(self.store),
                        item_key,
                        printer,
                        settings,
                        poses.get(item_key),
                    )
                else:
                    group = groups[item_key]
                    layout = layout_for_group(
                        model_root(self.store),
                        list(group["files"]),
                        printer,
                        settings,
                        poses,
                        group.get("manual_layout"),
                    )
                prepared = prepare_slice(model_root(self.store), printer, layout, settings)
                output = gcode_root(self.store) / output_rel

                def progress(line: str, current: int = item_id) -> None:
                    self.store.update_item(current, progress=line)

                seconds, filament = run_prepared_slice(
                    self.store.paths,
                    printer,
                    settings,
                    prepared,
                    output,
                    progress,
                    machine if isinstance(machine, dict) else None,
                )
                self.store.update_item(
                    item_id,
                    status="done",
                    progress="done",
                    time_seconds=seconds,
                    filament_meters=filament,
                    error=None,
                )
            except (EngineError, OSError, ValueError, KeyError) as exc:
                failures += 1
                self.store.update_item(item_id, status="failed", error=str(exc), progress=None)
            finally:
                if prepared is not None:
                    prepared.temp_dir.cleanup()
        status = "completed" if failures == 0 else "completed_with_errors"
        self.store.update_job(job_id, status, finished=True)


def _selected_filament(library: dict[str, Any]) -> dict[str, Any] | None:
    selected = library.get("selected_id")
    filaments = library.get("filaments")
    if not selected or not isinstance(filaments, list):
        return None
    return next((item for item in filaments if isinstance(item, dict) and item.get("id") == selected), None)


def _group_labels(groups: list[dict[str, Any]]) -> dict[str, str]:
    used: set[str] = set()
    labels: dict[str, str] = {}
    for group in groups:
        base = safe_segment(str(group["name"]))
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        labels[str(group["id"])] = candidate
    return labels


def _printer_directories(printers: list[PrinterProfile]) -> dict[str, str]:
    used: set[str] = set()
    names: dict[str, str] = {}
    for printer in printers:
        base = safe_segment(printer.name)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        names[printer.id] = candidate
    return names
