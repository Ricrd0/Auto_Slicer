from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from auto_slicer.browse import gcode_root, model_root
from auto_slicer.cura_config import PrinterProfile, discover_printers
from auto_slicer.engine import EngineError, SliceCancelled, locate_cura, terminate_slice_process
from auto_slicer.master_profiles import adopt_master_printers
from auto_slicer.machine_sync import (
    apply_filaments,
    apply_shared_slice_to_machine,
    idex_output_modes,
    machine_for_engine,
    merge_owned,
    normalize_owned,
    owned_for_printer,
    present_owned,
    printers_for_page,
    sync_owned_with_orca_user_profiles,
    transfer_configured,
    with_idex_mode_gcode,
    with_synced_bed,
)
from auto_slicer.transfer import (
    auto_upload_allowed,
    probe_transfer,
    remote_filename_for,
    upload_file,
    with_probe_result,
)
from auto_slicer.orca_config import active_orca_dir, discover_filaments, discover_orca_printers, load_orca_printers
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
        self._group_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._cancel = threading.Event()
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._printer_cache: dict[str, tuple[float, list[PrinterProfile]]] = {}
        self._filament_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._catalog_state: dict[str, str] = {"cura": "idle", "orca": "idle"}
        self._catalog_error: dict[str, str | None] = {"cura": None, "orca": None}
        self._warming: set[str] = set()

    # Orca discovery over a host mount can take well over a minute; keep results
    # long enough that boot (health + printers + owned) does not scan three times.
    _PRINTER_CACHE_SECONDS = 300.0

    def printers(self, *, catalog: bool = False, refresh: bool = False) -> list[PrinterProfile]:
        engine_name = self.store.load_settings().slicer_engine
        if refresh:
            self.clear_printer_cache()
        owned = self.owned_snapshot()
        if catalog:
            # Full catalog is opt-in ("Show every Orca printer") and may still be warming.
            found = self.cached_discovered(engine_name)
            if found is None:
                self.warm_catalog(engine_name)
                found = []
            elif engine_name == "orca":
                cura = self.cached_discovered("cura") or []
                owned = self.ensure_owned(cura_printers=cura, orca_printers=found)
            return printers_for_page(found, self._presented(owned), engine=engine_name, catalog=True)

        if refresh and engine_name != "orca":
            found = self._discovered("cura")
            orca = self.cached_discovered("orca") or []
            owned = self.ensure_owned(cura_printers=found, orca_printers=orca)
            return printers_for_page(found, self._presented(owned), engine=engine_name, catalog=False)

        # Default Printers page: owned machines only. Orca links resolve from disk
        # by id — never kick off a full catalog scan here.
        found: list[PrinterProfile] = []
        if engine_name == "orca":
            # Heal Marlin→Klipper from user overrides without scanning system machines.
            owned = sync_owned_with_orca_user_profiles(owned, active_orca_dir())
        else:
            found = self.cached_discovered("cura") or []
            if not found:
                self.warm_catalog("cura")
        return printers_for_page(found, self._presented(owned), engine=engine_name, catalog=False)

    def _presented(self, owned: list[dict[str, Any]]) -> list[dict[str, Any]]:
        presented: list[dict[str, Any]] = []
        for item in owned:
            try:
                presented.append(present_owned(item))
            except (TypeError, ValueError):
                presented.append(item)
        return presented

    def owned_snapshot(self) -> list[dict[str, Any]]:
        """Normalize owned printers without discovering engine catalogs."""
        existing = adopt_master_printers(
            [item for item in self.store.load_owned() if isinstance(item, dict)]
        )
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
            merged = [normalize_owned(item) for item in adopt_master_printers(merged)]
        except (TypeError, ValueError):
            # Keep the UI usable if one owned record has a stale/unknown field.
            return existing
        if merged != existing:
            self.store.save_owned(merged)
        return merged

    def clear_printer_cache(self) -> None:
        with self._printer_lock:
            self._printer_cache.clear()
            self._warming.clear()
            self._catalog_state = {"cura": "idle", "orca": "idle"}
            self._catalog_error = {"cura": None, "orca": None}

    def cached_discovered(
        self, engine_name: str, *, wait: bool = True
    ) -> list[PrinterProfile] | None:
        """Return a fresh catalog cache entry.

        ``wait=False`` is for page loads. A catalog warm holds this lock only
        around its bookkeeping, but a stuck holder must not freeze the printers
        the user already owns.
        """
        if wait:
            self._printer_lock.acquire()
        elif not self._printer_lock.acquire(blocking=False):
            return None
        try:
            cached = self._printer_cache.get(engine_name)
            if cached is None:
                return None
            if time.monotonic() - cached[0] >= self._PRINTER_CACHE_SECONDS:
                return None
            return cached[1]
        finally:
            self._printer_lock.release()

    def catalog_status(self, engine_name: str | None = None) -> dict[str, Any]:
        engine = engine_name or self.store.load_settings().slicer_engine
        with self._printer_lock:
            state = self._catalog_state.get(engine, "idle")
            error = self._catalog_error.get(engine)
            cached = self._printer_cache.get(engine)
            cache_ok = (
                cached is not None
                and time.monotonic() - cached[0] < self._PRINTER_CACHE_SECONDS
            )
            if state == "ready" and not cache_ok:
                state = "idle"
            ready = cache_ok and state == "ready"
            count = len(cached[1]) if cached is not None and ready else 0
        return {
            "engine": engine,
            "state": state,
            "ready": ready,
            "loading": state == "loading",
            "error": error,
            "count": count,
        }

    def warm_catalog(self, engine_name: str | None = None) -> dict[str, Any]:
        """Start a background full catalog scan for Cura or Orca (opt-in for Orca)."""
        engine = engine_name or self.store.load_settings().slicer_engine

        def _run() -> None:
            try:
                found = self._discovered(engine)
                if engine == "orca":
                    cura = self.cached_discovered("cura") or []
                    self.ensure_owned(cura_printers=cura, orca_printers=found)
                elif engine == "cura":
                    orca = self.cached_discovered("orca") or []
                    self.ensure_owned(cura_printers=found, orca_printers=orca)
                with self._printer_lock:
                    self._catalog_state[engine] = "ready"
            except Exception as exc:
                with self._printer_lock:
                    self._catalog_state[engine] = "error"
                    self._catalog_error[engine] = str(exc)
            finally:
                with self._printer_lock:
                    self._warming.discard(engine)

        start = False
        with self._printer_lock:
            cached = self._printer_cache.get(engine)
            ready = (
                cached is not None
                and time.monotonic() - cached[0] < self._PRINTER_CACHE_SECONDS
                and self._catalog_state.get(engine) == "ready"
            )
            # Status is read after this lock is released. Calling catalog_status
            # here deadlocks: it takes the same non-reentrant lock, and every
            # later printers/jobs request waits forever.
            if not ready and engine not in self._warming:
                self._warming.add(engine)
                self._catalog_state[engine] = "loading"
                self._catalog_error[engine] = None
                start = True
        if start:
            threading.Thread(target=_run, name=f"warm-{engine}-catalog", daemon=True).start()
        return self.catalog_status(engine)

    def warm_printer_cache(self) -> None:
        """Deprecated no-op: full Orca catalog warms only via Show every Orca printer."""
        return

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
            if engine_name not in self._warming:
                self._catalog_state[engine_name] = "ready"
            return found

    def resolve_owned_orca(self, owned: list[dict[str, Any]] | None = None) -> list[PrinterProfile]:
        """Load only the Orca profiles linked from owned printers."""
        records = owned if owned is not None else self.owned_snapshot()
        ids = {str(item["orca_id"]) for item in records if item.get("orca_id")}
        if not ids:
            return []
        return load_orca_printers(active_orca_dir(), only_ids=ids)

    def filament_catalog(self) -> list[dict[str, Any]]:
        with self._printer_lock:
            now = time.monotonic()
            if self._filament_cache is not None and now - self._filament_cache[0] < 30:
                return self._filament_cache[1]
        # Scan outside the lock. Holding it here froze the printers page for
        # the whole Orca filament walk (and forever, if that walk stalled).
        found = discover_filaments(active_orca_dir())
        with self._printer_lock:
            self._filament_cache = (time.monotonic(), found)
            return found

    def start_batch(self) -> dict[str, Any]:
        return self._start("batch", None, None)

    def _owned_for_slice(self) -> list[dict[str, Any]]:
        """Owned printers for a job, without a full engine catalog scan."""
        owned = self.owned_snapshot()
        healed = sync_owned_with_orca_user_profiles(owned, active_orca_dir())
        if healed != owned:
            self.store.save_owned(healed)
            return healed
        return owned

    def start_test(self, printer_id: str, item_type: str, item_name: str) -> dict[str, Any]:
        if item_type not in {"model", "group"}:
            raise ValueError("item_type must be model or group")
        return self._start("test", printer_id, (item_type, item_name))

    def stop(self) -> dict[str, Any]:
        """Stop the current batch/test job and kill active slicer processes."""
        self._cancel.set()
        with self._process_lock:
            processes = list(self._active_processes)
        for process in processes:
            terminate_slice_process(process)
        job = self.store.latest_job()
        if job is not None and job.get("status") in {"queued", "running"}:
            self.store.cancel_open_items(str(job["id"]))
            self.store.update_job(str(job["id"]), "stopped", finished=True)
            refreshed = self.store.job_record(str(job["id"]))
            return refreshed or job
        return job or {}

    def upload_job_item(self, item_id: int, *, force_probe: bool = True) -> dict[str, Any]:
        """Upload a finished job item's gcode using the owned printer transfer config."""
        item = self.store.get_item(item_id)
        if item is None:
            raise ValueError("job item not found")
        if item.get("status") != "done":
            raise ValueError("only finished items can be uploaded")
        output_rel = str(item.get("output_path") or "")
        if not output_rel:
            raise ValueError("job item has no output path")
        local = gcode_root(self.store) / output_rel
        if not local.is_file():
            raise ValueError(f"gcode file not found: {output_rel}")

        owned = self.owned_snapshot()
        record = next(
            (
                entry
                for entry in owned
                if isinstance(entry, dict)
                and (
                    entry.get("id") == item.get("printer_id")
                    or entry.get("cura_id") == item.get("printer_id")
                    or entry.get("orca_id") == item.get("printer_id")
                )
            ),
            None,
        )
        if record is None:
            # Fall back to matching by display name when printer_id is an engine id.
            record = next(
                (
                    entry
                    for entry in owned
                    if isinstance(entry, dict)
                    and str(entry.get("name") or "") == str(item.get("printer_name") or "")
                ),
                None,
            )
        transfer = (
            record.get("transfer")
            if isinstance(record, dict) and isinstance(record.get("transfer"), dict)
            else None
        )
        if not transfer_configured(transfer):
            raise ValueError("this printer has no transfer protocol configured")

        if force_probe:
            probe = probe_transfer(transfer)
            transfer = with_probe_result(transfer, probe)
            if isinstance(record, dict):
                record = {**record, "transfer": transfer}
                updated = [
                    record if entry.get("id") == record.get("id") else entry for entry in owned
                ]
                self.store.save_owned(updated)
            if not probe.ok:
                self.store.update_item(
                    item_id,
                    upload_status="skipped",
                    upload_error=probe.error or "Printer is not reachable",
                )
                refreshed = self.store.get_item(item_id)
                return refreshed or item

        self.store.update_item(item_id, upload_status="uploading", upload_error=None)
        result = upload_file(
            transfer, local, remote_name=remote_filename_for(output_rel)
        )
        if result.ok:
            self.store.update_item(item_id, upload_status="uploaded", upload_error=None)
        else:
            self.store.update_item(
                item_id, upload_status="failed", upload_error=result.error
            )
        refreshed = self.store.get_item(item_id)
        return refreshed or item

    def _maybe_auto_upload(
        self,
        item_id: int,
        record: dict[str, Any] | None,
        local_path: Path,
        output_rel: str,
    ) -> None:
        transfer = (
            record.get("transfer")
            if isinstance(record, dict) and isinstance(record.get("transfer"), dict)
            else None
        )
        if not transfer_configured(transfer):
            return
        if str(transfer.get("mode") or "manual") != "auto":
            return
        # Live probe before auto upload — skip with a clear reason if unreachable.
        probe = probe_transfer(transfer)
        updated_transfer = with_probe_result(transfer, probe)
        if isinstance(record, dict):
            owned = self.owned_snapshot()
            record_id = record.get("id")
            saved = [
                {**entry, "transfer": updated_transfer}
                if entry.get("id") == record_id
                else entry
                for entry in owned
            ]
            self.store.save_owned(saved)
        if not probe.ok or not auto_upload_allowed(updated_transfer):
            reason = probe.error or (
                "Auto upload requires a reachable printer. Test connection first."
            )
            self.store.update_item(
                item_id, upload_status="skipped", upload_error=reason
            )
            return
        self.store.update_item(item_id, upload_status="uploading", upload_error=None)
        result = upload_file(
            updated_transfer,
            local_path,
            remote_name=remote_filename_for(output_rel),
        )
        if result.ok:
            self.store.update_item(item_id, upload_status="uploaded", upload_error=None)
        else:
            self.store.update_item(
                item_id, upload_status="failed", upload_error=result.error
            )

    def _start(
        self,
        kind: str,
        printer_id: str | None,
        selection: tuple[str, str] | None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a slice is already running")
            self._cancel.clear()
            settings = self.store.load_settings()
            folder = settings.output_folder_name.strip()
            if folder:
                folder = safe_segment(folder)
            printers = [printer for printer in self.printers() if self._included(printer, printer_id)]
            if not printers:
                raise ValueError("no enabled printer is available")
            # Slice the printers already on the page. A full Cura/Orca catalog
            # scan belongs to "Show every Orca printer", not to starting a job.
            owned = self._owned_for_slice()
            groups_all = self.store.load_groups()
            arranged = _arranged_paths(groups_all)
            models = [
                model
                for model in list_models(model_root(self.store))
                if str(model["path"]) not in arranged
            ]
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
                if record is not None:
                    record = present_owned(record)
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
        workers = slice_worker_count(len(work))
        failures = 0
        cancelled = 0

        def _track_start(process: subprocess.Popen[str]) -> None:
            with self._process_lock:
                self._active_processes.add(process)

        def _track_finish(process: subprocess.Popen[str]) -> None:
            with self._process_lock:
                self._active_processes.discard(process)

        def _one(task: tuple[int, PrinterProfile, str, str, str, str | None]) -> str:
            item_id, printer, item_type, item_key, output_rel, mode = task
            if self._cancel.is_set():
                self.store.update_item(
                    item_id, status="cancelled", progress="stopped", error=None
                )
                return "cancelled"
            self.store.update_item(item_id, status="running", progress="starting")
            prepared = None
            try:
                record = owned_for_printer(printer, owned)
                if record is not None:
                    record = present_owned(record)
                machine = machine_for_engine(record, printer.engine) if record else None
                active_printer = printer
                if machine is not None:
                    active_printer = with_synced_bed(printer, machine)
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
                        active_printer,
                        settings,
                        poses.get(item_key),
                        machine if isinstance(machine, dict) else None,
                    )
                else:
                    with self._group_lock:
                        group = groups[item_key]
                        layout, dirty = ensure_group_layout(
                            model_root(self.store),
                            group,
                            active_printer,
                            settings,
                            poses,
                            machine if isinstance(machine, dict) else None,
                        )
                        if dirty:
                            self.store.save_groups(list(groups.values()))
                if self._cancel.is_set():
                    self.store.update_item(
                        item_id, status="cancelled", progress="stopped", error=None
                    )
                    return "cancelled"
                prepared = prepare_slice(
                    model_root(self.store), active_printer, layout, settings
                )
                output = gcode_root(self.store) / output_rel

                def progress(line: str, current: int = item_id) -> None:
                    self.store.update_item(current, progress=line)

                seconds, filament = run_prepared_slice(
                    self.store.paths,
                    active_printer,
                    settings,
                    prepared,
                    output,
                    progress,
                    machine if isinstance(machine, dict) else None,
                    should_cancel=self._cancel.is_set,
                    on_process_start=_track_start,
                    on_process_finish=_track_finish,
                )
                if self._cancel.is_set():
                    self.store.update_item(
                        item_id, status="cancelled", progress="stopped", error=None
                    )
                    return "cancelled"
                self.store.update_item(
                    item_id,
                    status="done",
                    progress="done",
                    time_seconds=seconds,
                    filament_meters=filament,
                    error=None,
                )
                self._maybe_auto_upload(item_id, record, output, output_rel)
                return "done"
            except SliceCancelled:
                self.store.update_item(
                    item_id, status="cancelled", progress="stopped", error=None
                )
                return "cancelled"
            except (EngineError, OSError, ValueError, KeyError) as exc:
                self.store.update_item(
                    item_id, status="failed", error=str(exc), progress=None
                )
                return "failed"
            finally:
                if prepared is not None:
                    prepared.temp_dir.cleanup()

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_one, task) for task in work]
                for future in as_completed(futures):
                    result = future.result()
                    if result == "failed":
                        failures += 1
                    elif result == "cancelled":
                        cancelled += 1
                    if self._cancel.is_set():
                        for pending in futures:
                            pending.cancel()
        finally:
            with self._process_lock:
                leftover = list(self._active_processes)
                self._active_processes.clear()
            for process in leftover:
                terminate_slice_process(process)

        if self._cancel.is_set() or cancelled:
            self.store.cancel_open_items(job_id)
            self.store.update_job(job_id, "stopped", finished=True)
            return
        status = "completed" if failures == 0 else "completed_with_errors"
        self.store.update_job(job_id, status, finished=True)


def slice_worker_count(work_items: int) -> int:
    """How many slicer processes to run at once.

    Each Orca/Cura invocation is already multi-threaded, so we overlap a few jobs
    to hide ramp-up/I/O without oversubscribing the machine. Override with
    ``AUTO_SLICER_SLICE_WORKERS``.
    """
    if work_items <= 1:
        return 1
    raw = os.environ.get("AUTO_SLICER_SLICE_WORKERS", "").strip()
    if raw:
        try:
            return max(1, min(work_items, int(raw)))
        except ValueError:
            pass
    cpus = os.cpu_count() or 4
    # ~one concurrent slicer per 4 cores, capped so heavy Orca jobs still breathe.
    return max(2, min(work_items, 4, max(1, cpus // 4)))


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
