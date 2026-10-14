from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_slicer.machine_sync import (
    apply_save_scope,
    default_transfer,
    normalize_owned,
    normalize_transfer,
    transfer_configured,
)
from auto_slicer.transfer import (
    auto_upload_allowed,
    probe_transfer,
    remote_filename_for,
    upload_file,
    with_probe_result,
)


def test_normalize_transfer_defaults() -> None:
    transfer = normalize_transfer(None)
    assert transfer["protocol"] == "none"
    assert transfer["mode"] == "manual"
    assert transfer["host"] == ""
    assert transfer["port"] is None
    assert transfer_configured(transfer) is False


def test_normalize_transfer_moonraker_defaults_port() -> None:
    transfer = normalize_transfer({"protocol": "moonraker", "host": "192.168.1.10"})
    assert transfer["port"] == 7125
    assert transfer_configured(transfer) is True


def test_normalize_transfer_requires_host() -> None:
    with pytest.raises(ValueError, match="host"):
        normalize_transfer({"protocol": "snapmaker"})


def test_normalize_owned_includes_transfer() -> None:
    owned = normalize_owned({"name": "Test Printer", "settings": {}})
    assert owned["transfer"]["protocol"] == "none"


def test_apply_save_scope_preserves_probe_when_unchanged() -> None:
    existing = normalize_owned(
        {
            "name": "Kossel",
            "settings": {},
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.5",
                "reachable": True,
                "last_probe_ok": True,
            },
        }
    )
    incoming = normalize_owned(
        {
            "id": existing["id"],
            "name": "Kossel",
            "settings": existing["settings"],
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.5",
                "mode": "auto",
            },
        }
    )
    saved = apply_save_scope(existing, incoming, "both")
    assert saved["transfer"]["mode"] == "auto"
    assert saved["transfer"]["reachable"] is True
    assert saved["transfer"]["last_probe_ok"] is True


def test_apply_save_scope_clears_probe_when_host_changes() -> None:
    existing = normalize_owned(
        {
            "name": "Kossel",
            "settings": {},
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.5",
                "reachable": True,
                "last_probe_ok": True,
            },
        }
    )
    incoming = normalize_owned(
        {
            "id": existing["id"],
            "name": "Kossel",
            "settings": existing["settings"],
            "transfer": {"protocol": "moonraker", "host": "10.0.0.9"},
        }
    )
    saved = apply_save_scope(existing, incoming, "both")
    assert saved["transfer"]["reachable"] is False
    assert saved["transfer"]["last_probe_ok"] is False


def test_auto_upload_allowed() -> None:
    transfer = default_transfer()
    assert auto_upload_allowed(transfer) is False
    transfer.update(
        {
            "protocol": "moonraker",
            "host": "1.2.3.4",
            "mode": "auto",
            "reachable": True,
            "last_probe_ok": True,
        }
    )
    assert auto_upload_allowed(normalize_transfer(transfer)) is True


def test_probe_moonraker_ok() -> None:
    transfer = normalize_transfer({"protocol": "moonraker", "host": "printer.local"})
    response = httpx.Response(200, json={"klippy_connected": True})
    with patch("auto_slicer.transfer.httpx.get", return_value=response) as get:
        result = probe_transfer(transfer)
    assert result.ok is True
    get.assert_called_once()
    assert "/server/info" in get.call_args.args[0]


def test_probe_moonraker_unreachable() -> None:
    transfer = normalize_transfer({"protocol": "moonraker", "host": "printer.local"})
    with patch(
        "auto_slicer.transfer.httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        result = probe_transfer(transfer)
    assert result.ok is False
    assert "unreachable" in (result.error or "").lower()


def test_upload_moonraker(tmp_path: Path) -> None:
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; gcode\n", encoding="utf-8")
    transfer = normalize_transfer(
        {"protocol": "moonraker", "host": "printer.local", "remote_path": "batch"}
    )
    response = httpx.Response(201, json={"item": {"path": "batch/part.gcode"}})
    with patch("auto_slicer.transfer.httpx.post", return_value=response) as post:
        result = upload_file(transfer, gcode)
    assert result.ok is True
    assert post.call_args.kwargs["data"]["root"] == "gcodes"
    assert post.call_args.kwargs["data"]["path"] == "batch"
    assert post.call_args.kwargs["data"]["print"] == "false"


def test_upload_snapmaker(tmp_path: Path) -> None:
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; gcode\n", encoding="utf-8")
    transfer = normalize_transfer(
        {"protocol": "snapmaker", "host": "192.168.1.20", "token": "abc"}
    )
    connect = httpx.Response(200, json={"token": "abc"})
    prepare = httpx.Response(200, json={"ok": True})
    with patch(
        "auto_slicer.transfer.httpx.post",
        side_effect=[connect, prepare],
    ) as post:
        result = upload_file(transfer, gcode)
    assert result.ok is True
    assert "prepare_print" in post.call_args_list[1].args[0]
    assert "start_print" not in "".join(call.args[0] for call in post.call_args_list)


def test_with_probe_result_and_remote_name() -> None:
    from auto_slicer.transfer import ProbeResult

    transfer = normalize_transfer({"protocol": "moonraker", "host": "a"})
    updated = with_probe_result(transfer, ProbeResult(ok=True))
    assert updated["reachable"] is True
    assert updated["last_probe_ok"] is True
    assert remote_filename_for("Printer/folder/part.gcode") == "part.gcode"


def test_upload_sftp(tmp_path: Path) -> None:
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; gcode\n", encoding="utf-8")
    transfer = normalize_transfer(
        {
            "protocol": "sftp",
            "host": "nas.local",
            "username": "pi",
            "password": "secret",
            "remote_path": "gcodes",
        }
    )
    sftp = MagicMock()
    ssh = MagicMock()
    with patch("auto_slicer.transfer._sftp_connect", return_value=(ssh, sftp)):
        result = upload_file(transfer, gcode)
    assert result.ok is True
    sftp.put.assert_called_once()
    assert str(sftp.put.call_args.args[1]).endswith("part.gcode")
