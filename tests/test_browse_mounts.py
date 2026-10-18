from pathlib import Path

from auto_slicer.browse import browse, mounts_from_env, to_display, to_local


MOUNTINFO = "\n".join(
    [
        "467 454 0:69 /Github/Personal/Auto_Slicer/data/input {input_dir} rw - 9p D:\\134 rw,aname=drvfs;path=D:\\;uid=0",
        "468 454 0:69 /Github/Personal/Auto_Slicer/data/output {output_dir} rw - 9p D:\\134 rw,aname=drvfs;path=D:\\;uid=0",
        "472 454 0:68 / /host/c rw - 9p C:\\134 rw,aname=drvfs;path=C:\\;uid=0",
    ]
)


def test_host_drives_follow_attached_letters(tmp_path: Path, monkeypatch) -> None:
    drives = tmp_path / "drives"
    (drives / "c").mkdir(parents=True)
    (drives / "d" / "models").mkdir(parents=True)
    (drives / "wsl").mkdir()
    (drives / "notes.txt").write_text("skip", encoding="utf-8")
    monkeypatch.setenv("AUTO_SLICER_HOST_DRIVES", str(drives))
    monkeypatch.delenv("AUTO_SLICER_HOST_MOUNTS", raising=False)
    monkeypatch.delenv("AUTO_SLICER_INPUT", raising=False)
    monkeypatch.delenv("AUTO_SLICER_OUTPUT", raising=False)

    listing = browse("", [tmp_path / "input", tmp_path / "output"])
    assert [entry["path"] for entry in listing["entries"]] == ["C:\\", "D:\\"]

    opened = browse("D:\\models", [])
    assert opened["path"] == "D:\\models"
    assert opened["parent"] == "D:\\"


def test_bind_mounts_show_windows_project_paths(tmp_path: Path, monkeypatch) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    monkeypatch.delenv("AUTO_SLICER_HOST_DRIVES", raising=False)
    monkeypatch.delenv("AUTO_SLICER_HOST_MOUNTS", raising=False)
    monkeypatch.setenv("AUTO_SLICER_INPUT", str(input_dir))
    monkeypatch.setenv("AUTO_SLICER_OUTPUT", str(output_dir))
    text = MOUNTINFO.format(input_dir=input_dir.as_posix(), output_dir=output_dir.as_posix())
    monkeypatch.setattr("auto_slicer.browse._mountinfo_text", lambda: text)

    mounts = mounts_from_env()
    assert to_display(input_dir, mounts) == "D:\\Github\\Personal\\Auto_Slicer\\data\\input"
    assert to_local("D:\\Github\\Personal\\Auto_Slicer\\data\\output", mounts) == output_dir


def test_explicit_mount_overrides_discovered_drive(tmp_path: Path, monkeypatch) -> None:
    drives = tmp_path / "drives"
    (drives / "c").mkdir(parents=True)
    custom = tmp_path / "custom-c"
    custom.mkdir()
    monkeypatch.setenv("AUTO_SLICER_HOST_DRIVES", str(drives))
    monkeypatch.setenv("AUTO_SLICER_HOST_MOUNTS", f"C:/={custom.as_posix()}")
    monkeypatch.delenv("AUTO_SLICER_INPUT", raising=False)
    monkeypatch.delenv("AUTO_SLICER_OUTPUT", raising=False)

    mounts = mounts_from_env()
    assert to_local("C:\\", mounts) == custom
