from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from auto_slicer.bundles import export_all, export_printer, import_bundle
from auto_slicer.cura_config import active_config_dir
from auto_slicer.engine import locate_cura
from auto_slicer.gcode import gcode_info, layer_polylines
from auto_slicer.jobs import JobRunner
from auto_slicer.paths import DataPaths, safe_relative, safe_segment
from auto_slicer.placement import Layout
from auto_slicer.settings_schema import (
    ADHESION_TYPES,
    COMBING_MODES,
    INFILL_PATTERNS,
    SEAM_POSITIONS,
    SEAM_TYPES,
    SUPPORT_STRUCTURES,
    SUPPORT_TYPES,
    SliceSettings,
)
from auto_slicer.slicing import (
    freeze_layout,
    layout_for_group,
    layout_for_model,
    list_models,
    model_stl_bytes,
)
from auto_slicer.store import Store


def create_app(paths: DataPaths | None = None) -> FastAPI:
    resolved = paths or DataPaths.from_env()
    resolved.ensure()
    store = Store(resolved)
    runner = JobRunner(store)
    app = FastAPI(title="Auto Slicer")
    app.state.store = store
    app.state.runner = runner

    @app.middleware("http")
    async def revalidate_frontend(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".css", ".js")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health() -> dict[str, object]:
        engine, resources, _libs = locate_cura(store.paths)
        config_dir = active_config_dir(store.paths.cura_config_dir)
        return {
            "ok": True,
            "engine": str(engine) if engine else None,
            "resources": str(resources) if resources else None,
            "config_dir": str(config_dir),
            "printer_count": len(runner.printers()),
        }

    @app.get("/api/options")
    def options() -> dict[str, object]:
        return {
            "adhesion_types": ADHESION_TYPES,
            "seam_types": SEAM_TYPES,
            "seam_positions": SEAM_POSITIONS,
            "combing_modes": COMBING_MODES,
            "support_types": SUPPORT_TYPES,
            "support_structures": SUPPORT_STRUCTURES,
            "infill_patterns": INFILL_PATTERNS,
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, object]:
        return store.load_settings().to_dict()

    @app.put("/api/settings")
    def put_settings(payload: dict[str, object]) -> dict[str, object]:
        try:
            settings = SliceSettings.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.save_settings(settings)
        return settings.to_dict()

    @app.get("/api/printers")
    def get_printers() -> list[dict[str, object]]:
        enabled = store.load_enabled()
        return [
            printer.to_public_dict(enabled.get(printer.id, True)) for printer in runner.printers()
        ]

    @app.put("/api/printers/enabled")
    def put_enabled(payload: dict[str, object]) -> dict[str, bool]:
        printer_id = str(payload.get("id", ""))
        if not printer_id:
            raise HTTPException(status_code=400, detail="id is required")
        enabled = bool(payload.get("enabled", True))
        store.set_enabled(printer_id, enabled)
        return {"id": printer_id, "enabled": enabled}

    @app.get("/api/bundles")
    def download_bundle(printer_id: str | None = None) -> Response:
        printers = runner.printers()
        if printer_id is None:
            payload = export_all(printers, store.paths.cura_config_dir)
            filename = "printers.zip"
        else:
            printer = _find_printer(runner, printer_id)
            payload = export_printer(printer, store.paths.cura_config_dir)
            filename = f"{safe_segment(printer.name)}.zip"
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/bundles")
    async def upload_bundle(file: UploadFile) -> dict[str, list[str]]:
        payload = await file.read()
        try:
            imported = import_bundle(payload, store.paths.cura_config_dir)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"imported": imported}

    @app.get("/api/models")
    def get_models() -> list[dict[str, object]]:
        return list_models(store.paths.input_dir)

    @app.get("/api/models/mesh")
    def get_mesh(path: str) -> Response:
        try:
            payload = model_stl_bytes(store.paths.input_dir, path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="model not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=payload, media_type="model/stl")

    @app.get("/api/poses")
    def get_poses() -> dict[str, object]:
        return store.load_poses()

    @app.put("/api/poses")
    def put_pose(payload: dict[str, object]) -> dict[str, object]:
        relative = str(payload.get("path", ""))
        try:
            safe_relative(relative)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        existing = dict(store.load_poses().get(relative, {}))
        if "rotation" in payload:
            rotation = payload.get("rotation")
            if rotation is None:
                existing.pop("rotation", None)
            elif isinstance(rotation, list) and len(rotation) == 3:
                existing["rotation"] = [float(rotation[0]), float(rotation[1]), float(rotation[2])]
            else:
                raise HTTPException(status_code=400, detail="rotation needs three numbers")
        if "position" in payload:
            position = payload.get("position")
            if position is None:
                existing.pop("position", None)
            elif isinstance(position, list) and len(position) == 2:
                existing["position"] = [float(position[0]), float(position[1])]
            else:
                raise HTTPException(status_code=400, detail="position needs two numbers")
        store.save_pose(relative, existing or None)
        return existing

    @app.get("/api/groups")
    def get_groups() -> dict[str, object]:
        return {"groups": store.load_groups()}

    @app.put("/api/groups")
    def put_groups(payload: dict[str, object]) -> dict[str, object]:
        raw = payload.get("groups", [])
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="groups must be a list")
        try:
            groups = store.save_groups([item for item in raw if isinstance(item, dict)])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"groups": groups}

    @app.put("/api/groups/layout")
    def put_group_layout(payload: dict[str, object]) -> dict[str, object]:
        group_id = str(payload.get("id", ""))
        printer_id = str(payload.get("printer_id", ""))
        file_name = str(payload.get("file", ""))
        groups = store.load_groups()
        group = next((item for item in groups if item["id"] == group_id), None)
        printer = _find_printer(runner, printer_id) if printer_id else None
        if group is None or printer is None:
            raise HTTPException(status_code=404, detail="group or printer was not found")
        if file_name not in group["files"]:
            raise HTTPException(status_code=400, detail="file is not in the group")
        settings = store.load_settings()
        poses = store.load_poses()
        if group.get("manual_layout"):
            positions = dict(group["manual_layout"])
            positions[file_name] = {"x": float(payload["x"]), "y": float(payload["y"])}
        else:
            current = layout_for_group(
                store.paths.input_dir,
                list(group["files"]),
                printer,
                settings,
                poses,
                None,
            )
            positions = freeze_layout(current, file_name, float(payload["x"]), float(payload["y"]))
        group["manual_layout"] = positions
        store.save_groups(groups)
        layout = layout_for_group(
            store.paths.input_dir,
            list(group["files"]),
            printer,
            settings,
            poses,
            positions,
        )
        return _layout_dict(layout)

    @app.delete("/api/groups/layout")
    def delete_group_layout(id: str) -> dict[str, object]:
        groups = store.load_groups()
        group = next((item for item in groups if item["id"] == id), None)
        if group is None:
            raise HTTPException(status_code=404, detail="group was not found")
        group["manual_layout"] = None
        store.save_groups(groups)
        return {"groups": groups}

    @app.post("/api/layout")
    def post_layout(payload: dict[str, object]) -> dict[str, object]:
        printer = _find_printer(runner, str(payload.get("printer_id", "")))
        settings = store.load_settings()
        poses = store.load_poses()
        kind = str(payload.get("kind", "model"))
        try:
            if kind == "model":
                layout = layout_for_model(
                    store.paths.input_dir,
                    str(payload.get("model", "")),
                    printer,
                    settings,
                    poses.get(str(payload.get("model", ""))),
                )
            elif kind == "group":
                group = next(
                    (item for item in store.load_groups() if item["id"] == str(payload.get("group_id", ""))),
                    None,
                )
                if group is None:
                    raise HTTPException(status_code=404, detail="group was not found")
                layout = layout_for_group(
                    store.paths.input_dir,
                    list(group["files"]),
                    printer,
                    settings,
                    poses,
                    group.get("manual_layout"),
                )
            else:
                raise HTTPException(status_code=400, detail="kind must be model or group")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="model not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _layout_dict(layout)

    @app.post("/api/jobs")
    def start_job() -> dict[str, object]:
        try:
            return runner.start_batch()
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/test")
    def start_test(payload: dict[str, object]) -> dict[str, object]:
        try:
            return runner.start_test(
                str(payload.get("printer_id", "")),
                str(payload.get("item_type", "")),
                str(payload.get("item_name", "")),
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/current")
    def current_job() -> dict[str, object]:
        job = store.latest_job()
        return job or {}

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return store.list_jobs()

    @app.get("/api/gcode/info")
    def gcode_information(path: str) -> dict[str, object]:
        file_path = _output_file(store, path)
        info = gcode_info(file_path)
        return {
            "path": path,
            "time_seconds": info.time_seconds,
            "filament_meters": info.filament_meters,
            "layer_count": info.layer_count,
        }

    @app.get("/api/gcode/layer")
    def gcode_layer(path: str, index: int = 0, include_travel: bool = False) -> dict[str, object]:
        file_path = _output_file(store, path)
        try:
            polylines = layer_polylines(file_path, index, include_travel)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="layer not found") from exc
        return {
            "index": index,
            "polylines": [{"type": item.type_name, "points": item.points} for item in polylines],
        }

    @app.get("/api/output")
    def download_output(path: str) -> FileResponse:
        file_path = _output_file(store, path)
        return FileResponse(file_path, filename=file_path.name, media_type="text/plain")

    if resolved.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=resolved.frontend_dir, html=True), name="frontend")
    return app


def _find_printer(runner: JobRunner, printer_id: str):
    for printer in runner.printers():
        if printer.id == printer_id:
            return printer
    raise HTTPException(status_code=404, detail="printer was not found")


def _output_file(store: Store, relative: str) -> Path:
    try:
        path = store.paths.output_dir / safe_relative(relative)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return path


def _layout_dict(layout: Layout) -> dict[str, object]:
    return {
        "bed_width": layout.bed_width,
        "bed_depth": layout.bed_depth,
        "bed_height": layout.bed_height,
        "error": layout.error,
        "items": [
            {
                "file": item.file,
                "min_x": item.min_x,
                "min_y": item.min_y,
                "max_x": item.max_x,
                "max_y": item.max_y,
                "size_z": item.size_z,
                "rotation": list(item.rotation),
            }
            for item in layout.items
        ],
    }


app = create_app()
