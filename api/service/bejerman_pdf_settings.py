from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from django.conf import settings
from django.db import connection, transaction

from .bejerman_companies import company_for_key, list_ingress_companies


logger = logging.getLogger(__name__)

DOCUMENT_KIND_REMITOS = "REMITOS"
DOCUMENT_KIND_FACTURAS = "FACTURAS"
DOCUMENT_KINDS = (DOCUMENT_KIND_REMITOS, DOCUMENT_KIND_FACTURAS)
WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]*(.*)$")


class BejermanPdfOutputSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class PdfOutputSettingValue:
    company_key: str
    document_kind: str
    output_dir: str
    effective_dir: str
    resolved_dir: str
    source: str
    validation_error: str = ""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_pdf_company_key(value: Any) -> str:
    company = company_for_key(value, default=None)
    if not company:
        raise BejermanPdfOutputSettingsError("Empresa Bejerman no válida para configurar PDFs.")
    return company.key


def normalize_document_kind(value: Any) -> str:
    kind = _clean(value).upper()
    if kind not in DOCUMENT_KINDS:
        raise BejermanPdfOutputSettingsError("Tipo de documento PDF no válido.")
    return kind


def _table_exists() -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = 'bejerman_pdf_output_settings'
                 LIMIT 1
                """
            )
            return bool(cur.fetchone())
    except Exception:
        return False


def _load_db_output_dir(company_key: str, document_kind: str) -> str:
    if not _table_exists():
        return ""
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT output_dir
                  FROM bejerman_pdf_output_settings
                 WHERE company_key = %s
                   AND document_kind = %s
                 LIMIT 1
                """,
                [company_key, document_kind],
            )
            row = cur.fetchone()
    except Exception:
        logger.warning("bejerman_pdf_settings_lookup_failed", exc_info=True)
        return ""
    return _clean(row[0]) if row else ""


def _env_output_dir(company_key: str, document_kind: str, env_lookup: Callable[[str], Any] | None = None) -> str:
    lookup = env_lookup or (lambda name: getattr(settings, name, ""))
    return _clean(lookup(f"BEJERMAN_PDF_{company_key}_{document_kind}_DIR")) or _clean(
        lookup(f"BEJERMAN_PDF_{document_kind}_DIR")
    )


def _windows_mounts() -> dict[str, str]:
    raw = _clean(getattr(settings, "BEJERMAN_PDF_WINDOWS_PATH_MOUNTS", "Z=/mnt/nexora-z"))
    mounts: dict[str, str] = {}
    for item in raw.split(","):
        part = item.strip()
        if not part or "=" not in part:
            continue
        drive, target = part.split("=", 1)
        drive_key = drive.strip().upper().rstrip(":")
        target_dir = target.strip()
        if drive_key and target_dir:
            mounts[drive_key] = target_dir
    return mounts


def _windows_path_match(path: str) -> re.Match[str] | None:
    return WINDOWS_DRIVE_RE.match(_clean(path))


def resolve_pdf_output_path(output_dir: Any) -> str:
    raw = _clean(output_dir)
    if not raw:
        return ""
    from .bejerman_pdf_remote import is_remote_pdf_path, remote_pdf_uri

    if is_remote_pdf_path(raw):
        return remote_pdf_uri(raw)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise BejermanPdfOutputSettingsError(
            "Las rutas UNC no están soportadas dentro del contenedor. Use una unidad montada, por ejemplo Z:\\..."
        )
    match = _windows_path_match(raw)
    if not match:
        return raw

    drive = match.group(1).upper()
    rest = match.group(2).replace("\\", "/").strip("/")
    mount_root = _clean(_windows_mounts().get(drive))
    if not mount_root and os.name == "nt":
        return raw
    if not mount_root:
        raise BejermanPdfOutputSettingsError(
            f"La unidad {drive}: no tiene un mount configurado para guardar PDFs."
        )
    if is_remote_pdf_path(mount_root):
        base = mount_root.rstrip("\\/")
        remote_path = base if not rest else base + "\\" + rest.replace("/", "\\")
        return remote_pdf_uri(remote_path)
    root_path = Path(mount_root)
    if not root_path.exists() or not root_path.is_dir():
        raise BejermanPdfOutputSettingsError(
            f"La unidad {drive}: no está montada en el contenedor ({mount_root})."
        )
    return str(root_path / rest) if rest else str(root_path)


def validate_pdf_output_dir(output_dir: Any) -> str:
    raw = _clean(output_dir)
    if not raw:
        return ""
    resolved = resolve_pdf_output_path(raw)
    from .bejerman_pdf_remote import is_remote_pdf_path, validate_remote_pdf_output_dir

    if is_remote_pdf_path(resolved):
        return validate_remote_pdf_output_dir(resolved)
    path = Path(resolved)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BejermanPdfOutputSettingsError(f"No se pudo crear la carpeta configurada: {exc}") from exc
    if not path.is_dir():
        raise BejermanPdfOutputSettingsError("La ruta configurada no es una carpeta.")
    try:
        fd, test_path = tempfile.mkstemp(prefix=".nexora-pdf-write-", dir=path)
        os.close(fd)
        Path(test_path).unlink(missing_ok=True)
    except OSError as exc:
        raise BejermanPdfOutputSettingsError(f"No se puede escribir en la carpeta configurada: {exc}") from exc
    return resolved


