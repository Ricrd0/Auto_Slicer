from __future__ import annotations

import threading
import time
from typing import Any

from auto_slicer.browse import gcode_root, model_root
from auto_slicer.cura_config import PrinterProfile, discover_printers
from auto_slicer.engine import EngineError, locate_cura
from auto_slicer.machine_sync import (
    apply_filaments,
    apply_shared_slice_to_machine,
    idex_output_modes,
    machine_for_engine,
    merge_owned,
    normalize_owned,
    owned_for_printer,
    printers_for_page,
    sync_owned_with_orca_user_profiles,
    with_idex_mode_gcode,
    with_synced_bed,
)
from auto_slicer.orca_config import active_orca_dir, discover_filaments, discover_orca_printers
from auto_slicer.paths import safe_segment
from auto_slicer.slicing import (
    ensure_group_layout,
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
        self._warming = False

    # Orca discovery over a host mount can take well over a minute; keep results
    # long enough that boot (health + printers + owned) does not scan three times.
    _PRINTER_CACHE_SECONDS = 300.0

    def printers(self, *, catalog: bool = False, refresh: bool = False) -> list[PrinterProfile]:
        engine_name = self.store.load_settings().slicer_engine
        if refresh:
            self.clear_printer_cache()
        if catalog or refresh:
            cura = self._discovered("cura")
            orca = self._discovered("orca")
            owned = self.ensure_owned(cura_printers=cura, orca_printers=orca)
            found = orca if engine_name == "orca" else cura
            return printers_for_page(found, owned, engine=engine_name, catalog=catalog)

        # Fast path for the Printers page: never block on a cold Orca catalog scan.
        owned = self.owned_snapshot()
        found = self.cached_discovered(engine_name)
        if found is None:
            self.warm_printer_cache()
            found = []
        else:
            cura = self.cached_discovered("cura")
            orca = self.cached_discovered("orca")
            if cura is not None and orca is not None:
                owned = self.ensure_owned(cura_printers=cura, orca_printers=orca)
        return printers_for_page(found, owned, engine=engine_name, catalog=False)

    def owned_snapshot(self) -> list[dict[str, Any]]:
        """Normalize owned printers without discovering engine catalogs."""
        existing = self.store.load_owned()
        snapshot: list[dict[str, Any]] = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            try:
                snapshot.append(normalize_owned(item))
            except (TypeError, ValueError):
                snapshot.append(item)
        return snapshot

    def ensure_owned(
        self,
        *,
        cura_printers: list[PrinterProfile] | None = None,
        orca_printers: list[PrinterProfile] | None = None,
    ) -> list[dict[str, Any]]:
        existing = self.store.load_owned()
        try:
            cura = cura_printers if cura_printers is not None else self._discovered("cura")
            orca = orca_printers if orca_printers is not None else self._discovered("orca")
            merged = merge_owned(existing, cura, orca)
            merged = sync_owned_with_orca_user_profiles(merged, active_orca_dir())
        except (TypeError, ValueError):
            # Keep the UI usable if one owned record has a stale/unknown field.
            return existing
        if merged != existing:
            self.store.save_owned(merged)
        return merged

    def clear_printer_cache(self) -> None:
        with self._printer_lock:
            self._printer_cache.clear()
            self._warming = False

    def cached_discovered(self, engine_name: str) -> list[PrinterProfile] | None:
        with self._printer_lock:
            cached = self._printer_cache.get(engine_name)
            if cached is None:
                return None
            if time.monotonic() - cached[0] >= self._PRINTER_CACHE_SECONDS:
                return None
            return cached[1]

    def warm_printer_cache(self) -> None:
        """Discover Cura/Orca catalogs in a daemon thread for later requests."""
        with self._printer_lock:
            if self._warming:
                return
            now = time.monotonic()
            cura = self._printer_cache.get("cura")
            orca = self._printer_cache.get("orca")
            cura_ok = cura is not None and now - cura[0] < self._PRINTER_CACHE_SECONDS
            orca_ok = orca is not None and now - orca[0] < self._PRINTER_CACHE_SECONDS
            if cura_ok and orca_ok:
                return
            self._warming = True

        def _run() -> None:
            try:
                self.ensure_owned()
            except Exception:
                pass
            finally:
                with self._printer_lock:
                    self._warming = False

        threading.Thread(target=_run, name="warm-printer-cache", daemon=True).start()

    def _discovered(self, engine_name: str) -> list[PrinterProfile]:
        with self._printer_lock:
            now = time.monotonic()
            cached = self._printer_cache.get(engine_name)
            if cached is not None and now - cached[0] < self._PRINTER_CACHE_SECONDS:
                return cached[1]
        # Discover outside the lock so the Printers page can still read owned
        # printers while a multi-minute Orca catalog scan runs in the background.
        if engine_name == "orca":
            found = discover_orca_printers(active_orca_dir())
        else:
            _engine, resources, _libs = locate_cura(self.store.paths)
            found = discover_printers(self.store.paths.cura_config_dir, resources)
        with self._printer_lock:
            self._printer_cache[engine_name] = (time.monotonic(), found)
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
            groups_all = self.store.load_groups()
            arranged = _arranged_paths(groups_all)
            models = [
                model
                for model in list_models(model_root(self.store))
                if str(model["path"]) not in arranged
            ]
            owned = self.ensure_owned()
            included = self.store.load_locations().get("included")
            if selection is None:
                chosen = {str(item) for item in included} if isinstance(included, list) else set()
                models = [model for model in models if str(model["path"]) in chosen]
            groups = [group for group in groups_all if group.get("files")]
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
                raise ValueError("there are no models or groups to slice")
            job_id = self.store.create_job(kind, folder)
            names = _printer_directories(printers)
            group_labels = _group_labels(groups)
            test = kind == "test"
            work: list[tuple[int, PrinterProfile, str, str, str, str | None]] = []
            for printer in printers:
                printer_dir = names[printer.id]
                record = owned_for_printer(printer, owned)
                machine = machine_for_engine(record, printer.engine) if record else None
                modes = idex_output_modes(machine)
                for mode in modes:
                    for model in models:
                        relative = str(model["path"])
                        output = output_model_path(
                            printer_dir, folder, relative, test, mode_suffix=mode
                        )
                        item_id = self.store.add_item(
                            job_id,
                            printer.id,
                            printer.name,
                            "model",
                            relative,
                            output.as_posix(),
                        )
                        work.append(
                            (item_id, printer, "model", relative, output.as_posix(), mode)
                        )
                    for group in groups:
                        label = group_labels[str(group["id"])]
                        output = output_group_path(
                            printer_dir, folder, label, test, mode_suffix=mode
                        )
                        item_id = self.store.add_item(
                            job_id,
                            printer.id,
                            printer.name,
                            "group",
                            label,
                            output.as_posix(),
                        )
                        work.append(
                            (
                                item_id,
                                printer,
                                "group",
                                str(group["id"]),
                                output.as_posix(),
                                mode,
                            )
                        )
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

    def _run(
        self, job_id: str, work: list[tuple[int, PrinterProfile, str, str, str, str | None]]
    ) -> None:
        settings = self.store.load_settings()
        owned = self.store.load_owned()
        filament_library = self.store.load_filaments()
        shared_filament = _selected_filament(filament_library)
        poses = self.store.load_poses()
        groups = {str(group["id"]): group for group in self.store.load_groups()}
        failures = 0
        for item_id, printer, item_type, item_key, output_rel, mode in work:
            self.store.update_item(item_id, status="running", progress="starting")
            prepared = None
            try:
                record = owned_for_printer(printer, owned)
                machine = machine_for_engine(record, printer.engine) if record else None
                if machine is not None:
                    printer = with_synced_bed(printer, machine)
                    filament_t1 = _filament_by_id(
                        filament_library, machine.get("filament_id_t1")
                    )
                    machine = apply_filaments(machine, shared_filament, filament_t1)
                    machine = apply_shared_slice_to_machine(machine, settings)
                    machine = with_idex_mode_gcode(machine, mode)
                if item_type == "model":
                    layout = layout_for_model(
                        model_root(self.store),
                        item_key,
                        printer,
                        settings,
                        poses.get(item_key),
                        machine if isinstance(machine, dict) else None,
                    )
                else:
                    group = groups[item_key]
                    layout, dirty = ensure_group_layout(
                        model_root(self.store),
                        group,
                        printer,
                        settings,
                        poses,
                        machine if isinstance(machine, dict) else None,
                    )
                    if dirty:
                        self.store.save_groups(list(groups.values()))
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
    return _filament_by_id(library, library.get("selected_id"))


def _filament_by_id(library: dict[str, Any], filament_id: object) -> dict[str, Any] | None:
    if not filament_id:
        return None
    filaments = library.get("filaments")
    if not isinstance(filaments, list):
        return None
    return next(
        (
            item
            for item in filaments
            if isinstance(item, dict) and item.get("id") == filament_id
        ),
        None,
    )


def _arranged_paths(groups: list[dict[str, Any]]) -> set[str]:
    arranged: set[str] = set()
    for group in groups:
        if group.get("arranged_3mf"):
            arranged.add(str(group["arranged_3mf"]))
        by_offset = group.get("arranged_3mf_by_offset")
        if isinstance(by_offset, dict):
            for value in by_offset.values():
                if value:
                    arranged.add(str(value))
    return arranged


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
