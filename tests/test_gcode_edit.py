import time
from pathlib import Path

from auto_slicer.edit_jobs import EditJobRunner
from auto_slicer.gcode_edit import (
    batch_edit_files,
    process_replace_file,
    process_sanitize_file,
    resolve_replacement,
)
from auto_slicer.paths import DataPaths
from auto_slicer.store import Store


SIDEWINDER_PRIME = """G90
M83
G92 E0
G1 X10 Y.5 Z0.25 F5000.0
G1 X100 Y.5 Z0.25 F1500.0 E15 ; Draw the first line
G1 X100 Y.2 Z0.25 F5000.0
G1 X10 Y.2 Z0.25 F1500.0 E30 ; Draw the second line
G92 E0
"""


def test_resolve_replacement_reads_gcode_file(tmp_path: Path) -> None:
    path = tmp_path / "block.gcode"
    path.write_text("M400\nG91\n", encoding="utf-8")
    assert resolve_replacement(str(path)) == "M400\nG91\n"
    assert resolve_replacement("literal text") == "literal text"


def test_process_replace_file_counts_and_backups(tmp_path: Path) -> None:
    path = tmp_path / "part.gcode"
    path.write_text("AAA BBB AAA\n", encoding="utf-8")
    result = process_replace_file(path, "AAA", "XX", dry_run=False, backup=True)
    assert result.modified is True
    assert result.replacements == 2
    assert path.read_text(encoding="utf-8") == "XX BBB XX\n"
    assert path.with_suffix(".gcode.bak").read_text(encoding="utf-8") == "AAA BBB AAA\n"


def test_process_sanitize_rewrites_absolute_prime(tmp_path: Path) -> None:
    path = tmp_path / "side.gcode"
    path.write_text(SIDEWINDER_PRIME, encoding="utf-8")
    result = process_sanitize_file(path, dry_run=False, backup=False, assume_relative_e=False)
    assert result.modified is True
    text = path.read_text(encoding="utf-8")
    assert "E30" not in text
    assert "E15 ; Draw the second line" in text


def test_batch_edit_replace_with_progress(tmp_path: Path) -> None:
    root = tmp_path / "out"
    nested = root / "printer"
    nested.mkdir(parents=True)
    (nested / "a.gcode").write_text("OLD\n", encoding="utf-8")
    (nested / "b.gcode").write_text("keep\n", encoding="utf-8")
    (nested / "c.txt").write_text("OLD\n", encoding="utf-8")
    seen: list[int] = []

    def on_progress(processed: int, total: int, path: Path | None) -> None:
        seen.append(processed)
        assert total == 2

    result = batch_edit_files(
        sorted(root.rglob("*.gcode")),
        mode="replace",
        find_str="OLD",
        replace_str="NEW",
        dry_run=False,
        backup=False,
        workers=2,
        on_progress=on_progress,
    )
    assert result.processed == 2
    assert result.modified == 1
    assert result.replacements == 1
    assert set(seen) == {1, 2}
    assert (nested / "a.gcode").read_text(encoding="utf-8") == "NEW\n"


def test_edit_job_runner_dry_run(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    target = output / "part.gcode"
    target.write_text("FINDME\n", encoding="utf-8")
    paths = DataPaths(
        input_dir=tmp_path / "input",
        output_dir=output,
        app_dir=tmp_path / "app",
        cura_config_dir=tmp_path / "cura",
        frontend_dir=tmp_path / "frontend",
        cura_root=None,
    )
    paths.ensure()
    store = Store(paths)
    runner = EditJobRunner(store)
    job = runner.start(
        {
            "mode": "replace",
            "directory": str(output),
            "find": "FINDME",
            "replace": "DONE",
            "dry_run": True,
            "backup": False,
            "workers": 1,
        }
    )
    assert job["status"] in {"queued", "running", "done"}
    for _ in range(50):
        current = runner.current()
        if current.get("status") in {"done", "failed", "stopped"}:
            break
        time.sleep(0.05)
    current = runner.current()
    assert current["status"] == "done"
    assert current["modified"] == 1
    assert target.read_text(encoding="utf-8") == "FINDME\n"
