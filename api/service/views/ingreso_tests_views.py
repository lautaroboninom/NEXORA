from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any

from django.db import connection
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from ..test_pdf import render_ingreso_test_pdf
from ..test_protocols import (
    default_values_for_protocol,
    flatten_items,
    get_protocol_by_template_key,
    resolve_protocol_for_equipo,
)
from ..permissions import require_any_permission
from .helpers import _rol, _set_audit_user, exec_void, q, require_permission


_EDIT_ROLES = {"tecnico", "jefe", "jefe_veedor", "admin", "supervisor"}
_VIEW_ROLES = {"tecnico", "jefe", "jefe_veedor", "admin", "supervisor", "recepcion"}
_BLOCKED_EDIT_STATES = {"entregado", "baja", "vendido_pendiente_entrega", "vendido_entregado"}
_VALID_GLOBAL_RESULTS = {"", "pendiente", "apto", "apto_condicional", "no_apto"}
_VALID_ITEM_RESULTS = {"", "ok", "observado", "no_ok", "na"}


def _safe_json(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return default
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default
    return default


def _trim_text(value: Any, max_len: int = 2000) -> str:
    s = "" if value is None else str(value)
    s = s.strip()
    if len(s) > max_len:
        return s[:max_len]
    return s


def _default_instrumentos_for_protocol(protocol: dict[str, Any] | None) -> str:
    return _trim_text((protocol or {}).get("default_instrumentos"), max_len=2000)


def _has_ingreso_tests_table() -> bool:
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name='ingreso_tests'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
                return cur.fetchone() is not None
            if connection.vendor == "sqlite":
                cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingreso_tests' LIMIT 1")
                return cur.fetchone() is not None
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_name='ingreso_tests'
                 LIMIT 1
                """
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _load_ingreso_context(ingreso_id: int) -> dict[str, Any] | None:
    return q(
        """
        SELECT
          t.id AS ingreso_id,
          t.estado,
          t.asignado_a,
          COALESCE(c.razon_social,'') AS cliente,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(NULLIF(m.tipo_equipo,''), NULLIF(d.tipo_equipo,''), '') AS tipo_equipo,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )


def _can_operate_on_ingreso(request, ingreso: dict[str, Any], for_edit: bool) -> None:
    rol = (_rol(request) or "").strip().lower()
    allowed = _EDIT_ROLES if for_edit else _VIEW_ROLES
    if rol not in allowed:
        raise PermissionDenied("No autorizado")
    if for_edit and (ingreso.get("estado") or "").strip().lower() in _BLOCKED_EDIT_STATES:
        raise PermissionDenied("No se puede editar test en estado entregado, baja o vendido")
    if rol in ("tecnico", "jefe_veedor"):
        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        if int(ingreso.get("asignado_a") or 0) != int(uid or 0):
            raise PermissionDenied("Sólo el técnico asignado puede operar este test")


def _load_test_row(ingreso_id: int) -> dict[str, Any] | None:
    return q(
        """
        SELECT
          t.ingreso_id,
          t.template_key,
          t.template_version,
          t.tipo_equipo_snapshot,
          t.payload,
          t.schema_snapshot,
          t.references_snapshot,
          t.resultado_global,
          t.conclusion,
          t.instrumentos,
          t.firmado_por,
          t.fecha_ejecucion,
          t.tecnico_id,
          COALESCE(u.nombre, '') AS tecnico_nombre
        FROM ingreso_tests t
        LEFT JOIN users u ON u.id = t.tecnico_id
        WHERE t.ingreso_id = %s
        """,
        [ingreso_id],
        one=True,
    )


