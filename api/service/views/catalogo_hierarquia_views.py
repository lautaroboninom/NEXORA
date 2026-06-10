from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import exec_void, exec_returning, q, require_roles, _set_audit_user
from .tipo_equipo_utils import clean_tipo_equipo, matching_rows, preferred_row


def _ensure_marca_tipo_id(marca_id: int, nombre: str, activo: bool = True):
    clean = clean_tipo_equipo(nombre)
    if not clean:
        return None
    rows = q(
        "SELECT id, nombre, activo FROM marca_tipos_equipo WHERE marca_id=%s ORDER BY id",
        [marca_id],
    ) or []
    row = preferred_row(rows, clean)
    if row:
        if clean_tipo_equipo(row.get("nombre")) != clean or bool(row.get("activo")) != bool(activo):
            exec_void("UPDATE marca_tipos_equipo SET nombre=%s, activo=%s WHERE id=%s", [clean, bool(activo), row["id"]])
        return row["id"]
    if connection.vendor == "postgresql":
        return exec_returning(
            "INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,%s) RETURNING id",
            [marca_id, clean, bool(activo)],
        )
    exec_void("INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,%s)", [marca_id, clean, bool(activo)])
    row = q(
        """
        SELECT id
          FROM marca_tipos_equipo
         WHERE marca_id=%s AND LOWER(TRIM(nombre))=LOWER(TRIM(%s))
         ORDER BY id DESC
         LIMIT 1
        """,
        [marca_id, clean],
        one=True,
    )
    return row["id"] if row else None


def _ensure_catalog_tipo(nombre: str) -> None:
    clean = clean_tipo_equipo(nombre)
    if not clean:
        return
    rows = q("SELECT id, nombre, activo FROM catalogo_tipos_equipo ORDER BY id") or []
    row = preferred_row(rows, clean)
    if row:
        if clean_tipo_equipo(row.get("nombre")) != clean or not row.get("activo"):
            exec_void("UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s", [clean, row["id"]])
        return
    exec_void("INSERT INTO catalogo_tipos_equipo(nombre, activo) VALUES (%s, TRUE)", [clean])


class CatalogoTiposView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _canon(value: str) -> str:
        if not value:
            return ""
        return " ".join(str(value).strip().split()).upper()

    def get(self, request, bid: int):
        try:
            marca_id = int(bid)
        except (TypeError, ValueError):
            return Response({"detail": "parametros invalidos"}, status=400)

        rows = q(
                """
                SELECT id, nombre, activo
                FROM marca_tipos_equipo
                WHERE marca_id=%s
                ORDER BY nombre
                """,
                [marca_id],
            ) or []

        data = []
        for row in rows:
            data.append({
                "id": row.get("id"),
                "name": row.get("nombre"),
                "label": self._canon(row.get("nombre")),
                "active": bool(row.get("activo")),
            })

        return Response(data)


class CatalogoModelosDeTipoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _canon(value: str) -> str:
        if not value:
            return ""
        return " ".join(str(value).strip().split()).upper()

    def get(self, request, bid: int, tid: int):
        try:
            marca_id = int(bid)
            tipo_id = int(tid)
        except (TypeError, ValueError):
            return Response({"detail": "parametros invalidos"}, status=400)

        rows = q(
            """
            SELECT id, nombre, alias, activo
            FROM marca_series
            WHERE marca_id=%s AND tipo_id=%s
            ORDER BY nombre
            """,
            [marca_id, tipo_id],
        ) or []

        data = []
        for row in rows:
            data.append({
                "id": row.get("id"),
                "name": row.get("nombre"),
                "label": self._canon(row.get("nombre")),
                "alias": row.get("alias") or "",
                "active": bool(row.get("activo")),
            })

        return Response(data)


class CatalogoVariantesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _canon(value: str) -> str:
        if not value:
            return ""
        return " ".join(str(value).strip().split()).upper()

    def get(self, request, bid: int, mid: int):
        try:
            marca_id = int(bid)
            modelo_id = int(mid)
        except (TypeError, ValueError):
            return Response({"detail": "parametros invalidos"}, status=400)

        modelo_row = q(
            "SELECT tipo_id FROM marca_series WHERE id=%s AND marca_id=%s",
            [modelo_id, marca_id],
            one=True,
        )
        if not modelo_row:
            return Response([])

        tipo_id = modelo_row.get("tipo_id")
        variantes = q(
            """
            SELECT id, nombre, activo
            FROM marca_series_variantes
            WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s
            ORDER BY nombre
            """,
            [marca_id, tipo_id, modelo_id],
        ) or []

        data = []
        for row in variantes:
            data.append({
                "id": row.get("id"),
                "name": row.get("nombre"),
                "label": self._canon(row.get("nombre")),
                "active": bool(row.get("activo")),
            })

        return Response(data)


class CatalogoMarcasPorTipoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, tipo_nombre: str):
        nombre = (tipo_nombre or "").strip()
        if not nombre:
            return Response([])
        rows = q(
            """
            SELECT DISTINCT m.id, m.nombre
            FROM marcas m
            JOIN marca_tipos_equipo t ON t.marca_id = m.id
            WHERE UPPER(TRIM(t.nombre)) = UPPER(TRIM(%s))
            ORDER BY m.nombre
            """,
            [nombre],
        ) or []
        return Response(rows)


class CatalogoTiposCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        try:
            marca_id = int(d.get("marca_id"))
        except (TypeError, ValueError):
            return Response({"detail": "marca_id requerido"}, status=400)
        nombre = (d.get("name") or d.get("nombre") or "").strip()
        if not nombre:
            return Response({"detail": "name requerido"}, status=400)
        active = d.get("active")
        activo_val = bool(active) if active is not None else True

        existing = preferred_row(
            q("SELECT id, nombre, activo FROM marca_tipos_equipo WHERE marca_id=%s ORDER BY id", [marca_id]) or [],
            nombre,
        )
        new_id = _ensure_marca_tipo_id(marca_id, nombre, activo_val)
        if not new_id:
            return Response({"detail": "No se pudo crear o actualizar el tipo"}, status=500)
        return Response({
            "ok": True,
            "id": new_id,
            "updated": bool(existing),
            "created": not bool(existing),
        })


class CatalogoTipoDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, tipo_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        row = q("SELECT id, marca_id, nombre, activo FROM marca_tipos_equipo WHERE id=%s", [tipo_id], one=True)
        if not row:
            return Response({"detail": "tipo no encontrado"}, status=404)

        d = request.data or {}
        nombre = d.get("name") if "name" in d else d.get("nombre") if "nombre" in d else None
        nombre = (nombre or "").strip() if nombre is not None else None
        active = d.get("active") if "active" in d else None

        sets = []
        params = []
        if nombre is not None and nombre != row.get("nombre"):
            rows = q("SELECT id, nombre FROM marca_tipos_equipo WHERE marca_id=%s ORDER BY id", [row["marca_id"]]) or []
            clash = [r for r in matching_rows(rows, nombre) if r["id"] != tipo_id]
            if clash:
                return Response({"detail": "ya existe un tipo con ese nombre"}, status=409)
            sets.append("nombre=%s")
            params.append(clean_tipo_equipo(nombre))
        if active is not None:
            sets.append("activo=%s")
            params.append(bool(active))

        if not sets:
            return Response({"ok": True})

        params.append(tipo_id)
        exec_void(f"UPDATE marca_tipos_equipo SET {', '.join(sets)} WHERE id=%s", params)
        return Response({"ok": True})


class CatalogoModelosCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        try:
            marca_id = int(d.get("marca_id"))
            tipo_id = int(d.get("tipo_id"))
        except (TypeError, ValueError):
            return Response({"detail": "marca_id y tipo_id requeridos"}, status=400)
        nombre = (d.get("name") or d.get("nombre") or "").strip()
        alias = d.get("alias")
        alias = (alias or "").strip()
        alias = alias if alias else None
        active = d.get("active")
        activo_val = bool(active) if active is not None else True

        existing = q(
            "SELECT id FROM marca_series WHERE marca_id=%s AND tipo_id=%s AND UPPER(nombre)=UPPER(%s)",
            [marca_id, tipo_id, nombre], one=True,
        )
        if existing:
            sets = []
            params = []
            if alias is not None:
                sets.append("alias=%s")
                params.append(alias)
            if active is not None:
                sets.append("activo=%s")
                params.append(activo_val)
            if sets:
                params.append(existing["id"])
                exec_void(f"UPDATE marca_series SET {', '.join(sets)} WHERE id=%s", params)
            return Response({"ok": True, "id": existing["id"], "updated": False})
        new_id = exec_returning(
            "INSERT INTO marca_series(marca_id, tipo_id, nombre, alias, activo) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            [marca_id, tipo_id, nombre, alias, activo_val],
        )
        return Response({"ok": True, "id": new_id, "created": True})


class CatalogoModeloDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, serie_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        row = q("SELECT id, marca_id, tipo_id, nombre, alias, activo FROM marca_series WHERE id=%s", [serie_id], one=True)
        if not row:
            return Response({"detail": "serie no encontrada"}, status=404)
        d = request.data or {}
        nombre = d.get("name") if "name" in d else d.get("nombre") if "nombre" in d else None
        nombre = (nombre or "").strip() if nombre is not None else None
        alias = d.get("alias") if "alias" in d else None
        alias = (alias or "").strip() if alias is not None else None
        active = d.get("active") if "active" in d else None

        sets = []
        params = []
        if nombre is not None and nombre != row.get("nombre"):
            clash = q(
                "SELECT id FROM marca_series WHERE marca_id=%s AND tipo_id=%s AND id<>%s AND UPPER(nombre)=UPPER(%s)",
                [row["marca_id"], row["tipo_id"], serie_id, nombre], one=True,
            )
            if clash:
                return Response({"detail": "ya existe una serie con ese nombre"}, status=409)
            sets.append("nombre=%s")
            params.append(nombre)
        if alias is not None:
            sets.append("alias=%s")
            params.append(alias if alias else None)
        if active is not None:
            sets.append("activo=%s")
            params.append(bool(active))

        if not sets:
            return Response({"ok": True})

        params.append(serie_id)
        exec_void(f"UPDATE marca_series SET {', '.join(sets)} WHERE id=%s", params)
        return Response({"ok": True})


class CatalogoVariantesCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        try:
            marca_id = int(d.get("marca_id"))
            tipo_id = int(d.get("tipo_id"))
            serie_id = int(d.get("serie_id"))
        except (TypeError, ValueError):
            return Response({"detail": "marca_id, tipo_id y serie_id requeridos"}, status=400)
        nombre = (d.get("name") or d.get("nombre") or "").strip()
        if not nombre:
            return Response({"detail": "name requerido"}, status=400)
        active = d.get("active")
        activo_val = bool(active) if active is not None else True
        backfill_first = bool(d.get("backfill_ingresos_if_first") or False)
        model_hint_id = None
        try:
            if d.get("model_id") not in (None, "", "null"):
                model_hint_id = int(d.get("model_id"))
        except Exception:
            model_hint_id = None

        # Detectar si es la primera variante de la serie
        pre = q(
            "SELECT COUNT(*) AS cnt FROM marca_series_variantes WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s",
            [marca_id, tipo_id, serie_id],
            one=True,
        ) or {"cnt": 0}
        pre_count = int(pre.get("cnt") or 0)

        existing = q(
            """
            SELECT id FROM marca_series_variantes
            WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s AND UPPER(nombre)=UPPER(%s)
            """,
            [marca_id, tipo_id, serie_id, nombre], one=True,
        )
        if existing:
            if active is not None:
                exec_void("UPDATE marca_series_variantes SET activo=%s WHERE id=%s", [activo_val, existing["id"]])
            return Response({"ok": True, "id": existing["id"], "updated": False})
        new_id = exec_returning(
            """
            INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
            VALUES (%s,%s,%s,%s,%s)
            RETURNING id
            """,
            [marca_id, tipo_id, serie_id, nombre, activo_val],
        )
        # Backfill opcional: solo si es la primera variante creada y el caller lo pide
        backfilled = 0
        model_variant_set = False
        if backfill_first and pre_count == 0:
            # Resolver el model_id asociado. Preferimos el hint si coincide con la marca
            model_id_to_use = None
            if model_hint_id:
                mm = q("SELECT id, marca_id, nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo FROM models WHERE id=%s", [model_hint_id], one=True)
                if mm and int(mm.get("marca_id")) == marca_id:
                    model_id_to_use = int(mm.get("id"))
            if not model_id_to_use:
                # Mapear por nombre de serie y tipo
                serie = q(
                    """
                    SELECT ms.nombre AS serie_nombre, mt.nombre AS tipo_nombre
                      FROM marca_series ms
                      JOIN marca_tipos_equipo mt ON mt.id = ms.tipo_id
                     WHERE ms.id = %s AND ms.marca_id = %s AND ms.tipo_id = %s
                    """,
                    [serie_id, marca_id, tipo_id],
                    one=True,
                ) or {}
                serie_nombre = (serie.get("serie_nombre") or "").strip()
                tipo_nombre = (serie.get("tipo_nombre") or "").strip()
                if serie_nombre:
                    cand = q(
                        """
                        SELECT id FROM models
                         WHERE marca_id=%s
                           AND UPPER(TRIM(nombre)) = UPPER(TRIM(%s))
                           AND (COALESCE(TRIM(tipo_equipo),'') = '' OR UPPER(TRIM(tipo_equipo)) = UPPER(TRIM(%s)))
                         LIMIT 1
                        """,
                        [marca_id, serie_nombre, tipo_nombre],
                        one=True,
                    ) or {}
                    if cand.get("id"):
                        model_id_to_use = int(cand.get("id"))
            if model_id_to_use:
                # Rellenar ingresos.equipo_variante cuando esté vacío, para todos los ingresos del modelo
                exec_void(
                    """
                    UPDATE ingresos i
                       SET equipo_variante = %s
                      FROM devices d
                     WHERE i.device_id = d.id
                       AND d.model_id = %s
                       AND (i.equipo_variante IS NULL OR i.equipo_variante = '')
                    """,
                    [nombre, model_id_to_use],
                )
                # Medimos cuántos ingresos quedaron con esa variante para información (aprox)
                row = q(
                    "SELECT COUNT(*) AS cnt FROM ingresos i JOIN devices d ON i.device_id=d.id WHERE d.model_id=%s AND i.equipo_variante=%s",
                    [model_id_to_use, nombre],
                    one=True,
                ) or {"cnt": 0}
                backfilled = int(row.get("cnt") or 0)
                # También actualizar models.variante si está vacío
                exec_void(
                    "UPDATE models SET variante=%s WHERE id=%s AND (variante IS NULL OR variante='')",
                    [nombre, model_id_to_use],
                )
                # Chequear si quedó seteado
                mv = q("SELECT COALESCE(TRIM(variante),'') AS v FROM models WHERE id=%s", [model_id_to_use], one=True) or {}
                model_variant_set = ((mv.get("v") or "") == nombre)

        return Response({"ok": True, "id": new_id, "created": True, "backfilled": backfilled, "model_variant_set": model_variant_set})


class CatalogoVarianteDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, variante_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        row = q(
            "SELECT id, marca_id, tipo_id, serie_id, nombre, activo FROM marca_series_variantes WHERE id=%s",
            [variante_id], one=True,
        )
        if not row:
            return Response({"detail": "variante no encontrada"}, status=404)
        d = request.data or {}
        nombre = d.get("name") if "name" in d else d.get("nombre") if "nombre" in d else None
        nombre = (nombre or "").strip() if nombre is not None else None
        active = d.get("active") if "active" in d else None

        sets = []
        params = []
        if nombre is not None and nombre != row.get("nombre"):
            clash = q(
                """
                SELECT id FROM marca_series_variantes
                WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s AND id<>%s AND UPPER(nombre)=UPPER(%s)
                """,
                [row["marca_id"], row["tipo_id"], row["serie_id"], variante_id, nombre], one=True,
            )
            if clash:
                return Response({"detail": "ya existe una variante con ese nombre"}, status=409)
            sets.append("nombre=%s")
            params.append(nombre)
        if active is not None:
            sets.append("activo=%s")
            params.append(bool(active))

        if not sets:
            return Response({"ok": True})

        params.append(variante_id)
        exec_void(f"UPDATE marca_series_variantes SET {', '.join(sets)} WHERE id=%s", params)
        return Response({"ok": True})

    def delete(self, request, variante_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        _set_audit_user(request)
        exec_void("DELETE FROM marca_series_variantes WHERE id=%s", [variante_id])
        return Response({"ok": True})


class ModeloTipoEquipoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, marca_id: int, modelo_id: int):
        _set_audit_user(request)
        d = request.data or {}
        tipo_nombre = (d.get("tipo_equipo") or "").strip()
        tipo_id = d.get("tipo_equipo_id")

        if tipo_id is not None and not tipo_nombre:
            r = q('SELECT "Equipo" AS equipo FROM equipos WHERE "IdEquipos"=%s', [tipo_id], one=True)
            if not r:
                return Response({"detail": "Tipo de equipo inexistente"}, status=400)
            tipo_nombre = r["equipo"]

        tipo_nombre = tipo_nombre or ""

        exec_void("""
            UPDATE models
               SET tipo_equipo = NULLIF(%s,'')
             WHERE id=%s AND marca_id=%s
        """, [tipo_nombre, modelo_id, marca_id])

        if tipo_nombre:
            _ensure_marca_tipo_id(marca_id, tipo_nombre, activo=True)
            _ensure_catalog_tipo(tipo_nombre)

        return Response({"ok": True})


__all__ = [
    'CatalogoTiposView',
    'CatalogoModelosDeTipoView',
    'CatalogoVariantesView',
    'CatalogoMarcasPorTipoView',
    'CatalogoTiposCreateView',
    'CatalogoTipoDetailView',
    'CatalogoModelosCreateView',
    'CatalogoModeloDetailView',
    'CatalogoVariantesCreateView',
    'CatalogoVarianteDetailView',
    'ModeloTipoEquipoView',
]
