from django.core.management.base import BaseCommand
from django.db import connection, transaction
from typing import Dict, List, Tuple, Optional, Any
import os
import csv


class Command(BaseCommand):
    help = (
        "Fase 1: Deduplica devices por numero_serie (normalizado). "
        "Mantiene el menor device.id como canónico, reasigna ingresos al canónico, "
        "y consolida snapshot usando datos del último ingreso."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Simula sin escribir (default)")
        parser.add_argument("--apply", action="store_true", help="Aplica cambios")
        parser.add_argument("--limit", type=int, default=None, help="Limita grupos a procesar")
        parser.add_argument("--docs-dir", default=None, help="Directorio de reportes (default autodetecta docs)")

    @staticmethod
    def _norm_ns(ns: Optional[str]) -> str:
        s = (ns or "").strip().upper()
        return s.replace(" ", "").replace("-", "")

    def _pick_docs_dir(self, user_docs_dir: Optional[str]) -> str:
        if user_docs_dir:
            return user_docs_dir
        if os.path.isdir(os.path.join("docs")):
            return os.path.join("docs")
        if os.path.isdir(os.path.join("..", "docs")):
            return os.path.join("..", "docs")
        return os.path.join("docs")

    def _write_csv(self, path: str, headers: List[str], rows: List[List[Any]]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)

    def _find_mgbio_id(self, cur) -> Optional[int]:
        # Heurística: buscar cliente MGBIO por nombre
        cur.execute(
            """
            SELECT id
              FROM customers
             WHERE LOWER(razon_social) LIKE '%mg%bio%'
             ORDER BY id ASC
             LIMIT 1
            """
        )
        r = cur.fetchone()
        return int(r[0]) if r else None

    def handle(self, *args, **opts):
        dry = True
        if opts.get("apply"):
            dry = False
        if opts.get("dry_run"):
            dry = True

        docs_dir = self._pick_docs_dir(opts.get("docs_dir"))
        merges_csv = os.path.join(docs_dir, "devices_merge_result.csv")
        conflicts_csv = os.path.join(docs_dir, "devices_conflictos.csv")
        backups_devices_csv = os.path.join(docs_dir, "backup_devices_before.csv")
        backups_ingresos_csv = os.path.join(docs_dir, "backup_ingresos_before.csv")

        # Datos de salida
        merges_rows: List[List[Any]] = [[
            "ns_norm", "canonical_id", "canonical_numero_serie", "removed_ids",
            "chosen_marca_id", "chosen_model_id", "chosen_customer_id",
            "snapshot_alquilado", "snapshot_alquiler_a", "snapshot_n_de_control", "snapshot_propietario",
            "snapshot_last_ingreso_id",
        ]]
        conflict_rows: List[List[Any]] = [[
            "ns_norm", "device_ids", "marca_ids", "model_ids", "customer_ids", "nota"
        ]]
        backup_devices: List[List[Any]] = [[
            "id", "customer_id", "marca_id", "model_id", "numero_serie", "numero_interno",
            "tipo_equipo", "variante", "garantia_vence", "propietario", "n_de_control", "alquilado", "alquiler_a"
        ]]
        backup_ingresos: List[List[Any]] = [[
            "ingreso_id", "device_id", "ubicacion_id", "alquilado", "alquiler_a"
        ]]

        limit = opts.get("limit")

        with transaction.atomic():
            with connection.cursor() as cur:
                # 1) Armar grupos por ns_norm
                cur.execute(
                    """
                    SELECT ns_norm, array_agg(id ORDER BY id ASC) AS device_ids,
                           array_agg(marca_id), array_agg(model_id), array_agg(customer_id),
                           array_agg(numero_serie)
                      FROM (
                        SELECT d.id, d.marca_id, d.model_id, d.customer_id, d.numero_serie,
                               UPPER(REPLACE(REPLACE(COALESCE(d.numero_serie,''),' ',''),'-','')) AS ns_norm
                          FROM devices d
                         WHERE COALESCE(NULLIF(TRIM(d.numero_serie),''), '') <> ''
                      ) s
                     GROUP BY ns_norm
                     HAVING COUNT(*) > 1
                     ORDER BY ns_norm ASC
                    """
                )
                groups = cur.fetchall() or []

                mgbio_id = self._find_mgbio_id(cur)

                processed = 0
                for ns_norm, device_ids, marca_ids, model_ids, customer_ids, series in groups:
                    if limit and processed >= limit:
                        break
                    processed += 1

                    canonical_id = int(device_ids[0])  # menor id
                    removed_ids = [int(x) for x in device_ids[1:]]

                    # Backups devices
                    cur.execute(
                        "SELECT id, customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo, variante, garantia_vence, propietario, n_de_control, alquilado, alquiler_a FROM devices WHERE id = ANY(%s)",
                        [device_ids],
                    )
                    for row in cur.fetchall() or []:
                        backup_devices.append(list(row))

                    # 2) Recolectar último ingreso global entre todos los devices del grupo
                    cur.execute(
                        """
                        SELECT t.id, t.device_id, t.alquilado, t.alquiler_a,
                               t.propietario_nombre,
                               COALESCE(l.nombre,'') AS ubicacion,
                               COALESCE(d.n_de_control,'') AS n_de_control
                          FROM ingresos t
                          JOIN devices d ON d.id = t.device_id
                          LEFT JOIN locations l ON l.id = t.ubicacion_id
                         WHERE t.device_id = ANY(%s)
                         ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
                         LIMIT 1
                        """,
                        [device_ids],
                    )
                    last_row = cur.fetchone()
                    last_ingreso_id = int(last_row[0]) if last_row else None
                    snap_alquilado = bool(last_row[2]) if last_row else False
                    snap_alquiler_a = last_row[3] if last_row else None
                    snap_propietario = last_row[4] if last_row else None
                    snap_n_de_control = last_row[6] if last_row else None

                    # 3) Elegir marca/modelo consistente: mayoría de valores no nulos
                    def _majority(values: List[Optional[int]]) -> Optional[int]:
                        counts: Dict[int, int] = {}
                        for v in values:
                            if v is None:
                                continue
                            counts[v] = counts.get(v, 0) + 1
                        if not counts:
                            return None
                        return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]

                    chosen_marca = _majority(list(marca_ids)) or None
                    chosen_model = _majority(list(model_ids)) or None
                    if chosen_model is not None:
                        # Si hay modelo, la marca final debe ser la del modelo para no dejar cruces inválidos.
                        cur.execute("SELECT marca_id FROM models WHERE id=%s", [chosen_model])
                        model_row = cur.fetchone()
                        model_marca_id = model_row[0] if model_row else None
                        if model_marca_id is not None:
                            chosen_marca = int(model_marca_id)

                    # 4) Determinar si propio (numero_serie con prefijo MG/NM/NV)
                    series_list = [s or "" for s in series]
                    is_own = False
                    for s in series_list:
                        s_up = s.strip().upper()
                        if s_up.startswith("MG") or s_up.startswith("NM") or s_up.startswith("NV"):
                            is_own = True
                            break

                    # 5) customer_id a fijar
                    chosen_customer = None
                    if is_own and mgbio_id:
                        chosen_customer = mgbio_id
                    else:
                        chosen_customer = _majority(list(customer_ids)) or int(customer_ids[0])

                    # 6) Reasignar ingresos de duplicados y consolidar device canónico
                    # Backups de ingresos a mover
                    if removed_ids:
                        cur.execute(
                            "SELECT id, device_id, ubicacion_id, alquilado, alquiler_a FROM ingresos WHERE device_id = ANY(%s)",
                            [removed_ids],
                        )
                        for row in cur.fetchall() or []:
                            backup_ingresos.append(list(row))

                    if not dry:
                        # Reasignar ingresos a canónico
                        if removed_ids:
                            cur.execute(
                                "UPDATE ingresos SET device_id=%s WHERE device_id = ANY(%s)",
                                [canonical_id, removed_ids],
                            )

                        # Consolidar snapshot en device canónico
                        cur.execute(
                            """
                            UPDATE devices
                               SET marca_id = COALESCE(%s, marca_id),
                                   model_id = COALESCE(%s, model_id),
                                   customer_id = COALESCE(%s, customer_id),
                                   alquilado = COALESCE(%s, alquilado),
                                   alquiler_a = COALESCE(%s, alquiler_a),
                                   n_de_control = COALESCE(NULLIF(%s,''), n_de_control),
                                   propietario = CASE WHEN %s THEN COALESCE(%s, propietario) ELSE propietario END
                             WHERE id = %s
                            """,
                            [
                                chosen_marca,
                                chosen_model,
                                chosen_customer,
                                snap_alquilado,
                                snap_alquiler_a,
                                snap_n_de_control or None,
                                True if is_own else False,
                                snap_propietario,
                                canonical_id,
                            ],
                        )

                        # Borrar devices duplicados
                        if removed_ids:
                            cur.execute("DELETE FROM devices WHERE id = ANY(%s)", [removed_ids])

                    # 7) Conflictos (marcas/modelos/clientes distintos)
                    def uniq(vals):
                        s = set()
                        for v in vals:
                            s.add(v)
                        # sort with None last
                        return sorted(list(s), key=lambda x: (x is None, x if x is not None else 0))
                    mset = uniq(list(marca_ids))
                    mdlset = uniq(list(model_ids))
                    cset = uniq(list(customer_ids))
                    nota = []
                    if len([x for x in mset if x is not None]) > 1:
                        nota.append("marcas_distintas")
                    if len([x for x in mdlset if x is not None]) > 1:
                        nota.append("modelos_distintos")
                    if len([x for x in cset if x is not None]) > 1:
                        nota.append("clientes_distintos")
                    if nota:
                        conflict_rows.append([
                            ns_norm, ",".join(map(str, device_ids)), ",".join(map(lambda x: str(x), mset)), ",".join(map(lambda x: str(x), mdlset)), ",".join(map(lambda x: str(x), cset)), ";".join(nota)
                        ])

                    merges_rows.append([
                        ns_norm, canonical_id, series_list[0] if series_list else "", ",".join(map(str, removed_ids)),
                        chosen_marca or "", chosen_model or "", chosen_customer or "",
                        snap_alquilado, snap_alquiler_a or "", snap_n_de_control or "", snap_propietario or "",
                        last_ingreso_id or "",
                    ])

                if dry:
                    transaction.set_rollback(True)

        # Escribir reportes
        self._write_csv(merges_csv, merges_rows[0], merges_rows[1:])
        self._write_csv(conflicts_csv, conflict_rows[0], conflict_rows[1:])
        self._write_csv(backups_devices_csv, backup_devices[0], backup_devices[1:])
        self._write_csv(backups_ingresos_csv, backup_ingresos[0], backup_ingresos[1:])

        self.stdout.write(("DRY-RUN " if dry else "APLICADO ") + "OK: Fase 1 dedupe | Reportes en docs")