def _resolve_protocol_for_row(ingreso: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any] | None:
    marca = ingreso.get("marca") or ""
    modelo = ingreso.get("modelo") or ""
    numero_serie = ingreso.get("numero_serie") or ""
    protocol_live = resolve_protocol_for_equipo(
        ingreso.get("tipo_equipo") or "",
        marca=marca,
        modelo=modelo,
        numero_serie=numero_serie,
    )
    protocol_row = None
    if row and (row.get("template_key") or "").strip():
        protocol_row = get_protocol_by_template_key(
            row.get("template_key") or "",
            marca=marca,
            modelo=modelo,
            numero_serie=numero_serie,
        )

    if (protocol_live or {}).get("template_key") == "aspirador_electrico_v1":
        return protocol_live
    if protocol_row is not None:
        return protocol_row
    return protocol_live


def _extract_values_from_payload(payload: Any) -> dict[str, Any]:
    doc = _safe_json(payload, {})
    if not isinstance(doc, dict):
        return {}
    values = doc.get("values")
    if isinstance(values, dict):
        return values
    return {}


def _normalize_values_for_protocol(raw_values: Any, protocol: dict[str, Any]) -> dict[str, dict[str, str]]:
    defaults = default_values_for_protocol(protocol)
    incoming = raw_values if isinstance(raw_values, dict) else {}
    out: dict[str, dict[str, str]] = copy.deepcopy(defaults)

    for item in flatten_items(protocol):
        key = (item.get("key") or "").strip()
        if not key:
            continue
        src = incoming.get(key) if isinstance(incoming, dict) else None
        if not isinstance(src, dict):
            continue
        valor_a_medir = _trim_text(src.get("valor_a_medir"), max_len=250)
        measured = _trim_text(src.get("measured"), max_len=250)
        result = _trim_text(src.get("result"), max_len=40).lower()
        if result not in _VALID_ITEM_RESULTS:
            result = ""
        observaciones = _trim_text(src.get("observaciones"), max_len=900)
        out[key] = {
            "valor_a_medir": valor_a_medir,
            "measured": measured,
            "result": result,
            "observaciones": observaciones,
        }
    return out


