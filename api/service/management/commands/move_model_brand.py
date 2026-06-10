from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.views.tipo_equipo_utils import clean_tipo_equipo, preferred_row, tipo_equipo_key


class Command(BaseCommand):
    help = (
        "Mueve un modelo de una marca origen a una marca destino por nombre,"
        " preservando variantes. Si existe un modelo con el mismo nombre en"
        " la marca destino, unifica (merge) ambos modelos."
    )

    def add_arguments(self, parser):
        parser.add_argument("--model", required=True, help="Nombre del modelo a mover (p.ej., VacuMax)")
        parser.add_argument("--from-brand", dest="from_brand", required=True, help="Nombre de la marca origen")
        parser.add_argument("--to-brand", dest="to_brand", required=True, help="Nombre de la marca destino")
        parser.add_argument("--force-merge-types", action="store_true", help="Permitir unificar aunque los tipos de equipo difieran")
        parser.add_argument("--dry-run", action="store_true", help="Mostrar acciones sin aplicarlas")

    def _row(self, sql, params=None):
        with connection.cursor() as cur:
            cur.execute(sql, params or [])
            row = cur.fetchone()
            return row

    def _rows(self, sql, params=None):
        with connection.cursor() as cur:
            cur.execute(sql, params or [])
            return cur.fetchall() or []

    def _exec(self, sql, params=None):
        with connection.cursor() as cur:
            cur.execute(sql, params or [])

    def _ensure_marca_tipo_id(self, marca_id: int, nombre: str):
        clean = clean_tipo_equipo(nombre)
        if not clean:
            return None
        rows = self._rows(
            "SELECT id, nombre, activo FROM marca_tipos_equipo WHERE marca_id=%s ORDER BY id",
            [marca_id],
        )
        row = preferred_row(
            [dict(zip(["id", "nombre", "activo"], r)) for r in rows],
            clean,
        )
        if row:
            if clean_tipo_equipo(row.get("nombre")) != clean or not row.get("activo"):
                self._exec("UPDATE marca_tipos_equipo SET nombre=%s, activo=TRUE WHERE id=%s", [clean, row["id"]])
            return int(row["id"])
        self._exec(
            "INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,TRUE)",
            [marca_id, clean],
        )
        row = self._row(
            """
            SELECT id
              FROM marca_tipos_equipo
             WHERE marca_id=%s AND LOWER(TRIM(nombre))=LOWER(TRIM(%s))
             ORDER BY id DESC
             LIMIT 1
            """,
            [marca_id, clean],
        )
        return int(row[0]) if row else None

    def handle(self, *args, **opts):
        model_name = (opts.get("model") or "").strip()
        from_brand = (opts.get("from_brand") or "").strip()
        to_brand = (opts.get("to_brand") or "").strip()
        force_types = bool(opts.get("force_merge_types"))
        dry = bool(opts.get("dry_run"))

        if not model_name or not from_brand or not to_brand:
            self.stderr.write("Parámetros inválidos: --model, --from-brand y --to-brand son requeridos")
            return 1

        # Resolver marcas
        a = self._row(
            "SELECT id FROM marcas WHERE UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
            [from_brand],
        )
        b = self._row(
            "SELECT id FROM marcas WHERE UPPER(TRIM(nombre))=UPPER(TRIM(%s))",
            [to_brand],
        )
        if not a:
            self.stderr.write(f"Marca origen no encontrada: {from_brand}")
            return 1
        if not b:
            self.stderr.write(f"Marca destino no encontrada: {to_brand}")
            return 1
        source_id = int(a[0])
        target_id = int(b[0])
        if source_id == target_id:
            self.stdout.write("Las marcas origen y destino son iguales; no hay nada que mover")
            return 0

        # Resolver modelo en marca origen
        mm = self._row(
            """
            SELECT id, nombre, COALESCE(TRIM(tipo_equipo),'') AS tipo, COALESCE(TRIM(variante),'') AS variante
              FROM models
             WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
             LIMIT 1
            """,
            [source_id, model_name],
        )
        if not mm:
            self.stderr.write(f"Modelo '{model_name}' no encontrado en la marca '{from_brand}'")
            return 1
        model_id = int(mm[0])
        modelo_nombre = mm[1]
        tipo_nombre = (mm[2] or "").strip()
        variante_simple = (mm[3] or "").strip()

        # ¿Existe modelo homónimo en destino?
        dup = self._row(
            """
            SELECT id, COALESCE(TRIM(tipo_equipo),'') AS tipo, COALESCE(TRIM(variante),'') AS variante
              FROM models
             WHERE marca_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
             LIMIT 1
            """,
            [target_id, modelo_nombre],
        )

        def ensure_catalog_for_series(marca_id: int, tipo_txt: str, serie_nombre: str):
            if not tipo_txt or not serie_nombre:
                return None
            tipo_id = self._ensure_marca_tipo_id(marca_id, tipo_txt)
            if not tipo_id:
                return None
            # Asegurar serie
            self._exec(
                """
                INSERT INTO marca_series(marca_id, tipo_id, nombre, activo)
                VALUES (%s,%s,%s,TRUE)
                ON CONFLICT (marca_id, tipo_id, nombre) DO UPDATE SET activo=EXCLUDED.activo
                """,
                [marca_id, tipo_id, serie_nombre],
            )
            serie_row = self._row(
                """
                SELECT id FROM marca_series
                 WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                 LIMIT 1
                """,
                [marca_id, tipo_id, serie_nombre],
            )
            return (tipo_id, int(serie_row[0]) if serie_row else None)

        def copy_catalog_variants(src_marca_id: int, src_tipo_txt: str, src_serie_nombre: str, dst_marca_id: int, dst_tipo_id: int, dst_serie_id: int):
            if not (src_tipo_txt and src_serie_nombre and dst_serie_id and dst_tipo_id):
                return
            src_tipo_id = self._ensure_marca_tipo_id(src_marca_id, src_tipo_txt)
            if not src_tipo_id:
                return
            src_serie_row = self._row(
                """
                SELECT id FROM marca_series
                 WHERE marca_id=%s AND tipo_id=%s AND UPPER(TRIM(nombre))=UPPER(TRIM(%s))
                 LIMIT 1
                """,
                [src_marca_id, src_tipo_id, src_serie_nombre],
            )
            if not src_serie_row:
                return
            src_serie_id = int(src_serie_row[0])
            rows = self._rows(
                "SELECT nombre FROM marca_series_variantes WHERE marca_id=%s AND tipo_id=%s AND serie_id=%s",
                [src_marca_id, src_tipo_id, src_serie_id],
            )
            for (vname,) in rows:
                v = (vname or "").strip()
                if not v:
                    continue
                self._exec(
                    """
                    INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                    VALUES (%s,%s,%s,%s,TRUE)
                    ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                    DO UPDATE SET activo=EXCLUDED.activo
                    """,
                    [dst_marca_id, dst_tipo_id, dst_serie_id, v],
                )

        if dry:
            self.stdout.write(
                f"DRY-RUN: mover '{modelo_nombre}' de '{from_brand}' -> '{to_brand}'"
            )
            return 0

        with transaction.atomic():
            if dup:
                # Unificar con modelo existente en destino
                to_id = int(dup[0])
                dst_tipo = (dup[1] or "").strip()
                if (tipo_equipo_key(tipo_nombre) != tipo_equipo_key(dst_tipo)) and not (tipo_nombre == "" or dst_tipo == "" or force_types):
                    raise SystemExit("Tipos de equipo distintos; use --force-merge-types para unificar igualmente")

                # Devices -> modelo destino
                self._exec("UPDATE devices SET model_id=%s WHERE model_id=%s", [to_id, model_id])
                self._exec("UPDATE devices SET marca_id=%s WHERE model_id=%s", [target_id, to_id])

                # Copiar variante simple si falta en destino
                if variante_simple and not (dup[2] or "").strip():
                    self._exec("UPDATE models SET variante=%s WHERE id=%s", [variante_simple, to_id])

                # Asegurar catálogo para serie destino y variantes
                tipo_id, serie_id = ensure_catalog_for_series(target_id, (dst_tipo or tipo_nombre), modelo_nombre)
                if serie_id and tipo_id:
                    # variantes simples de ambos
                    for vname in [variante_simple, (dup[2] or "").strip()]:
                        v = (vname or "").strip()
                        if not v:
                            continue
                        self._exec(
                            """
                            INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                            VALUES (%s,%s,%s,%s,TRUE)
                            ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                            DO UPDATE SET activo=EXCLUDED.activo
                            """,
                            [target_id, tipo_id, serie_id, v],
                        )
                    # Copiar TODAS las variantes del catálogo de la serie origen
                    copy_catalog_variants(source_id, tipo_nombre, modelo_nombre, target_id, tipo_id, serie_id)

                # Eliminar modelo origen
                self._exec("DELETE FROM models WHERE id=%s", [model_id])
                self.stdout.write(f"OK: unificado con modelo destino id={to_id} en '{to_brand}'")
            else:
                # Mover modelo a la marca destino
                self._exec("UPDATE models SET marca_id=%s WHERE id=%s", [target_id, model_id])
                self._exec("UPDATE devices SET marca_id=%s WHERE model_id=%s", [target_id, model_id])

                # Reflejar catálogo en la marca destino
                tipo_id, serie_id = ensure_catalog_for_series(target_id, tipo_nombre, modelo_nombre)
                if serie_id and tipo_id:
                    # Variante simple del modelo (si existe)
                    if variante_simple:
                        self._exec(
                            """
                            INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo)
                            VALUES (%s,%s,%s,%s,TRUE)
                            ON CONFLICT (marca_id, tipo_id, serie_id, nombre)
                            DO UPDATE SET activo=EXCLUDED.activo
                            """,
                            [target_id, tipo_id, serie_id, variante_simple],
                        )
                    # Copiar TODAS las variantes del catálogo de la serie en origen
                    copy_catalog_variants(source_id, tipo_nombre, modelo_nombre, target_id, tipo_id, serie_id)

                self.stdout.write(f"OK: modelo movido a marca '{to_brand}' (id={target_id})")

        return 0
