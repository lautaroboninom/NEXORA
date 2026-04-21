from __future__ import annotations

import json

from django.db import IntegrityError, connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..test_protocol_catalog import normalize_protocol_document, safe_json_doc, serialize_protocol_row
from .helpers import _set_audit_user, exec_returning, exec_void, q, require_permission


def _has_protocol_catalog_table() -> bool:
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name='test_protocol_templates'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
            elif connection.vendor == "sqlite":
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_protocol_templates' LIMIT 1"
                )
            else:
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name='test_protocol_templates'
                     LIMIT 1
                    """
                )
            return cur.fetchone() is not None
    except Exception:
        return False


def _json_param(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_protocol_row(protocol_id: int) -> dict | None:
    return q(
        """
        SELECT id, type_key, template_key, active, doc, created_at, updated_at
          FROM test_protocol_templates
         WHERE id=%s
        """,
        [protocol_id],
        one=True,
    )


class TestProtocolCatalogView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "action.tests_protocol.manage")
        if not _has_protocol_catalog_table():
            return Response(
                {"detail": "Tabla test_protocol_templates inexistente. Ejecuta apply_test_schema."},
                status=503,
            )
        rows = q(
            """
            SELECT id, type_key, template_key, active, doc, created_at, updated_at
              FROM test_protocol_templates
             ORDER BY type_key ASC, id ASC
            """
        ) or []
        out = []
        for row in rows:
            item = serialize_protocol_row(row, detail=False)
            item["created_at"] = row.get("created_at")
            item["updated_at"] = row.get("updated_at")
            out.append(item)
        return Response(out)

    def post(self, request):
        require_permission(request, "action.tests_protocol.manage")
        if not _has_protocol_catalog_table():
            return Response(
                {"detail": "Tabla test_protocol_templates inexistente. Ejecuta apply_test_schema."},
                status=503,
            )
        try:
            normalized = normalize_protocol_document(request.data or {}, partial=False)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        _set_audit_user(request)
        raw_doc = _json_param(normalized)
        active = bool(normalized.get("active", True))
        try:
            if connection.vendor == "postgresql":
                new_id = exec_returning(
                    """
                    INSERT INTO test_protocol_templates(
                      type_key, template_key, active, doc, created_by, updated_by
                    ) VALUES (
                      %s, %s, %s, %s::jsonb,
                      current_setting('app.user_id', true)::INT,
                      current_setting('app.user_id', true)::INT
                    )
                    RETURNING id
                    """,
                    [normalized["type_key"], normalized["template_key"], active, raw_doc],
                )
            else:
                exec_void(
                    """
                    INSERT INTO test_protocol_templates(
                      type_key, template_key, active, doc, created_by, updated_by
                    ) VALUES (%s, %s, %s, %s, NULL, NULL)
                    """,
                    [normalized["type_key"], normalized["template_key"], int(active), raw_doc],
                )
                created = q(
                    """
                    SELECT id
                      FROM test_protocol_templates
                     WHERE type_key=%s
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    [normalized["type_key"]],
                    one=True,
                ) or {}
                new_id = created.get("id")
        except IntegrityError:
            return Response(
                {"detail": "type_key o template_key ya existente"},
                status=409,
            )
        except Exception as exc:
            low = str(exc).lower()
            if "unique" in low or "duplicate" in low:
                return Response(
                    {"detail": "type_key o template_key ya existente"},
                    status=409,
                )
            raise

        row = _load_protocol_row(int(new_id))
        if not row:
            return Response({"detail": "No se pudo recuperar protocolo creado"}, status=500)
        payload = serialize_protocol_row(row, detail=True)
        payload["created_at"] = row.get("created_at")
        payload["updated_at"] = row.get("updated_at")
        return Response(payload, status=201)


class TestProtocolDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, protocol_id: int):
        require_permission(request, "action.tests_protocol.manage")
        if not _has_protocol_catalog_table():
            return Response(
                {"detail": "Tabla test_protocol_templates inexistente. Ejecuta apply_test_schema."},
                status=503,
            )
        row = _load_protocol_row(protocol_id)
        if not row:
            return Response({"detail": "Protocolo no encontrado"}, status=404)
        payload = serialize_protocol_row(row, detail=True)
        payload["created_at"] = row.get("created_at")
        payload["updated_at"] = row.get("updated_at")
        return Response(payload)

    def patch(self, request, protocol_id: int):
        require_permission(request, "action.tests_protocol.manage")
        if not _has_protocol_catalog_table():
            return Response(
                {"detail": "Tabla test_protocol_templates inexistente. Ejecuta apply_test_schema."},
                status=503,
            )
        row = _load_protocol_row(protocol_id)
        if not row:
            return Response({"detail": "Protocolo no encontrado"}, status=404)
        existing_doc = safe_json_doc(row.get("doc"), {})
        if not isinstance(existing_doc, dict):
            existing_doc = {}
        if not existing_doc.get("type_key"):
            existing_doc["type_key"] = row.get("type_key")
        if not existing_doc.get("template_key"):
            existing_doc["template_key"] = row.get("template_key")
        existing_doc["active"] = bool(row.get("active", True))
        try:
            normalized = normalize_protocol_document(
                request.data or {},
                partial=True,
                existing=existing_doc,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        _set_audit_user(request)
        raw_doc = _json_param(normalized)
        active = bool(normalized.get("active", True))
        try:
            if connection.vendor == "postgresql":
                exec_void(
                    """
                    UPDATE test_protocol_templates
                       SET type_key=%s,
                           template_key=%s,
                           active=%s,
                           doc=%s::jsonb,
                           updated_by=current_setting('app.user_id', true)::INT,
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    [normalized["type_key"], normalized["template_key"], active, raw_doc, protocol_id],
                )
            else:
                exec_void(
                    """
                    UPDATE test_protocol_templates
                       SET type_key=%s,
                           template_key=%s,
                           active=%s,
                           doc=%s,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE id=%s
                    """,
                    [normalized["type_key"], normalized["template_key"], int(active), raw_doc, protocol_id],
                )
        except IntegrityError:
            return Response(
                {"detail": "type_key o template_key ya existente"},
                status=409,
            )
        except Exception as exc:
            low = str(exc).lower()
            if "unique" in low or "duplicate" in low:
                return Response(
                    {"detail": "type_key o template_key ya existente"},
                    status=409,
                )
            raise

        updated = _load_protocol_row(protocol_id)
        payload = serialize_protocol_row(updated, detail=True)
        payload["created_at"] = updated.get("created_at")
        payload["updated_at"] = updated.get("updated_at")
        return Response(payload)

    def delete(self, request, protocol_id: int):
        require_permission(request, "action.tests_protocol.manage")
        if not _has_protocol_catalog_table():
            return Response(
                {"detail": "Tabla test_protocol_templates inexistente. Ejecuta apply_test_schema."},
                status=503,
            )
        row = _load_protocol_row(protocol_id)
        if not row:
            return Response({"detail": "Protocolo no encontrado"}, status=404)

        _set_audit_user(request)
        if connection.vendor == "postgresql":
            exec_void(
                """
                UPDATE test_protocol_templates
                   SET active=FALSE,
                       updated_by=current_setting('app.user_id', true)::INT,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                [protocol_id],
            )
        else:
            exec_void(
                """
                UPDATE test_protocol_templates
                   SET active=0,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE id=%s
                """,
                [protocol_id],
            )
        return Response({"ok": True})


__all__ = ["TestProtocolCatalogView", "TestProtocolDetailView"]