def _merge_values(
    existing_values: dict[str, Any],
    incoming_values: dict[str, Any] | None,
    protocol: dict[str, Any],
) -> dict[str, dict[str, str]]:
    base = _normalize_values_for_protocol(existing_values or {}, protocol)
    if incoming_values is None:
        return base
    merged = copy.deepcopy(base)
    norm_in = _normalize_values_for_protocol(incoming_values, protocol)
    for key, value in norm_in.items():
        merged[key] = value
    return merged


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _save_test_row(
    ingreso_id: int,
    template_key: str,
    template_version: str,
    tipo_equipo_snapshot: str,
    payload_doc: dict[str, Any],
    schema_snapshot: dict[str, Any],
    references_snapshot: list[dict[str, Any]],
    resultado_global: str,
    conclusion: str,
    instrumentos: str,
    firmado_por: str,
    tecnico_id: int | None,
) -> None:
    is_pg = connection.vendor == "postgresql"
    now_dt = timezone.now()
    payload_json = _json_param(payload_doc)
    schema_json = _json_param(schema_snapshot)
    refs_json = _json_param(references_snapshot)

    if is_pg:
        exec_void(
            """
            INSERT INTO ingreso_tests (
                ingreso_id,
                template_key,
                template_version,
                tipo_equipo_snapshot,
                payload,
                schema_snapshot,
                references_snapshot,
                resultado_global,
                conclusion,
                instrumentos,
                firmado_por,
                fecha_ejecucion,
                tecnico_id,
                created_at,
                updated_at
            )
            VALUES (
                %s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,NULLIF(%s,''),NULLIF(%s,''),NULLIF(%s,''),%s,%s,%s,%s
            )
            ON CONFLICT (ingreso_id)
            DO UPDATE SET
                template_key = EXCLUDED.template_key,
                template_version = EXCLUDED.template_version,
                tipo_equipo_snapshot = EXCLUDED.tipo_equipo_snapshot,
                payload = EXCLUDED.payload,
                schema_snapshot = EXCLUDED.schema_snapshot,
                references_snapshot = EXCLUDED.references_snapshot,
                resultado_global = EXCLUDED.resultado_global,
                conclusion = EXCLUDED.conclusion,
                instrumentos = EXCLUDED.instrumentos,
                firmado_por = EXCLUDED.firmado_por,
                fecha_ejecucion = EXCLUDED.fecha_ejecucion,
                tecnico_id = EXCLUDED.tecnico_id,
                updated_at = EXCLUDED.updated_at
            """,
            [
                ingreso_id,
                template_key,
                template_version,
                tipo_equipo_snapshot,
                payload_json,
                schema_json,
                refs_json,
                resultado_global,
                conclusion,
                instrumentos,
                firmado_por,
                now_dt,
                tecnico_id,
                now_dt,
                now_dt,
            ],
        )
        return

    # Generic fallback for sqlite tests.
    row = _load_test_row(ingreso_id)
    if row:
        exec_void(
            """
            UPDATE ingreso_tests
               SET template_key=%s,
                   template_version=%s,
                   tipo_equipo_snapshot=%s,
                   payload=%s,
                   schema_snapshot=%s,
                   references_snapshot=%s,
                   resultado_global=%s,
                   conclusion=NULLIF(%s,''),
                   instrumentos=NULLIF(%s,''),
                   firmado_por=NULLIF(%s,''),
                   fecha_ejecucion=%s,
                   tecnico_id=%s,
                   updated_at=%s
             WHERE ingreso_id=%s
            """,
            [
                template_key,
                template_version,
                tipo_equipo_snapshot,
                payload_json,
                schema_json,
                refs_json,
                resultado_global,
                conclusion,
                instrumentos,
                firmado_por,
                now_dt,
                tecnico_id,
                now_dt,
                ingreso_id,
            ],
        )
    else:
        exec_void(
            """
            INSERT INTO ingreso_tests (
                ingreso_id,
                template_key,
                template_version,
                tipo_equipo_snapshot,
                payload,
                schema_snapshot,
                references_snapshot,
                resultado_global,
                conclusion,
                instrumentos,
                firmado_por,
                fecha_ejecucion,
                tecnico_id,
                created_at,
                updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,''),NULLIF(%s,''),NULLIF(%s,''),%s,%s,%s,%s)
            """,
            [
                ingreso_id,
                template_key,
                template_version,
                tipo_equipo_snapshot,
                payload_json,
                schema_json,
                refs_json,
                resultado_global,
                conclusion,
                instrumentos,
                firmado_por,
                now_dt,
                tecnico_id,
                now_dt,
                now_dt,
            ],
        )


