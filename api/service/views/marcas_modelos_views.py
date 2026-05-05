from django.db import IntegrityError, connection, transaction
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import (
    ensure_default_locations,
    q,
    exec_void,
    exec_returning,
    _set_audit_user,
    require_roles,
)


class CatalogoMarcasView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rows = q(
            """
            SELECT b.id, b.nombre,
                b.tecnico_id,
                COALESCE(u.nombre,'') AS tecnico_nombre
            FROM marcas b
            LEFT JOIN users u ON u.id = b.tecnico_id
            ORDER BY b.nombre
            """
        )
        return Response(rows)

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        data = request.data or {}
        nombre = (data.get("nombre") or "").strip()
        if not nombre:
            raise ValidationError("nombre requerido")

        tecnico_raw = data.get("tecnico_id")
        tecnico_id = None
        if tecnico_raw not in (None, "", "null"):
            try:
                tecnico_id = int(tecnico_raw)
            except (TypeError, ValueError):
                raise ValidationError("tecnico_id inválido")

        existing = q(
            "SELECT id FROM marcas WHERE LOWER(nombre)=LOWER(%s)",
            [nombre],
            one=True,
        )

        _set_audit_user(request)

        if existing:
            sets = ["nombre=%s"]
            params = [nombre]
            if "tecnico_id" in data:
                if tecnico_id is None:
                    sets.append("tecnico_id=NULL")
                else:
                    sets.append("tecnico_id=%s")
                    params.append(tecnico_id)
            params.append(existing["id"])
            exec_void(
                f"UPDATE marcas SET {', '.join(sets)} WHERE id=%s",
                params,
            )
            return Response({"ok": True, "id": existing["id"], "updated": True})

        cols = ["nombre"]
        placeholders = ["%s"]
        params = [nombre]
        if tecnico_id is not None:
            cols.append("tecnico_id")
            placeholders.append("%s")
            params.append(tecnico_id)
        try:
            mid = exec_returning(
                f"INSERT INTO marcas({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING id",
                params,
            )
            return Response({"ok": True, "id": mid, "created": True})
        except IntegrityError:
            existing = q(
                "SELECT id FROM marcas WHERE LOWER(nombre)=LOWER(%s)",
                [nombre],
                one=True,
            )
            if existing:
                return Response({"ok": True, "id": existing["id"], "updated": False})
            raise


class CatalogoModelosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        marca_id = request.GET.get("marca_id")
        if not marca_id:
            return Response({"detail": "marca_id requerido"}, status=400)

        rows = q(
            """
            SELECT m.id, m.nombre,
                m.tecnico_id,
                COALESCE(u.nombre,'') AS tecnico_nombre,
                COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                COALESCE(m.variante,'') AS variante
            FROM models m
            LEFT JOIN users u ON u.id = m.tecnico_id
            WHERE m.marca_id=%s
            ORDER BY m.nombre
            """,
            [marca_id],
        )
        return Response(rows)


class CatalogoVariantesPorMarcaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, bid):
        try:
            marca_id = int(bid)
        except (TypeError, ValueError):
            return Response({"detail": "marca_id inválido"}, status=400)

        rows = q(
            """
            SELECT DISTINCT TRIM(variante) AS variante
            FROM models
            WHERE marca_id=%s
              AND NULLIF(TRIM(COALESCE(variante, '')), '') IS NOT NULL
            ORDER BY TRIM(variante)
            """,
            [marca_id],
        ) or []
        out = [r.get("variante") for r in rows if (r.get("variante") or "").strip()]
        return Response(out)


class CatalogoUbicacionesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        ensure_default_locations()
        return Response(q("SELECT id, nombre FROM locations ORDER BY id"))


class ModeloVarianteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, marca_id: int, modelo_id: int):
        _set_audit_user(request)
        d = request.data or {}
        variante = (d.get("variante") or "").strip()
        exec_void(
            """
            UPDATE models
               SET variante = NULLIF(%s,'')
             WHERE id=%s AND marca_id=%s
            """,
            [variante, modelo_id, marca_id],
        )
        return Response({"ok": True})


class ModeloTecnicoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def patch(self, request, bid, mid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        tecnico_id = request.data.get("tecnico_id")
        if tecnico_id:
            ok = q("SELECT id FROM users WHERE id=%s AND activo=true AND rol IN ('tecnico','jefe')",
                [tecnico_id], one=True)
            if not ok:
                raise ValidationError("Técnico inválido")
        exec_void("UPDATE models SET tecnico_id=%s WHERE id=%s AND marca_id=%s", [tecnico_id, mid, bid])
        return Response({"ok": True, "tecnico_id": tecnico_id})


class MarcaTecnicoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def patch(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        tecnico_id = request.data.get("tecnico_id")
        if tecnico_id:
            ok = q("SELECT id FROM users WHERE id=%s AND activo=true AND rol IN ('tecnico','jefe')",
                [tecnico_id], one=True)
            if not ok:
                raise ValidationError("Técnico inválido")
        exec_void("UPDATE marcas SET tecnico_id=%s WHERE id=%s", [tecnico_id, bid])
        return Response({"ok": True, "tecnico_id": tecnico_id})


class MarcaAplicarTecnicoAModelosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        if connection.vendor == "postgresql":
            q(
                """
                UPDATE models m
                   SET tecnico_id = b.tecnico_id
                  FROM marcas b
                 WHERE m.marca_id = b.id
                   AND b.id = %s
                   AND m.tecnico_id IS NULL
                """,
                [bid],
            )
        else:
            q(
                """
                UPDATE models m
                JOIN marcas b ON m.marca_id = b.id
                   SET m.tecnico_id = b.tecnico_id
                 WHERE b.id = %s
                   AND m.tecnico_id IS NULL
                """,
                [bid],
            )
        return Response({"ok": True})


class ModelosPorMarcaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor", "tecnico", "recepcion"])
        rows = q(
            """
            SELECT id, nombre, tecnico_id, tipo_equipo, variante
            FROM models
            WHERE marca_id=%s
            ORDER BY nombre
            """,
            [bid],
        )
        return Response(rows)

    def post(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        n = (d.get("nombre") or d.get("name") or "").strip()
        if not n:
            return Response({"detail": "nombre requerido"}, status=400)
        tecnico_id = d.get("tecnico_id")
        tipo_equipo = (d.get("tipo_equipo") or "").strip() or None
        variante = (d.get("variante") or "").strip() or None
        if tecnico_id:
            ok = q(
                "SELECT id FROM users WHERE id=%s AND activo=true AND rol IN ('tecnico','jefe')",
                [tecnico_id],
                one=True,
            )
            if not ok:
                raise ValidationError("Técnico inválido")

        if connection.vendor == "postgresql":
            q(
                """
              INSERT INTO models(marca_id, nombre, tecnico_id, tipo_equipo, variante)
              VALUES (%(b)s, %(n)s, %(t)s, NULLIF(%(te)s,''), NULLIF(%(va)s,''))
              ON CONFLICT (marca_id, nombre) DO UPDATE
                 SET tecnico_id = EXCLUDED.tecnico_id,
                     tipo_equipo = COALESCE(EXCLUDED.tipo_equipo, models.tipo_equipo),
                     variante = COALESCE(EXCLUDED.variante, models.variante)
            """,
                {"b": bid, "n": n, "t": tecnico_id, "te": tipo_equipo, "va": variante},
            )
        else:
            q(
                """
                INSERT INTO models(marca_id, nombre, tecnico_id, tipo_equipo, variante)
                VALUES (%s, %s, %s, NULLIF(%s,''), NULLIF(%s,''))
                ON DUPLICATE KEY UPDATE
                  tecnico_id = VALUES(tecnico_id),
                  tipo_equipo = IFNULL(VALUES(tipo_equipo), tipo_equipo),
                  variante = IFNULL(VALUES(variante), variante)
                """,
                [bid, n, tecnico_id, tipo_equipo, variante],
            )

        if tipo_equipo:
            if connection.vendor == "postgresql":
                q(
                    """
                    INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                    VALUES (%s,%s,TRUE)
                    ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                    """,
                    [bid, tipo_equipo],
                )
            else:
                q(
                    """
                    INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                    VALUES (%s,%s,TRUE)
                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                    """,
                    [bid, tipo_equipo],
                )

            existing_tipo = q(
                "SELECT id FROM catalogo_tipos_equipo WHERE UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                [tipo_equipo],
                one=True,
            )
            if existing_tipo:
                q("UPDATE catalogo_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s", [tipo_equipo, existing_tipo["id"]])
            else:
                q("INSERT INTO catalogo_tipos_equipo(nombre, activo) VALUES (%s, TRUE)", [tipo_equipo])

        return Response({"ok": True})


class MarcaDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def delete(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        try:
            row = q("SELECT COUNT(*) AS cnt FROM models WHERE marca_id=%s", [bid], one=True)
            if row and (row.get("cnt") or 0) > 0:
                return Response({
                    "detail": "No se puede eliminar la marca: tiene modelos asociados. Elimine o reasigne los modelos primero.",
                    "models_count": int(row.get("cnt") or 0),
                }, status=409)
            exec_void("DELETE FROM marcas WHERE id = %s", [bid])
            return Response({"ok": True})
        except IntegrityError:
            return Response({
                "detail": "No se puede eliminar la marca por restricciones de integridad (tiene referencias activas).",
            }, status=409)

    def patch(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        nombre = (d.get("nombre") or d.get("name") or "").strip()
        if not nombre:
            return Response({"detail": "nombre requerido"}, status=400)
        row = q("SELECT id FROM marcas WHERE id=%s", [bid], one=True)
        if not row:
            return Response({"detail": "marca no encontrada"}, status=404)
        clash = q("SELECT id FROM marcas WHERE id<>%s AND LOWER(nombre)=LOWER(%s)", [bid, nombre], one=True)
        if clash:
            return Response({"detail": "ya existe una marca con ese nombre"}, status=409)
        exec_void("UPDATE marcas SET nombre=%s WHERE id=%s", [nombre, bid])
        return Response({"ok": True})


class ModeloDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def delete(self, request, mid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        try:
            exec_void("DELETE FROM models WHERE id = %s", [mid])
            return Response({"ok": True})
        except IntegrityError:
            return Response({
                "detail": "No se puede eliminar el modelo por restricciones de integridad.",
            }, status=409)

    def patch(self, request, mid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        d = request.data or {}
        nombre = (d.get("nombre") or d.get("name") or "").strip()
        if not nombre:
            return Response({"detail": "nombre requerido"}, status=400)
        row = q("SELECT id, marca_id FROM models WHERE id=%s", [mid], one=True)
        if not row:
            return Response({"detail": "modelo no encontrado"}, status=404)
        marca_id = row.get("marca_id")
        clash = q(
            "SELECT id FROM models WHERE marca_id=%s AND id<>%s AND LOWER(nombre)=LOWER(%s)",
            [marca_id, mid, nombre], one=True,
        )
        if clash:
            return Response({"detail": "ya existe un modelo con ese nombre para la marca"}, status=409)
        exec_void("UPDATE models SET nombre=%s WHERE id=%s", [nombre, mid])
        return Response({"ok": True})


class ModelMergeView(APIView):
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

        src = q("SELECT id, marca_id, COALESCE(TRIM(tipo_equipo), '') AS tipo, nombre, COALESCE(TRIM(variante),'') AS variante FROM models WHERE id=%s", [source_id], one=True)
        dst = q("SELECT id, marca_id, COALESCE(TRIM(tipo_equipo), '') AS tipo, nombre, COALESCE(TRIM(variante),'') AS variante FROM models WHERE id=%s", [target_id], one=True)
        if not src or not dst:
            return Response({"detail": "modelo source/target inexistente"}, status=404)
        if src["marca_id"] != dst["marca_id"]:
            return Response({"detail": "Solo se puede unificar dentro de la misma marca"}, status=409)

        tipo_a = (src.get("tipo") or "").strip()
        tipo_b = (dst.get("tipo") or "").strip()
        # Allow merge when types match (case-insensitive) or when either is blank.
        if tipo_a.lower() != tipo_b.lower() and tipo_a != "" and tipo_b != "":
            return Response({"detail": "No se puede unificar: los tipos de equipo no coinciden"}, status=409)

        with transaction.atomic():
            exec_void("UPDATE devices SET model_id=%s WHERE model_id=%s", [target_id, source_id])
            # Merge simple variant field: copy if target empty and source has value
            dst_var = (dst.get("variante") or "").strip()
            src_var = (src.get("variante") or "").strip()
            if (not dst_var) and src_var:
                exec_void("UPDATE models SET variante=%s WHERE id=%s", [src_var, target_id])

            # Reflect variants into hierarchical catalog when possible (brand/type/series/variant)
            marca_id = dst.get("marca_id")
            tipo_nombre = (tipo_b or tipo_a or "").strip()
            modelo_nombre = (dst.get("nombre") or "").strip() or (src.get("nombre") or "").strip()
            if tipo_nombre and modelo_nombre:
                if connection.vendor == "postgresql":
                    q(
                        """
                        INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                        VALUES (%s,%s,TRUE)
                        ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                        """,
                        [marca_id, tipo_nombre],
                    )
                else:
                    q(
                        """
                        INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                        VALUES (%s,%s,TRUE)
                        ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                        """,
                        [marca_id, tipo_nombre],
                    )
                tipo_row = q(
                    "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                    [marca_id, tipo_nombre],
                    one=True,
                ) or {}
                tipo_id = tipo_row.get("id")
                if tipo_id:
                    if connection.vendor == "postgresql":
                        q(
                            """
                            INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                            VALUES (%s,%s,%s,TRUE)
                            ON CONFLICT (marca_id, tipo_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                            """,
                            [marca_id, tipo_id, modelo_nombre],
                        )
                    else:
                        q(
                            """
                            INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                            VALUES (%s,%s,%s,TRUE)
                            ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                            """,
                            [marca_id, tipo_id, modelo_nombre],
                        )
                    serie_row = q(
                        """
                        SELECT id FROM marca_series
                         WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                         LIMIT 1
                        """,
                        [marca_id, tipo_id, modelo_nombre],
                        one=True,
                    ) or {}
                    serie_id = serie_row.get("id")
                    if serie_id:
                        # 1) Asegurar variantes simples (campo models.variante) de ambos modelos
                        for vname in [dst_var, src_var]:
                            vname = (vname or "").strip()
                            if not vname:
                                continue
                            if connection.vendor == "postgresql":
                                q(
                                    """
                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                    VALUES (%s,%s,%s,%s,TRUE)
                                    ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                    DO UPDATE SET activo=EXCLUDED.activo
                                    """,
                                    [marca_id, tipo_id, serie_id, vname],
                                )
                            else:
                                q(
                                    """
                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                    VALUES (%s,%s,%s,%s,TRUE)
                                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                    """,
                                    [marca_id, tipo_id, serie_id, vname],
                                )

                        # 2) Copiar TODAS las variantes existentes en el catálogo para la serie del modelo source
                        src_tipo_nombre = (src.get("tipo") or "").strip()
                        if src_tipo_nombre:
                            src_tipo_row = q(
                                "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                                [marca_id, src_tipo_nombre],
                                one=True,
                            ) or {}
                            src_tipo_id = src_tipo_row.get("id")
                            if src_tipo_id:
                                src_serie_row = q(
                                    """
                                    SELECT id FROM marca_series
                                     WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                                     LIMIT 1
                                    """,
                                    [marca_id, src_tipo_id, (src.get("nombre") or "").strip()],
                                    one=True,
                                ) or {}
                                src_serie_id = src_serie_row.get("id")
                                if src_serie_id:
                                    src_vars = q(
                                        "SELECT nombre FROM marca_series_variantes WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s",
                                        [marca_id, src_tipo_id, src_serie_id],
                                    ) or []
                                    for rowv in src_vars:
                                        v2 = (rowv.get("nombre") or "").strip()
                                        if not v2:
                                            continue
                                        if connection.vendor == "postgresql":
                                            q(
                                                """
                                                INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                VALUES (%s,%s,%s,%s,TRUE)
                                                ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                                DO UPDATE SET activo=EXCLUDED.activo
                                                """,
                                                [marca_id, tipo_id, serie_id, v2],
                                            )
                                        else:
                                            q(
                                                """
                                                INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                VALUES (%s,%s,%s,%s,TRUE)
                                                ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                                """,
                                                [marca_id, tipo_id, serie_id, v2],
                                            )

            exec_void("DELETE FROM models WHERE id=%s", [source_id])

        moved = q("SELECT COUNT(*) AS cnt FROM devices WHERE model_id=%s", [target_id], one=True)
        return Response({"ok": True, "devices_now_point_to": target_id, "moved_count": moved.get("cnt") if moved else None})


class MarcaMergeView(APIView):
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

        a = q("SELECT id, nombre FROM marcas WHERE id=%s", [source_id], one=True)
        b = q("SELECT id, nombre FROM marcas WHERE id=%s", [target_id], one=True)
        if not a or not b:
            return Response({"detail": "marca source/target inexistente"}, status=404)

        force_types = bool(d.get("force_model_type_merge") or d.get("force") or False)

        # Primera pasada: decidir acciones y detectar conflictos
        modelos = q(
            "SELECT id, nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo, variante FROM models WHERE marca_id=%s",
            [source_id],
        ) or []

        actions = []  # (action, payload)
        conflicts = []
        for mm in modelos:
            nombre = (mm.get("nombre") or "").strip()
            tipo_src = (mm.get("tipo") or "").strip()
            # ¿Existe modelo con mismo nombre en la marca destino? (ignorando tipo)
            dup_any = q(
                "SELECT id, COALESCE(TRIM(tipo_equipo),'') AS tipo, variante FROM models WHERE marca_id=%s AND LOWER(TRIM(nombre))=LOWER(TRIM(%s)) LIMIT 1",
                [target_id, nombre],
                one=True,
            )
            if dup_any:
                tipo_dst = (dup_any.get("tipo") or "").strip()
                # Tipos iguales (o alguno vacío) => merge directo
                if tipo_src.lower() == tipo_dst.lower() or tipo_src == "" or tipo_dst == "" or force_types:
                    actions.append((
                        "merge_models",
                        {
                            "from_model_id": mm.get("id"),
                            "to_model_id": dup_any.get("id"),
                            "copy_variant_if_missing": True,
                        },
                    ))
                else:
                    conflicts.append({
                        "nombre": nombre,
                        "source_model_id": mm.get("id"),
                        "target_model_id": dup_any.get("id"),
                        "source_tipo": tipo_src,
                        "target_tipo": tipo_dst,
                    })
            else:
                actions.append(("move_model", {"model_id": mm.get("id")}))

        if conflicts and not force_types:
            # No aplicamos cambios; pedimos confirmación de merge a pesar del tipo distinto
            return Response({
                "detail": "Conflicto: hay modelos con mismo nombre pero distinto tipo_equipo en la marca destino",
                "conflicts": conflicts,
                "hint": "Reintente con force_model_type_merge=true para unificarlos igualmente.",
            }, status=409)

        # Segunda pasada: aplicar acciones dentro de una transacción
        with transaction.atomic():
            for (act, p) in actions:
                if act == "move_model":
                    mid_to_move = p["model_id"]
                    # Mover el modelo a la marca destino
                    exec_void("UPDATE models SET marca_id=%s WHERE id=%s", [target_id, mid_to_move])
                    # Reflejar variantes en el catálogo jerárquico para el modelo movido (no perder)
                    mv = q("SELECT nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo, COALESCE(TRIM(variante),'') AS variante FROM models WHERE id=%s", [mid_to_move], one=True) or {}
                    tipo_nombre_m = (mv.get("tipo") or "").strip()
                    modelo_nombre_m = (mv.get("nombre") or "").strip()
                    variante_nombre_m = (mv.get("variante") or "").strip()
                    if tipo_nombre_m and modelo_nombre_m:
                        # asegurar tipo en marca destino
                        if connection.vendor == "postgresql":
                            q(
                                """
                                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                                VALUES (%s,%s,TRUE)
                                ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                                """,
                                [target_id, tipo_nombre_m],
                            )
                        else:
                            q(
                                """
                                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                                VALUES (%s,%s,TRUE)
                                ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                """,
                                [target_id, tipo_nombre_m],
                            )
                        tipo_row_m = q(
                            "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                            [target_id, tipo_nombre_m],
                            one=True,
                        ) or {}
                        tipo_id_m = tipo_row_m.get("id")
                        if tipo_id_m:
                            # asegurar serie en marca destino
                            if connection.vendor == "postgresql":
                                q(
                                    """
                                    INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                                    VALUES (%s,%s,%s,TRUE)
                                    ON CONFLICT (marca_id, tipo_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                                    """,
                                    [target_id, tipo_id_m, modelo_nombre_m],
                                )
                            else:
                                q(
                                    """
                                    INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                                    VALUES (%s,%s,%s,TRUE)
                                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                    """,
                                    [target_id, tipo_id_m, modelo_nombre_m],
                                )
                            serie_row_m = q(
                                """
                                SELECT id FROM marca_series
                                 WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                                 LIMIT 1
                                """,
                                [target_id, tipo_id_m, modelo_nombre_m],
                                one=True,
                            ) or {}
                            serie_id_m = serie_row_m.get("id")
                            if serie_id_m:
                                # 1) Copiar variante simple del modelo movido (si existe)
                                if variante_nombre_m:
                                    if connection.vendor == "postgresql":
                                        q(
                                            """
                                            INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                            VALUES (%s,%s,%s,%s,TRUE)
                                            ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                            DO UPDATE SET activo=EXCLUDED.activo
                                            """,
                                            [target_id, tipo_id_m, serie_id_m, variante_nombre_m],
                                        )
                                    else:
                                        q(
                                            """
                                            INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                            VALUES (%s,%s,%s,%s,TRUE)
                                            ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                            """,
                                            [target_id, tipo_id_m, serie_id_m, variante_nombre_m],
                                        )
                                # 2) Copiar TODAS las variantes del catálogo de la marca origen (si existían)
                                src_tipo_row = q(
                                    "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                                    [source_id, tipo_nombre_m],
                                    one=True,
                                ) or {}
                                src_tipo_id = src_tipo_row.get("id")
                                if src_tipo_id:
                                    src_serie_row = q(
                                        """
                                        SELECT id FROM marca_series
                                         WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                                         LIMIT 1
                                        """,
                                        [source_id, src_tipo_id, modelo_nombre_m],
                                        one=True,
                                    ) or {}
                                    src_serie_id = src_serie_row.get("id")
                                    if src_serie_id:
                                        src_vars = q(
                                            "SELECT nombre FROM marca_series_variantes WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s",
                                            [source_id, src_tipo_id, src_serie_id],
                                        ) or []
                                        for rowv in src_vars:
                                            vnom = (rowv.get("nombre") or "").strip()
                                            if not vnom:
                                                continue
                                            if connection.vendor == "postgresql":
                                                q(
                                                    """
                                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                    VALUES (%s,%s,%s,%s,TRUE)
                                                    ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                                    DO UPDATE SET activo=EXCLUDED.activo
                                                    """,
                                                    [target_id, tipo_id_m, serie_id_m, vnom],
                                                )
                                            else:
                                                q(
                                                    """
                                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                    VALUES (%s,%s,%s,%s,TRUE)
                                                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                                    """,
                                                    [target_id, tipo_id_m, serie_id_m, vnom],
                                                )
                elif act == "merge_models":
                    to_id = p["to_model_id"]
                    from_id = p["from_model_id"]
                    # Mover devices al modelo destino
                    exec_void("UPDATE devices SET model_id=%s WHERE model_id=%s", [to_id, from_id])
                    # Copiar variante si destino no tiene y el source sí
                    if p.get("copy_variant_if_missing"):
                        dst = q("SELECT variante FROM models WHERE id=%s", [to_id], one=True) or {}
                        src = q("SELECT variante FROM models WHERE id=%s", [from_id], one=True) or {}
                        if (dst.get("variante") or None) in (None, "") and (src.get("variante") or "").strip():
                            exec_void("UPDATE models SET variante=%s WHERE id=%s", [(src.get("variante") or "").strip(), to_id])
                    # Reflect variants into hierarchical catalog (do not lose)
                    dst_full = q("SELECT marca_id, nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo, COALESCE(TRIM(variante),'') AS variante FROM models WHERE id=%s", [to_id], one=True) or {}
                    src_full = q("SELECT marca_id, nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo, COALESCE(TRIM(variante),'') AS variante FROM models WHERE id=%s", [from_id], one=True) or {}
                    marca_id_2 = dst_full.get("marca_id") or target_id
                    tipo_nombre_2 = (dst_full.get("tipo") or src_full.get("tipo") or "").strip()
                    modelo_nombre_2 = (dst_full.get("nombre") or src_full.get("nombre") or "").strip()
                    if marca_id_2 and tipo_nombre_2 and modelo_nombre_2:
                        if connection.vendor == "postgresql":
                            q(
                                """
                                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                                VALUES (%s,%s,TRUE)
                                ON CONFLICT (marca_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                                """,
                                [marca_id_2, tipo_nombre_2],
                            )
                        else:
                            q(
                                """
                                INSERT INTO marca_tipos_equipo(marca_id, nombre, activo)
                                VALUES (%s,%s,TRUE)
                                ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                """,
                                [marca_id_2, tipo_nombre_2],
                            )
                        tipo_row_2 = q(
                            "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                            [marca_id_2, tipo_nombre_2],
                            one=True,
                        ) or {}
                        t_id = tipo_row_2.get("id")
                        if t_id:
                            if connection.vendor == "postgresql":
                                q(
                                    """
                                    INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                                    VALUES (%s,%s,%s,TRUE)
                                    ON CONFLICT (marca_id, tipo_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                                    """,
                                    [marca_id_2, t_id, modelo_nombre_2],
                                )
                            else:
                                q(
                                    """
                                    INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                                    VALUES (%s,%s,%s,TRUE)
                                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                    """,
                                    [marca_id_2, t_id, modelo_nombre_2],
                                )
                            serie_row_2 = q(
                                """
                                SELECT id FROM marca_series
                                 WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                                 LIMIT 1
                                """,
                                [marca_id_2, t_id, modelo_nombre_2],
                                one=True,
                            ) or {}
                            s_id = serie_row_2.get("id")
                            if s_id:
                                # 1) Insertar variantes desde campos simples de ambos modelos
                                for vname in [dst_full.get("variante"), src_full.get("variante")]:
                                    v = (vname or "").strip()
                                    if v:
                                        if connection.vendor == "postgresql":
                                            q(
                                                """
                                                INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                VALUES (%s,%s,%s,%s,TRUE)
                                                ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                                DO UPDATE SET activo=EXCLUDED.activo
                                                """,
                                                [marca_id_2, t_id, s_id, v],
                                            )
                                        else:
                                            q(
                                                """
                                                INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                VALUES (%s,%s,%s,%s,TRUE)
                                                ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                                """,
                                                [marca_id_2, t_id, s_id, v],
                                            )
                                # 2) Copiar TODAS las variantes del catálogo de la marca origen para el modelo source
                                src_tipo_row2 = q(
                                    "SELECT id FROM marca_tipos_equipo WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
                                    [source_id, (src_full.get("tipo") or "").strip()],
                                    one=True,
                                ) or {}
                                src_tipo_id2 = src_tipo_row2.get("id")
                                if src_tipo_id2:
                                    src_serie_row2 = q(
                                        """
                                        SELECT id FROM marca_series
                                         WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                                         LIMIT 1
                                        """,
                                        [source_id, src_tipo_id2, (src_full.get("nombre") or "").strip()],
                                        one=True,
                                    ) or {}
                                    src_serie_id2 = src_serie_row2.get("id")
                                    if src_serie_id2:
                                        src_vars2 = q(
                                            "SELECT nombre FROM marca_series_variantes WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s",
                                            [source_id, src_tipo_id2, src_serie_id2],
                                        ) or []
                                        for rowv in src_vars2:
                                            v2 = (rowv.get("nombre") or "").strip()
                                            if not v2:
                                                continue
                                            if connection.vendor == "postgresql":
                                                q(
                                                    """
                                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                    VALUES (%s,%s,%s,%s,TRUE)
                                                    ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                                                    DO UPDATE SET activo=EXCLUDED.activo
                                                    """,
                                                    [marca_id_2, t_id, s_id, v2],
                                                )
                                            else:
                                                q(
                                                    """
                                                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                                                    VALUES (%s,%s,%s,%s,TRUE)
                                                    ON DUPLICATE KEY UPDATE activo=VALUES(activo)
                                                    """,
                                                    [marca_id_2, t_id, s_id, v2],
                                                )

                    # Delete duplicate model
                    exec_void("DELETE FROM models WHERE id=%s", [from_id])

            # Actualizar marca en devices
            exec_void("UPDATE devices SET marca_id=%s WHERE marca_id=%s", [target_id, source_id])
            # Eliminar marca origen
            exec_void("DELETE FROM marcas WHERE id=%s", [source_id])

        moved_count = 0
        merged_count = 0
        try:
            moved_count = sum(1 for (act, _) in actions if act == "move_model")
            merged_count = sum(1 for (act, _) in actions if act == "merge_models")
        except Exception:
            moved_count = 0
            merged_count = 0

        return Response({"ok": True, "target_id": target_id, "conflicts_resolved": len(conflicts) if conflicts else 0, "models_moved": int(moved_count), "models_merged": int(merged_count)})


class MarcaDeleteCascadeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def delete(self, request, bid):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        _set_audit_user(request)
        try:
            with transaction.atomic():
                exec_void(
                    """
                    UPDATE devices
                       SET model_id = NULL
                     WHERE model_id IN (SELECT id FROM models WHERE marca_id = %s)
                    """,
                    [bid],
                )
                exec_void("UPDATE devices SET marca_id = NULL WHERE marca_id = %s", [bid])
                exec_void("DELETE FROM models WHERE marca_id = %s", [bid])
                exec_void("DELETE FROM marcas WHERE id = %s", [bid])
            return Response({"ok": True})
        except IntegrityError:
            return Response({
                "detail": "No se pudo eliminar en cascada por restricciones de integridad.",
            }, status=409)


__all__ = [
    'CatalogoMarcasView',
    'CatalogoModelosView',
    'CatalogoVariantesPorMarcaView',
    'CatalogoUbicacionesView',
    'ModeloVarianteView',
    'ModelosPorMarcaView',
    'MarcaDeleteView',
    'MarcaDeleteCascadeView',
    'ModeloDeleteView',
    'ModeloTecnicoView',
    'MarcaTecnicoView',
    'MarcaAplicarTecnicoAModelosView',
    'ModelMergeView',
    'MarcaMergeView',
]
