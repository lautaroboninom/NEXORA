from django.db import connection
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import _fetchall_dicts, _set_audit_user, exec_void, exec_returning, q, require_roles
from ..derivations import attach_active_derivations
from ..serializers import IngresoListItemSerializer


class CatalogoAccesoriosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rows = q("SELECT id, nombre FROM catalogo_accesorios WHERE activo ORDER BY nombre")
        return Response(rows)

    def post(self, request):
        # Alta y renombrado: solo jefes/admin/jefe_veedor
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        d = request.data or {}
        new_name = (d.get("nombre") or "").strip()
        old_name = (d.get("rename_from") or "").strip()
        if not new_name:
            return Response({"detail": "nombre requerido"}, status=400)
        _set_audit_user(request)

        # Renombrar existente (case-insensitive)
        if old_name and old_name.lower() != new_name.lower():
            # Evitar colisiones por nombre (case-insensitive)
            exists = q(
                "SELECT id FROM catalogo_accesorios WHERE LOWER(TRIM(nombre))=LOWER(TRIM(%s))",
                [new_name],
                one=True,
            )
            if exists:
                return Response({"detail": "ya existe"}, status=400)

            exec_void(
                "UPDATE catalogo_accesorios SET nombre=%s WHERE LOWER(TRIM(nombre))=LOWER(TRIM(%s))",
                [new_name, old_name],
            )
            return Response({"ok": True, "renamed": True})

        # Alta o reactivar si ya existe (case-insensitive)
        row = q(
            "SELECT id FROM catalogo_accesorios WHERE LOWER(TRIM(nombre))=LOWER(TRIM(%s))",
            [new_name],
            one=True,
        )
        if row:
            exec_void(
                "UPDATE catalogo_accesorios SET nombre=%s, activo=TRUE WHERE id=%s",
                [new_name, row.get("id")],
            )
            return Response({"ok": True, "reactivated": True})

        exec_void(
            """
            INSERT INTO catalogo_accesorios(nombre, activo)
            VALUES (%s, TRUE)
            ON CONFLICT (nombre) DO UPDATE SET activo=EXCLUDED.activo
            """,
            [new_name],
        )
        return Response({"ok": True, "created": True})

    def delete(self, request):
        # Baja lógica para no romper historiales que referencian el accesorio
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        nombre = (request.GET.get("nombre") or "").strip()
        if not nombre:
            return Response({"detail": "nombre requerido"}, status=400)
        _set_audit_user(request)
        exec_void(
            "UPDATE catalogo_accesorios SET activo=FALSE WHERE LOWER(TRIM(nombre))=LOWER(TRIM(%s))",
            [nombre],
        )
        return Response({"ok": True})


class IngresoAccesoriosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe","admin","jefe_veedor","tecnico","recepcion"])
        rows = q(
            """
              SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
              FROM ingreso_accesorios ia
              JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id AND ca.activo
              WHERE ia.ingreso_id=%s
              ORDER BY ia.id
            """,
            [ingreso_id]
        )
        return Response(rows)

    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe","admin","jefe_veedor","tecnico","recepcion"])
        d = request.data or {}
        # Asumimos catálogo disponible (PostgreSQL-only)
        try:
            acc_id = int(d.get("accesorio_id"))
        except (TypeError, ValueError):
            return Response({"detail": "accesorio_id requerido"}, status=400)
        acc = q("SELECT id FROM catalogo_accesorios WHERE id=%s AND activo", [acc_id], one=True)
        if not acc:
            return Response({"detail": "accesorio inválido"}, status=400)
        ref = (d.get("referencia") or "").strip() or None
        desc = (d.get("descripcion") or "").strip() or None
        _set_audit_user(request)
        new_id = exec_returning(
            """
            INSERT INTO ingreso_accesorios(ingreso_id, accesorio_id, referencia, descripcion)
            VALUES (%s,%s,%s,%s)
            RETURNING id
            """,
            [ingreso_id, acc_id, ref, desc]
        )
        row = q(
            """
              SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
              FROM ingreso_accesorios ia
              JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id
              WHERE ia.id=%s
            """,
            [new_id], one=True
        )
        return Response(row, status=201)


class IngresoAccesorioDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, ingreso_id: int, item_id: int):
        require_roles(request, ["jefe","admin","jefe_veedor","tecnico","recepcion"])
        _set_audit_user(request)
        exec_void(
            "DELETE FROM ingreso_accesorios WHERE ingreso_id=%s AND id=%s",
            [ingreso_id, item_id]
        )
        return Response({"ok": True})


class BuscarAccesorioPorReferenciaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        like = "%" + (request.GET.get("ref") or "").strip() + "%"
        if not like or like == "%%":
            return Response([])
        # El dueño va por el helper y no escrito a mano: `propietario_nombre` es una columna
        # de compat y sin la guarda de esquema este endpoint tira 500 en una base donde el
        # bloque de `sql/schema.sql` no corrió, mientras el resto degrada limpio.
        from .ingresos_views import _owner_list_select_sql

        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  t.id,
                  t.estado AS estado,
                  t.presupuesto_estado,
                  t.motivo,
                  c.razon_social,
                  {_owner_list_select_sql()}
                  b.nombre AS marca,
                  m.nombre AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  d.numero_serie,
                  t.fecha_ingreso,
                  ia.referencia,
                  COALESCE(ca.nombre,'') AS accesorio_nombre
                FROM ingreso_accesorios ia
                JOIN ingresos t ON t.id = ia.ingreso_id
                JOIN devices  d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id
                WHERE (LOWER(ia.referencia) LIKE LOWER(%s))
                ORDER BY t.fecha_ingreso DESC, t.id DESC;
            """,
                [like])
            rows = _fetchall_dicts(cur)
        attach_active_derivations(rows)
        return Response(IngresoListItemSerializer(rows, many=True).data)


class IngresoAlquilerAccesoriosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        # Ver: todos los usuarios autenticados
        rows = q(
            """
              SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
              FROM ingreso_alquiler_accesorios ia
              JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id AND ca.activo
              WHERE ia.ingreso_id=%s
              ORDER BY ia.id
            """,
            [ingreso_id]
        )
        return Response(rows)

    def post(self, request, ingreso_id: int):
        # Modificar: solo admin/jefe/jefe_veedor
        require_roles(request, ["jefe","admin","jefe_veedor"])
        d = request.data or {}
        try:
            acc_id = int(d.get("accesorio_id"))
        except (TypeError, ValueError):
            return Response({"detail": "accesorio_id requerido"}, status=400)
        acc = q("SELECT id FROM catalogo_accesorios WHERE id=%s AND activo", [acc_id], one=True)
        if not acc:
            return Response({"detail": "accesorio inválido"}, status=400)
        ref = (d.get("referencia") or "").strip() or None
        desc = (d.get("descripcion") or "").strip() or None
        _set_audit_user(request)
        new_id = exec_returning(
            """
            INSERT INTO ingreso_alquiler_accesorios(ingreso_id, accesorio_id, referencia, descripcion)
            VALUES (%s,%s,%s,%s)
            RETURNING id
            """,
            [ingreso_id, acc_id, ref, desc]
        )
        row = q(
            """
              SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
              FROM ingreso_alquiler_accesorios ia
              JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id
              WHERE ia.id=%s
            """,
            [new_id], one=True
        )
        return Response(row, status=201)


class IngresoAlquilerAccesorioDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, ingreso_id: int, item_id: int):
        # Modificar: solo admin/jefe/jefe_veedor
        require_roles(request, ["jefe","admin","jefe_veedor"])
        _set_audit_user(request)
        exec_void(
            "DELETE FROM ingreso_alquiler_accesorios WHERE ingreso_id=%s AND id=%s",
            [ingreso_id, item_id]
        )
        return Response({"ok": True})


__all__ = [
    'CatalogoAccesoriosView',
    'IngresoAccesoriosView',
    'IngresoAccesorioDetailView',
    'BuscarAccesorioPorReferenciaView',
    'IngresoAlquilerAccesoriosView',
    'IngresoAlquilerAccesorioDetailView',
]
