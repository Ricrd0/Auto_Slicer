from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from auto_slicer.machine_sync import normalize_transfer, transfer_configured

PROBE_TIMEOUT = 5.0
UPLOAD_TIMEOUT = 120.0


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "error": self.error, "reachable": self.ok}


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    error: str | None = None
    remote_name: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "error": self.error, "remote_name": self.remote_name}


class TransferError(RuntimeError):
    pass


def probe_transfer(transfer: dict[str, object] | None) -> ProbeResult:
    config = normalize_transfer(transfer)
    protocol = str(config["protocol"])
    if protocol == "none":
        return ProbeResult(ok=False, error="No transfer protocol configured")
    try:
        if protocol == "moonraker":
            _probe_moonraker(config)
        elif protocol == "snapmaker":
            _probe_snapmaker(config)
        elif protocol == "sftp":
            _probe_sftp(config)
        else:
            return ProbeResult(ok=False, error=f"Unknown protocol: {protocol}")
    except TransferError as exc:
        return ProbeResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any probe failure to the UI
        return ProbeResult(ok=False, error=str(exc))
    return ProbeResult(ok=True)


def upload_file(
    transfer: dict[str, object] | None,
    local_path: Path,
    *,
    remote_name: str | None = None,
) -> UploadResult:
    config = normalize_transfer(transfer)
    if not transfer_configured(config):
        return UploadResult(ok=False, error="No transfer protocol configured")
    if not local_path.is_file():
        return UploadResult(ok=False, error=f"Local file not found: {local_path}")
    name = remote_name or local_path.name
    try:
        protocol = str(config["protocol"])
        if protocol == "moonraker":
            _upload_moonraker(config, local_path, name)
        elif protocol == "snapmaker":
            _upload_snapmaker(config, local_path, name)
        elif protocol == "sftp":
            _upload_sftp(config, local_path, name)
        else:
            return UploadResult(ok=False, error=f"Unknown protocol: {protocol}")
    except TransferError as exc:
        return UploadResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface upload failures without failing the slice
        return UploadResult(ok=False, error=str(exc))
    return UploadResult(ok=True, remote_name=name)


def auto_upload_allowed(transfer: dict[str, object] | None) -> bool:
    """Auto mode may run only when configured and last successful probe still holds."""
    config = normalize_transfer(transfer)
    return (
        transfer_configured(config)
        and str(config.get("mode")) == "auto"
        and bool(config.get("last_probe_ok"))
        and bool(config.get("reachable"))
    )


def with_probe_result(
    transfer: dict[str, object] | None, result: ProbeResult
) -> dict[str, object]:
    config = normalize_transfer(transfer)
    config["reachable"] = result.ok
    config["last_probe_ok"] = result.ok
    config["last_probe_error"] = result.error
    return config


def _base_url(transfer: dict[str, object]) -> str:
    host = str(transfer["host"]).strip()
    port = transfer.get("port")
    scheme = "https" if transfer.get("tls") else "http"
    if port in (None, ""):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{int(port)}"


def _moonraker_headers(transfer: dict[str, object]) -> dict[str, str]:
    key = str(transfer.get("api_key") or "").strip()
    if not key:
        return {}
    return {"X-Api-Key": key}


