from __future__ import annotations

import posixpath
import re
import stat
import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from .bejerman_pdf_settings import BejermanPdfOutputSettingsError


REMOTE_PREFIX = "samtronic:"
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class RemotePdfWriteResult:
    path: str
    created: bool


def _clean(value: Any) -> str:
    return str(value or "").strip()


def is_remote_pdf_path(value: Any) -> bool:
    return _clean(value).lower().startswith(REMOTE_PREFIX)


def remote_pdf_uri(windows_path: Any) -> str:
    path = _clean(windows_path)
    if is_remote_pdf_path(path):
        return path
    if not WINDOWS_PATH_RE.match(path):
        raise BejermanPdfOutputSettingsError("La ruta remota de Samtronic debe ser una ruta Windows absoluta.")
    return f"{REMOTE_PREFIX}{path}"


def remote_windows_path(uri: Any) -> str:
    value = _clean(uri)
    if not is_remote_pdf_path(value):
        raise BejermanPdfOutputSettingsError("La ruta no es un destino remoto de Samtronic.")
    path = value[len(REMOTE_PREFIX):].strip()
    if not WINDOWS_PATH_RE.match(path):
        raise BejermanPdfOutputSettingsError("La ruta remota de Samtronic debe ser una ruta Windows absoluta.")
    return path


def _sftp_path(windows_path: str) -> str:
    return windows_path.replace("\\", "/")


def _remote_options() -> dict[str, str | int]:
    return {
        "host": _clean(
            getattr(settings, "BEJERMAN_PDF_SAMTRONIC_SSH_HOST", "")
            or getattr(settings, "BEJERMAN_SSH_HOST", "")
            or "45.173.2.155"
        ),
        "port": int(
            _clean(
                getattr(settings, "BEJERMAN_PDF_SAMTRONIC_SSH_PORT", "")
                or getattr(settings, "BEJERMAN_SSH_PORT", "")
                or "22"
            )
        ),
        "user": _clean(
            getattr(settings, "BEJERMAN_PDF_SAMTRONIC_SSH_USER", "")
            or getattr(settings, "BEJERMAN_SSH_USER", "")
            or "administrator"
        ),
        "password": _clean(
            getattr(settings, "BEJERMAN_PDF_SAMTRONIC_SSH_PASSWORD", "")
            or getattr(settings, "BEJERMAN_SSH_PASSWORD", "")
        ),
    }


def _connect():
    try:
        import paramiko
    except ImportError as exc:
        raise BejermanPdfOutputSettingsError("Falta instalar paramiko para escribir PDFs en Samtronic por SSH.") from exc

    options = _remote_options()
    if not options["host"] or not options["user"] or not options["password"]:
        raise BejermanPdfOutputSettingsError(
            "Faltan credenciales SSH de Samtronic para guardar PDFs remotos."
        )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        str(options["host"]),
        port=int(options["port"]),
        username=str(options["user"]),
        password=str(options["password"]),
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
    )
    return client


def _remote_stat_dir(sftp, path: str) -> None:
    try:
        attrs = sftp.stat(path)
    except OSError as exc:
        raise BejermanPdfOutputSettingsError(f"La carpeta remota no existe en Samtronic: {path}") from exc
    if not stat.S_ISDIR(attrs.st_mode or 0):
        raise BejermanPdfOutputSettingsError(f"La ruta remota no es una carpeta en Samtronic: {path}")


def validate_remote_pdf_output_dir(uri: Any) -> str:
    windows_path = remote_windows_path(uri)
    path = _sftp_path(windows_path)
    client = _connect()
    try:
        sftp = client.open_sftp()
        try:
            _remote_stat_dir(sftp, path)
            test_name = f".nexora-pdf-write-{uuid.uuid4().hex}.tmp"
            test_path = posixpath.join(path.rstrip("/"), test_name)
            with sftp.open(test_path, "wb") as handle:
                handle.write(b"ok")
            sftp.remove(test_path)
        finally:
            sftp.close()
    finally:
        client.close()
    return remote_pdf_uri(windows_path)


def _read_remote_bytes(sftp, path: str) -> bytes | None:
    try:
        with sftp.open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _remote_exists(sftp, path: str) -> bool:
    try:
        sftp.stat(path)
        return True
    except OSError:
        return False


def _join_windows(directory: str, filename: str) -> str:
    clean_dir = directory.rstrip("/").rstrip("\\")
    return clean_dir + "\\" + filename


def write_remote_pdf(directory_uri: Any, filename: str, pdf: bytes) -> RemotePdfWriteResult:
    windows_dir = remote_windows_path(directory_uri)
    directory = _sftp_path(windows_dir).rstrip("/")
    client = _connect()
    try:
        sftp = client.open_sftp()
        try:
            _remote_stat_dir(sftp, directory)
            base, dot, suffix = filename.rpartition(".")
            if not dot:
                base, suffix = filename, ""
            candidates = [filename]
            candidates.extend(f"{base}_{index:02d}.{suffix}" if suffix else f"{base}_{index:02d}" for index in range(1, 100))
            for candidate in candidates:
                remote_path = posixpath.join(directory, candidate)
                existing = _read_remote_bytes(sftp, remote_path)
                if existing == pdf:
                    return RemotePdfWriteResult(path=_join_windows(windows_dir, candidate), created=False)
                if existing is None and not _remote_exists(sftp, remote_path):
                    with sftp.open(remote_path, "wb") as handle:
                        handle.write(pdf)
                    return RemotePdfWriteResult(path=_join_windows(windows_dir, candidate), created=True)
            fallback = f"{base}_{uuid.uuid4().hex[:8]}.{suffix}" if suffix else f"{base}_{uuid.uuid4().hex[:8]}"
            remote_path = posixpath.join(directory, fallback)
            with sftp.open(remote_path, "wb") as handle:
                handle.write(pdf)
            return RemotePdfWriteResult(path=_join_windows(windows_dir, fallback), created=True)
        finally:
            sftp.close()
    finally:
        client.close()


def read_remote_pdf(directory_uri: Any, filename: str) -> bytes | None:
    windows_dir = remote_windows_path(directory_uri)
    directory = _sftp_path(windows_dir).rstrip("/")
    client = _connect()
    try:
        sftp = client.open_sftp()
        try:
            _remote_stat_dir(sftp, directory)
            return _read_remote_bytes(sftp, posixpath.join(directory, filename))
        finally:
            sftp.close()
    finally:
        client.close()


def remove_remote_file_if_same(file_uri: Any, expected: bytes) -> bool:
    windows_file = remote_windows_path(file_uri)
    path = _sftp_path(windows_file)
    client = _connect()
    try:
        sftp = client.open_sftp()
        try:
            existing = _read_remote_bytes(sftp, path)
            if existing is None or existing != expected:
                return False
            sftp.remove(path)
            return True
        finally:
            sftp.close()
    finally:
        client.close()