def configured_pdf_output_dir(
    company_key: Any,
    document_kind: Any,
    *,
    env_lookup: Callable[[str], Any] | None = None,
) -> str:
    company = normalize_pdf_company_key(company_key)
    kind = normalize_document_kind(document_kind)
    raw = _load_db_output_dir(company, kind) or _env_output_dir(company, kind, env_lookup)
    if not raw:
        return ""
    return resolve_pdf_output_path(raw)


def _setting_value(company_key: str, document_kind: str) -> PdfOutputSettingValue:
    db_dir = _load_db_output_dir(company_key, document_kind)
    env_dir = _env_output_dir(company_key, document_kind)
    effective = db_dir or env_dir
    source = "db" if db_dir else ("env" if env_dir else "none")
    resolved = ""
    validation_error = ""
    if effective:
        try:
            resolved = resolve_pdf_output_path(effective)
        except BejermanPdfOutputSettingsError as exc:
            validation_error = str(exc)
    return PdfOutputSettingValue(
        company_key=company_key,
        document_kind=document_kind,
        output_dir=db_dir,
        effective_dir=effective,
        resolved_dir=resolved,
        source=source,
        validation_error=validation_error,
    )


def _value_dict(value: PdfOutputSettingValue) -> dict[str, str]:
    return {
        "outputDir": value.output_dir,
        "effectiveDir": value.effective_dir,
        "resolvedDir": value.resolved_dir,
        "source": value.source,
        "validationError": value.validation_error,
    }


def serialize_pdf_output_settings() -> dict[str, Any]:
    from .bejerman_pdf_remote import is_remote_pdf_path

    companies = list_ingress_companies()
    company_dicts = [
        {
            **company.as_public_dict(),
            "bejermanCompany": company.bejerman_company,
        }
        for company in companies
    ]
    items = []
    for company in companies:
        remitos = _setting_value(company.key, DOCUMENT_KIND_REMITOS)
        facturas = _setting_value(company.key, DOCUMENT_KIND_FACTURAS)
        items.append(
            {
                "companyKey": company.key,
                "companyLabel": company.label,
                "bejermanCompany": company.bejerman_company,
                "remitos": _value_dict(remitos),
                "facturas": _value_dict(facturas),
            }
        )
    return {
        "items": items,
        "companies": company_dicts,
        "documentKinds": list(DOCUMENT_KINDS),
        "windowsPathMounts": [
            {
                "drive": drive,
                "containerDir": target,
                "mounted": True if is_remote_pdf_path(target) else Path(target).is_dir(),
                "remote": is_remote_pdf_path(target),
            }
            for drive, target in sorted(_windows_mounts().items())
        ],
    }


def _payload_dir(item: dict[str, Any], key: str) -> str:
    nested = item.get(key)
    if isinstance(nested, dict):
        return _clean(nested.get("outputDir"))
    camel = f"{key}Dir"
    return _clean(item.get(camel) or item.get(camel[0].upper() + camel[1:]))


def update_pdf_output_settings(items: list[dict[str, Any]], *, actor_user_id: int | None = None) -> dict[str, Any]:
    if not isinstance(items, list):
        raise BejermanPdfOutputSettingsError("La configuración debe enviarse como una lista de empresas.")

    updates: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise BejermanPdfOutputSettingsError("Cada configuración de empresa debe ser un objeto.")
        company_key = normalize_pdf_company_key(item.get("companyKey") or item.get("company_key"))
        for field, document_kind in (("remitos", DOCUMENT_KIND_REMITOS), ("facturas", DOCUMENT_KIND_FACTURAS)):
            output_dir = _payload_dir(item, field)
            if output_dir:
                validate_pdf_output_dir(output_dir)
            updates.append((company_key, document_kind, output_dir))

    with transaction.atomic():
        with connection.cursor() as cur:
            for company_key, document_kind, output_dir in updates:
                if output_dir:
                    cur.execute(
                        """
                        INSERT INTO bejerman_pdf_output_settings(company_key, document_kind, output_dir, updated_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (company_key, document_kind)
                        DO UPDATE SET
                          output_dir = EXCLUDED.output_dir,
                          updated_by = EXCLUDED.updated_by,
                          updated_at = CURRENT_TIMESTAMP
                        """,
                        [company_key, document_kind, output_dir, actor_user_id],
                    )
                else:
                    cur.execute(
                        """
                        DELETE FROM bejerman_pdf_output_settings
                         WHERE company_key = %s
                           AND document_kind = %s
                        """,
                        [company_key, document_kind],
                    )
    return serialize_pdf_output_settings()