def _probe_moonraker(transfer: dict[str, object]) -> None:
    url = f"{_base_url(transfer)}/server/info"
    try:
        response = httpx.get(
            url, headers=_moonraker_headers(transfer), timeout=PROBE_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise TransferError(f"Moonraker unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise TransferError(f"Moonraker probe failed ({response.status_code}): {response.text[:200]}")


def _upload_moonraker(transfer: dict[str, object], local_path: Path, remote_name: str) -> None:
    url = f"{_base_url(transfer)}/server/files/upload"
    remote_path = str(transfer.get("remote_path") or "").strip().strip("/")
    data: dict[str, str] = {"root": "gcodes", "print": "false"}
    if remote_path:
        data["path"] = remote_path
    try:
        with local_path.open("rb") as handle:
            files = {"file": (remote_name, handle, "application/octet-stream")}
            response = httpx.post(
                url,
                headers=_moonraker_headers(transfer),
                data=data,
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise TransferError(f"Moonraker upload failed: {exc}") from exc
    if response.status_code not in (200, 201):
        raise TransferError(
            f"Moonraker upload failed ({response.status_code}): {response.text[:300]}"
        )


def _snapmaker_token(transfer: dict[str, object]) -> str:
    token = str(transfer.get("token") or "").strip()
    if not token:
        raise TransferError("Snapmaker token is required")
    return token


def _probe_snapmaker(transfer: dict[str, object]) -> None:
    token = _snapmaker_token(transfer)
    base = _base_url(transfer)
    try:
        connect = httpx.post(
            f"{base}/api/v1/connect",
            params={"token": token},
            timeout=PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise TransferError(f"Snapmaker unreachable: {exc}") from exc
    if connect.status_code >= 400:
        raise TransferError(
            f"Snapmaker connect failed ({connect.status_code}): {connect.text[:200]}"
        )
    try:
        status = httpx.get(
            f"{base}/api/v1/status",
            params={"token": token},
            timeout=PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise TransferError(f"Snapmaker status failed: {exc}") from exc
    if status.status_code >= 400:
        raise TransferError(
            f"Snapmaker status failed ({status.status_code}): {status.text[:200]}"
        )


def _upload_snapmaker(transfer: dict[str, object], local_path: Path, remote_name: str) -> None:
    token = _snapmaker_token(transfer)
    base = _base_url(transfer)
    try:
        connect = httpx.post(
            f"{base}/api/v1/connect",
            params={"token": token},
            timeout=PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise TransferError(f"Snapmaker connect failed: {exc}") from exc
    if connect.status_code >= 400:
        raise TransferError(
            f"Snapmaker connect failed ({connect.status_code}): {connect.text[:200]}"
        )
    # prepare_print uploads the file; do not call start_print.
    try:
        with local_path.open("rb") as handle:
            files = {"file": (remote_name, handle, "application/octet-stream")}
            response = httpx.post(
                f"{base}/api/v1/prepare_print",
                params={"token": token},
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise TransferError(f"Snapmaker upload failed: {exc}") from exc
    if response.status_code >= 400:
        raise TransferError(
            f"Snapmaker upload failed ({response.status_code}): {response.text[:300]}"
        )


def _probe_sftp(transfer: dict[str, object]) -> None:
    ssh, sftp = _sftp_connect(transfer)
    try:
        sftp.listdir(".")
    finally:
        _sftp_close(ssh, sftp)


def _upload_sftp(transfer: dict[str, object], local_path: Path, remote_name: str) -> None:
    import posixpath

    ssh, sftp = _sftp_connect(transfer)
    try:
        remote_dir = str(transfer.get("remote_path") or "").strip().strip("/")
        if remote_dir:
            _sftp_makedirs(sftp, remote_dir)
            remote_file = posixpath.join(remote_dir, remote_name)
        else:
            remote_file = remote_name
        sftp.put(str(local_path), remote_file)
    finally:
        _sftp_close(ssh, sftp)


def _sftp_connect(transfer: dict[str, object]) -> tuple[Any, Any]:
    try:
        import paramiko
    except ImportError as exc:
        raise TransferError("SFTP support requires the paramiko package") from exc
    host = str(transfer["host"]).strip()
    port = int(transfer.get("port") or 22)
    username = str(transfer.get("username") or "").strip()
    password = str(transfer.get("password") or "")
    if not username:
        raise TransferError("SFTP username is required")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            timeout=PROBE_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:  # noqa: BLE001 — wrap paramiko auth/network errors
        raise TransferError(f"SFTP connection failed: {exc}") from exc
    return ssh, ssh.open_sftp()


def _sftp_close(ssh: Any, sftp: Any) -> None:
    try:
        sftp.close()
    finally:
        ssh.close()


def _sftp_makedirs(sftp: Any, remote_dir: str) -> None:
    import posixpath

    parts: list[str] = []
    path = remote_dir
    while path not in ("", "/"):
        parts.append(path)
        path = posixpath.dirname(path)
    for directory in reversed(parts):
        try:
            sftp.stat(directory)
        except OSError:
            sftp.mkdir(directory)


def remote_filename_for(output_path: str) -> str:
    """Use the basename of the local relative output path for remote upload."""
    return Path(output_path).name