def _protocol_sections_with_values(protocol: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
    sections = copy.deepcopy(protocol.get("sections") or [])
    defaults = default_values_for_protocol(protocol)
    for section in sections:
        for item in section.get("items", []) or []:
            key = (item.get("key") or "").strip()
            item["value"] = values.get(key) or defaults.get(key) or {
                "valor_a_medir": "",
                "measured": "",
                "result": "",
                "observaciones": "",
            }
            if not isinstance(item.get("ref_ids"), list):
                item["ref_ids"] = []
    return sections


def _extract_schema_snapshot(row: dict[str, Any] | None) -> dict[str, Any]:
    doc = _safe_json((row or {}).get("schema_snapshot"), {})
    if not isinstance(doc, dict):
        return {}
    if not isinstance(doc.get("sections"), list):
        return {}
    if not isinstance(doc.get("references"), list):
        doc["references"] = []
    if not isinstance(doc.get("result_options"), list):
        doc["result_options"] = []
    if not isinstance(doc.get("global_result_options"), list):
        doc["global_result_options"] = []
    return doc


def _build_schema_snapshot(
    protocol: dict[str, Any],
    references_snapshot: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "template_key": protocol.get("template_key") or "",
        "template_version": protocol.get("template_version") or "",
        "display_name": protocol.get("display_name") or "",
        "default_instrumentos": protocol.get("default_instrumentos") or "",
        "references": copy.deepcopy(references_snapshot or []),
        "sections": copy.deepcopy(protocol.get("sections") or []),
        "result_options": copy.deepcopy(protocol.get("result_options") or []),
        "global_result_options": copy.deepcopy(protocol.get("global_result_options") or []),
    }


def _protocol_from_schema_snapshot(
    snapshot: dict[str, Any],
    protocol_fallback: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if (protocol_fallback or {}).get("template_key") == "aspirador_electrico_v1":
        return protocol_fallback
    if snapshot and isinstance(snapshot.get("sections"), list):
        out = {
            "type_key": (protocol_fallback or {}).get("type_key") or "",
            "template_key": snapshot.get("template_key") or (protocol_fallback or {}).get("template_key") or "",
            "template_version": snapshot.get("template_version")
            or (protocol_fallback or {}).get("template_version")
            or "",
            "display_name": snapshot.get("display_name") or (protocol_fallback or {}).get("display_name") or "",
            "default_instrumentos": snapshot.get("default_instrumentos")
            or (protocol_fallback or {}).get("default_instrumentos")
            or "",
            "references": copy.deepcopy(snapshot.get("references") or []),
            "sections": copy.deepcopy(snapshot.get("sections") or []),
            "result_options": copy.deepcopy(
                snapshot.get("result_options") or (protocol_fallback or {}).get("result_options") or []
            ),
            "global_result_options": copy.deepcopy(
                snapshot.get("global_result_options")
                or (protocol_fallback or {}).get("global_result_options")
                or []
            ),
            "applied_overrides": [],
        }
        return out
    return protocol_fallback


class IngresoTestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_any_permission(
            request,
            [
                "page.ingresos_history",
                "page.work_queues",
                "page.budget_queues",
                "page.logistics",
                "action.ingreso.edit_diagnosis",
            ],
        )
        if not _has_ingreso_tests_table():
            return Response({"detail": "Tabla ingreso_tests inexistente. Ejecuta schema."}, status=503)

        ingreso = _load_ingreso_context(ingreso_id)
        if not ingreso:
            return Response({"detail": "Ingreso no encontrado"}, status=404)

        _can_operate_on_ingreso(request, ingreso, for_edit=False)
        _set_audit_user(request)

        row = _load_test_row(ingreso_id)
        protocol_live = _resolve_protocol_for_row(ingreso, row)
        schema_snapshot = _extract_schema_snapshot(row)
        protocol = _protocol_from_schema_snapshot(schema_snapshot, protocol_live)
        if protocol is None:
            return Response(
                {"detail": "No hay protocolo de test para este tipo de equipo", "tipo_equipo": ingreso.get("tipo_equipo") or ""},
                status=409,
            )

        payload_values = _extract_values_from_payload(row.get("payload")) if row else {}
        values = _merge_values(payload_values, None, protocol)
        references_snapshot = _safe_json((row or {}).get("references_snapshot"), [])
        if not isinstance(references_snapshot, list):
            references_snapshot = []
        if schema_snapshot and isinstance(schema_snapshot.get("references"), list):
            references = copy.deepcopy(schema_snapshot.get("references") or [])
        else:
            references = references_snapshot if references_snapshot else copy.deepcopy(protocol.get("references") or [])

        template_key = (row or {}).get("template_key") or protocol.get("template_key")
        template_version = (row or {}).get("template_version") or protocol.get("template_version")
        instrumentos = (row or {}).get("instrumentos") or _default_instrumentos_for_protocol(protocol)

        return Response(
            {
                "ingreso_id": ingreso_id,
                "tipo_equipo_resuelto": protocol.get("display_name") or ingreso.get("tipo_equipo") or "",
                "template_key": template_key,
                "template_version": template_version,
                "schema": {
                    "references": references,
                    "sections": _protocol_sections_with_values(protocol, values),
                    "result_options": protocol.get("result_options") or [],
                    "global_result_options": protocol.get("global_result_options") or [],
                },
                "values": values,
                "resultado_global": (row or {}).get("resultado_global") or "pendiente",
                "conclusion": (row or {}).get("conclusion") or "",
                "instrumentos": instrumentos,
                "firmado_por": (row or {}).get("firmado_por") or "",
                "fecha_ejecucion": (row or {}).get("fecha_ejecucion"),
                "references_snapshot": references_snapshot,
                "applied_overrides": [] if schema_snapshot else (protocol.get("applied_overrides") or []),
            }
        )

    def patch(self, request, ingreso_id: int):
        require_permission(request, "action.ingreso.edit_diagnosis")
        if not _has_ingreso_tests_table():
            return Response({"detail": "Tabla ingreso_tests inexistente. Ejecuta schema."}, status=503)

        ingreso = _load_ingreso_context(ingreso_id)
        if not ingreso:
            return Response({"detail": "Ingreso no encontrado"}, status=404)

        _can_operate_on_ingreso(request, ingreso, for_edit=True)
        _set_audit_user(request)

        row = _load_test_row(ingreso_id)
        protocol_live = _resolve_protocol_for_row(ingreso, row)
        schema_snapshot = _extract_schema_snapshot(row)
        protocol = _protocol_from_schema_snapshot(schema_snapshot, protocol_live)
        if protocol is None:
            return Response(
                {"detail": "No hay protocolo de test para este tipo de equipo", "tipo_equipo": ingreso.get("tipo_equipo") or ""},
                status=409,
            )

        d = request.data or {}
        requested_template_key = _trim_text(d.get("template_key") if "template_key" in d else "", 100).lower()
        incoming_values = d.get("values") if "values" in d else None
        existing_values = _extract_values_from_payload((row or {}).get("payload"))
        protocol_requested = None
        if requested_template_key:
            protocol_requested = get_protocol_by_template_key(
                requested_template_key,
                marca=ingreso.get("marca") or "",
                modelo=ingreso.get("modelo") or "",
                numero_serie=ingreso.get("numero_serie") or "",
            )
            if protocol_requested is None:
                return Response({"detail": "template_key inválido o no disponible"}, status=400)

        if protocol_requested is not None:
            protocol = protocol_requested

        merged_values = _merge_values(existing_values, incoming_values, protocol)

        resultado_global = _trim_text(d.get("resultado_global") if "resultado_global" in d else (row or {}).get("resultado_global"), 50).lower()
        if resultado_global not in _VALID_GLOBAL_RESULTS:
            return Response({"detail": "resultado_global inválido"}, status=400)
        if not resultado_global:
            resultado_global = "pendiente"

        conclusion = _trim_text(d.get("conclusion") if "conclusion" in d else (row or {}).get("conclusion"), 2000)
        instrumentos = _trim_text(d.get("instrumentos") if "instrumentos" in d else (row or {}).get("instrumentos"), 2000)
        if not instrumentos:
            instrumentos = _default_instrumentos_for_protocol(protocol)
        firmado_por = _trim_text(d.get("firmado_por") if "firmado_por" in d else (row or {}).get("firmado_por"), 250)

        # Freeze references on first save to keep historical traceability stable.
        references_snapshot = _safe_json((row or {}).get("references_snapshot"), [])
        if not isinstance(references_snapshot, list):
            references_snapshot = []
        if not references_snapshot and schema_snapshot and isinstance(schema_snapshot.get("references"), list):
            references_snapshot = copy.deepcopy(schema_snapshot.get("references") or [])
        if protocol_requested is not None:
            references_snapshot = copy.deepcopy(protocol.get("references") or [])
        if not references_snapshot:
            references_snapshot = copy.deepcopy(protocol.get("references") or [])

        if resultado_global == "apto" and not references_snapshot:
            return Response(
                {"detail": "No se puede emitir 'Apto' sin referencias técnicas cargadas en el protocolo."},
                status=400,
            )

        if not schema_snapshot or protocol_requested is not None:
            source_protocol = protocol
            schema_snapshot = _build_schema_snapshot(source_protocol, references_snapshot)

        template_key = protocol.get("template_key") or schema_snapshot.get("template_key") or (row or {}).get("template_key") or ""
        template_version = (
            protocol.get("template_version")
            or schema_snapshot.get("template_version")
            or (row or {}).get("template_version")
            or ""
        )
        tipo_equipo_snapshot = (row or {}).get("tipo_equipo_snapshot") or (ingreso.get("tipo_equipo") or "")
        tecnico_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)

        _save_test_row(
            ingreso_id=ingreso_id,
            template_key=template_key,
            template_version=template_version,
            tipo_equipo_snapshot=tipo_equipo_snapshot,
            payload_doc={"values": merged_values},
            schema_snapshot=schema_snapshot,
            references_snapshot=references_snapshot,
            resultado_global=resultado_global,
            conclusion=conclusion,
            instrumentos=instrumentos,
            firmado_por=firmado_por,
            tecnico_id=tecnico_id,
        )

        return Response({"ok": True})


class IngresoTestPdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_any_permission(
            request,
            [
                "page.ingresos_history",
                "page.work_queues",
                "page.budget_queues",
                "page.logistics",
                "action.ingreso.edit_diagnosis",
            ],
        )
        if not _has_ingreso_tests_table():
            return Response({"detail": "Tabla ingreso_tests inexistente. Ejecuta schema."}, status=503)

        ingreso = _load_ingreso_context(ingreso_id)
        if not ingreso:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        _can_operate_on_ingreso(request, ingreso, for_edit=False)
        _set_audit_user(request)

        row = _load_test_row(ingreso_id)
        if not row:
            return Response({"detail": "No existe test guardado para este ingreso"}, status=404)

        protocol_live = _resolve_protocol_for_row(ingreso, row)
        schema_snapshot = _extract_schema_snapshot(row)
        protocol = _protocol_from_schema_snapshot(schema_snapshot, protocol_live)
        if protocol is None:
            return Response({"detail": "No se pudo resolver protocolo de test"}, status=409)

        references_snapshot = _safe_json(row.get("references_snapshot"), [])
        if not isinstance(references_snapshot, list):
            references_snapshot = []
        if not references_snapshot and schema_snapshot and isinstance(schema_snapshot.get("references"), list):
            references_snapshot = copy.deepcopy(schema_snapshot.get("references") or [])
        if not references_snapshot:
            return Response(
                {"detail": "No hay referencias congeladas para este test. Guarda el test antes de imprimir."},
                status=409,
            )

        values = _merge_values(_extract_values_from_payload(row.get("payload")), None, protocol)
        resultado_global = _trim_text(row.get("resultado_global"), 50).lower()
        if resultado_global == "apto" and not references_snapshot:
            return Response({"detail": "No se puede emitir 'Apto' sin referencias técnicas."}, status=409)

        report = {
            "ingreso_id": ingreso_id,
            "os": ingreso_id,
            "fecha_ejecucion": row.get("fecha_ejecucion") or timezone.now(),
            "cliente": ingreso.get("cliente") or "",
            "tipo_equipo": ingreso.get("tipo_equipo") or "",
            "marca": ingreso.get("marca") or "",
            "modelo": ingreso.get("modelo") or "",
            "numero_serie": ingreso.get("numero_serie") or "",
            "numero_interno": ingreso.get("numero_interno") or "",
            "template_key": row.get("template_key") or schema_snapshot.get("template_key") or protocol.get("template_key") or "",
            "template_version": row.get("template_version") or schema_snapshot.get("template_version") or protocol.get("template_version") or "",
            "resultado_global": resultado_global or "pendiente",
            "conclusion": row.get("conclusion") or "",
            "instrumentos": row.get("instrumentos") or _default_instrumentos_for_protocol(protocol),
            "firmado_por": row.get("firmado_por") or "",
            "references": references_snapshot,
            "sections": _protocol_sections_with_values(protocol, values),
        }
        pdf_bytes, fname = render_ingreso_test_pdf(report, printed_by=getattr(request.user, "nombre", ""))
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


__all__ = ["IngresoTestView", "IngresoTestPdfView"]
