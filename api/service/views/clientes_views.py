from django.db import transaction
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ..permissions import require_any_permission
from .helpers import exec_void, q, require_roles, _set_audit_user


class CustomersListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(
            request,
            [
                "page.home_search",
                "action.ingreso.edit_basics",
                "action.ingreso.create",
                "page.new_ingreso",
                "action.delivery_order.create",
            ],
        )
        return Response(q("SELECT id, razon_social FROM customers ORDER BY razon_social;"))


class ClientesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin","jefe_veedor", "recepcion"])
        return Response(q("SELECT * FROM customers ORDER BY razon_social"))

    def post(self, request):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        if not (d.get("razon_social") and d.get("cod_empresa")):
            raise ValidationError("razon_social y cod_empresa son requeridos")
        exec_void(
            """INSERT INTO customers(razon_social, cod_empresa, telefono, telefono_2, email)
             VALUES (%(rs)s, %(ce)s, %(tel)s, %(tel2)s, %(email)s)""",
            {"rs": d["razon_social"], "ce": d["cod_empresa"], "tel": d.get("telefono"), "tel2": d.get("telefono_2"), "email": d.get("email")},
        )
        return Response({"ok": True})


class ClienteDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, cid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        rs = (d.get("razon_social") or "").strip()
        ce = (d.get("cod_empresa") or "").strip()
        if not (rs and ce):
            raise ValidationError("razon_social y cod_empresa son requeridos")
        try:
            exec_void(
                """
                UPDATE customers
                   SET razon_social = %(rs)s,
                       cod_empresa  = %(ce)s,
                       telefono     = %(tel)s,
                       telefono_2   = %(tel2)s,
                       email        = %(email)s
                 WHERE id = %(id)s
                """,
                {
                    "id": cid,
                    "rs": rs,
                    "ce": ce,
                    "tel": d.get("telefono"),
                    "tel2": d.get("telefono_2"),
                    "email": d.get("email"),
                },
            )
            return Response({"ok": True})
        except Exception as e:
            raise ValidationError(str(e) or "No se pudo actualizar el cliente")

    def delete(self, request, cid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        refs = q(
            """
            SELECT
              (SELECT COUNT(*) FROM devices d WHERE d.customer_id = %s) AS cnt_devices,
              (SELECT COUNT(*)
                 FROM ingresos t
                 JOIN devices d ON d.id = t.device_id
                WHERE d.customer_id = %s) AS cnt_ingresos
            """,
            [cid, cid], one=True
        ) or {"cnt_devices": 0, "cnt_ingresos": 0}
        if refs["cnt_devices"] or refs["cnt_ingresos"]:
            return Response(
                {"detail": f"No se puede eliminar: el cliente tiene {refs['cnt_devices']} equipos y {refs['cnt_ingresos']} ingresos asociados."},
                status=409
            )
        try:
            _set_audit_user(request)
            exec_void("DELETE FROM customers WHERE id = %(id)s", {"id": cid})
            return Response({"ok": True})
        except Exception:
            return Response({"detail": "No se pudo eliminar por restricciones de integridad."}, status=409)


class ClienteMergeView(APIView):
    """Mover referencias de un cliente duplicado a otro y eliminar el source."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        try:
            source_id = int(d.get("source_id"))
            target_id = int(d.get("target_id"))
        except Exception:
            return Response({"detail": "source_id y target_id requeridos"}, status=400)
        if source_id == target_id:
            return Response({"detail": "source y target no pueden ser iguales"}, status=400)

        src = q(
            "SELECT id, razon_social, cod_empresa, telefono, telefono_2, email FROM customers WHERE id=%s",
            [source_id],
            one=True,
        )
        dst = q(
            "SELECT id, razon_social, cod_empresa, telefono, telefono_2, email FROM customers WHERE id=%s",
            [target_id],
            one=True,
        )
        if not src or not dst:
            return Response({"detail": "cliente source/target inexistente"}, status=404)

        # Completar campos faltantes del destino con los del source (sin tocar razon_social)
        def _merge_field(dst_val, src_val):
            dst_clean = (dst_val or "").strip()
            src_clean = (src_val or "").strip()
            return dst_clean or src_clean or None

        updated_target = {
            "cod_empresa": _merge_field(dst.get("cod_empresa"), src.get("cod_empresa")),
            "telefono": _merge_field(dst.get("telefono"), src.get("telefono")),
            "telefono_2": _merge_field(dst.get("telefono_2"), src.get("telefono_2")),
            "email": _merge_field(dst.get("email"), src.get("email")),
        }

        moved_devices = q(
            "SELECT COUNT(*) AS cnt FROM devices WHERE customer_id=%s",
            [source_id],
            one=True,
        ) or {"cnt": 0}

        with transaction.atomic():
            exec_void(
                """
                UPDATE customers
                   SET cod_empresa = %(cod)s,
                       telefono    = %(tel)s,
                       telefono_2  = %(tel2)s,
                       email       = %(email)s
                 WHERE id = %(id)s
                """,
                {
                    "cod": updated_target["cod_empresa"],
                    "tel": updated_target["telefono"],
                    "tel2": updated_target["telefono_2"],
                    "email": updated_target["email"],
                    "id": target_id,
                },
            )
            exec_void(
                "UPDATE devices SET customer_id=%(target)s WHERE customer_id=%(source)s",
                {"target": target_id, "source": source_id},
            )
            exec_void(
                "DELETE FROM customers WHERE id=%(id)s",
                {"id": source_id},
            )

        return Response(
            {
                "ok": True,
                "source_id": source_id,
                "target_id": target_id,
                "moved_devices": int(moved_devices.get("cnt") or 0),
            }
        )


__all__ = [
    'CustomersListView',
    'ClientesView',
    'ClienteDeleteView',
    'ClienteMergeView',
]
