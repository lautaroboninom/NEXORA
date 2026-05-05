from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import exec_void, q, require_roles, _set_audit_user


class TiposEquipoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _norm_name(value: str) -> str:
        return " ".join(str(value or "").split()).strip()

    @classmethod
    def _ensure_catalog_tipo(cls, nombre: str) -> None:
        clean = cls._norm_name(nombre)
        if not clean:
            return
        row = q(
            """
            SELECT id
            FROM catalogo_tipos_equipo
            WHERE UPPER(TRIM(nombre)) = UPPER(TRIM(%s))
            LIMIT 1
            """,
            [clean],
            one=True,
        )
        if row:
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

        old_row = q(
            """
            SELECT id
            FROM catalogo_tipos_equipo
            WHERE UPPER(TRIM(nombre)) = UPPER(TRIM(%s))
            LIMIT 1
            """,
            [old_clean],
            one=True,
        )
        new_row = q(
            """
            SELECT id
            FROM catalogo_tipos_equipo
            WHERE UPPER(TRIM(nombre)) = UPPER(TRIM(%s))
            LIMIT 1
            """,
            [new_clean],
            one=True,
        )

        if old_row and new_row and old_row["id"] != new_row["id"]:
            exec_void("DELETE FROM catalogo_tipos_equipo WHERE id=%s", [old_row["id"]])
            exec_void("UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s", [new_clean, new_row["id"]])
            return
        if old_row and not new_row:
            exec_void("UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s", [new_clean, old_row["id"]])
            return
        if new_row:
            exec_void("UPDATE catalogo_tipos_equipo SET activo=TRUE WHERE id=%s", [new_row["id"]])
            return

        cls._ensure_catalog_tipo(new_clean)

    def get(self, request):
        try:
            rows = q(
                """
                SELECT DISTINCT TRIM(src.nombre) AS nombre
                FROM (
                    SELECT nombre
                    FROM catalogo_tipos_equipo
                    WHERE activo = TRUE
                    UNION
                    SELECT nombre
                    FROM marca_tipos_equipo
                    WHERE activo = TRUE
                    UNION
                    SELECT COALESCE(NULLIF(TRIM(tipo_equipo), ''), NULL) AS nombre
                    FROM models
                ) src
                WHERE src.nombre IS NOT NULL
                  AND NULLIF(TRIM(src.nombre), '') IS NOT NULL
                ORDER BY 1
                """
            ) or []
        except Exception:
            rows = q(
                """
                SELECT DISTINCT TRIM(nombre) AS nombre
                FROM marca_tipos_equipo
                WHERE activo = TRUE
                ORDER BY 1
                """
            ) or []

        out = []
        for idx, row in enumerate(rows, start=1):
            name = self._norm_name(row.get("nombre"))
            if name:
                out.append({"id": idx, "nombre": name})
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
            exec_void(
                """
                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                SELECT marca_id, %s, activo
                FROM marca_tipos_equipo
                WHERE UPPER(nombre)=UPPER(%s)
                ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                """,
                [new_name, old_name],
            )
            exec_void("DELETE FROM marca_tipos_equipo WHERE UPPER(nombre)=UPPER(%s)", [old_name])
            exec_void(
                "UPDATE models SET tipo_equipo=%s WHERE UPPER(TRIM(tipo_equipo))=UPPER(TRIM(%s))",
                [new_name, old_name],
            )
            return Response({"ok": True, "renamed": True})

        self._ensure_catalog_tipo(new_name)
        return Response({"ok": True, "created": True})

    def delete(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        nombre = self._norm_name(request.GET.get("nombre"))
        if not nombre:
            return Response({"detail": "nombre requerido"}, status=400)
        _set_audit_user(request)
        exec_void("DELETE FROM catalogo_tipos_equipo WHERE UPPER(TRIM(nombre))=UPPER(TRIM(%s))", [nombre])
        exec_void("DELETE FROM marca_tipos_equipo WHERE UPPER(TRIM(nombre))=UPPER(TRIM(%s))", [nombre])
        exec_void(
            "UPDATE models SET tipo_equipo=NULL WHERE UPPER(TRIM(tipo_equipo))=UPPER(TRIM(%s))",
            [nombre],
        )
        return Response({"ok": True})

