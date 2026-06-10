from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import exec_void, q, require_roles, _set_audit_user
from .tipo_equipo_utils import clean_tipo_equipo, matching_rows, preferred_name, preferred_row, tipo_equipo_key


class TiposEquipoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _norm_name(value: str) -> str:
        return clean_tipo_equipo(value)

    @classmethod
    def _ensure_catalog_tipo(cls, nombre: str) -> None:
        clean = cls._norm_name(nombre)
        if not clean:
            return
        rows = q("SELECT id, nombre, activo FROM catalogo_tipos_equipo ORDER BY id") or []
        row = preferred_row(rows, clean)
        if row:
            if clean_tipo_equipo(row.get("nombre")) != clean or not row.get("activo"):
                exec_void(
                    """
                    UPDATE catalogo_tipos_equipo
                       SET nombre=%s, activo=TRUE
                     WHERE id=%s
                    """,
                    [clean, row["id"]],
                )
            return
        exec_void(
            """
            INSERT INTO catalogo_tipos_equipo(nombre, activo)
            VALUES (%s, TRUE)
            """,
            [clean],
        )

    @classmethod
    def _rename_catalog_tipo(cls, old_name: str, new_name: str) -> None:
        old_clean = cls._norm_name(old_name)
        new_clean = cls._norm_name(new_name)
        if not old_clean or not new_clean:
            return

        rows = q("SELECT id, nombre, activo FROM catalogo_tipos_equipo ORDER BY id") or []
        old_rows = matching_rows(rows, old_clean)
        new_rows = matching_rows(rows, new_clean)
        target_row = preferred_row(new_rows or old_rows, new_clean if new_rows else old_clean)

        if target_row:
            exec_void(
                "UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s",
                [new_clean, target_row["id"]],
            )
            for row in old_rows + new_rows:
                if row["id"] != target_row["id"]:
                    exec_void("DELETE FROM catalogo_tipos_equipo WHERE id=%s", [row["id"]])
        else:
            cls._ensure_catalog_tipo(new_clean)

        try:
            marca_rows = q("SELECT id, marca_id, nombre, activo FROM marca_tipos_equipo ORDER BY id") or []
        except Exception:
            marca_rows = []
        for row in matching_rows(marca_rows, old_clean):
            exec_void(
                """
                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                VALUES (%s, %s, %s)
                ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                """,
                [row["marca_id"], new_clean, row.get("activo", True)],
            )
            exec_void("DELETE FROM marca_tipos_equipo WHERE id=%s", [row["id"]])

        try:
            model_rows = q("SELECT id, tipo_equipo FROM models ORDER BY id") or []
        except Exception:
            model_rows = []
        for row in matching_rows(model_rows, old_clean, field="tipo_equipo"):
            exec_void("UPDATE models SET tipo_equipo=%s WHERE id=%s", [new_clean, row["id"]])

    def get(self, request):
        rows = []
        sources = [
            ("SELECT nombre FROM catalogo_tipos_equipo WHERE activo = TRUE", 0),
            ("SELECT nombre FROM marca_tipos_equipo WHERE activo = TRUE", 1),
            ("SELECT COALESCE(NULLIF(TRIM(tipo_equipo), ''), NULL) AS nombre FROM models", 2),
        ]
        for sql, priority in sources:
            try:
                fetched = q(sql) or []
            except Exception:
                continue
            for row in fetched:
                rows.append({"nombre": row.get("nombre"), "priority": priority})

        grouped = preferred_name(rows, priority_field="priority")
        out = [{"id": idx, "nombre": name} for idx, name in enumerate(sorted(grouped.values(), key=tipo_equipo_key), start=1)]
        return Response(out)

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        new_name = self._norm_name(d.get("nombre"))
        old_name = self._norm_name(d.get("rename_from"))
        if not new_name:
            return Response({"detail": "nombre requerido"}, status=400)

        if old_name and old_name.lower() != new_name.lower():
            self._rename_catalog_tipo(old_name, new_name)
            return Response({"ok": True, "renamed": True})

        self._ensure_catalog_tipo(new_name)
        return Response({"ok": True, "created": True})

    def delete(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        nombre = self._norm_name(request.GET.get("nombre"))
        if not nombre:
            return Response({"detail": "nombre requerido"}, status=400)
        _set_audit_user(request)
        clean = self._norm_name(nombre)
        for row in q("SELECT id, nombre FROM catalogo_tipos_equipo ORDER BY id") or []:
            if tipo_equipo_key(row.get("nombre")) == tipo_equipo_key(clean):
                exec_void("DELETE FROM catalogo_tipos_equipo WHERE id=%s", [row["id"]])
        for row in q("SELECT id, nombre FROM marca_tipos_equipo ORDER BY id") or []:
            if tipo_equipo_key(row.get("nombre")) == tipo_equipo_key(clean):
                exec_void("DELETE FROM marca_tipos_equipo WHERE id=%s", [row["id"]])
        for row in q("SELECT id, tipo_equipo FROM models ORDER BY id") or []:
            if tipo_equipo_key(row.get("tipo_equipo")) == tipo_equipo_key(clean):
                exec_void("UPDATE models SET tipo_equipo=NULL WHERE id=%s", [row["id"]])
        return Response({"ok": True})
