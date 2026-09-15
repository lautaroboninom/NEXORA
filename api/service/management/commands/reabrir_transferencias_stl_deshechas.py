"""Reencola las transferencias STR→STL que figuran hechas pero que Bejerman ya no refleja.

Caso de origen: OS 28054 (MG 1464, N/S T9022575). El 08/06/2026 `restore_target_stock_from_jobs`
le emitió un ENT a STL sin la SAL de STR y el job 30 quedó `succeeded`. El 07/09
`sql/ops/bejerman_borrar_ent_fantasma_backfill_stl.sql` borró ese ENT —y otros 17— para sacar el
stock duplicado, pero dejó la unidad en STR y el job en verde: nada volvió a mirarlo. El equipo
está en la Estantería de Alquiler y Bejerman lo tiene en el taller.

Qué hace, sin decidir a ojo:

- Toma los jobs `stock_str_to_stl` en `succeeded` cuya OS es la última del equipo, sigue en la
  Estantería de Alquiler, sin alquilar y fuera de los estados de entrega, venta o baja (el mismo
  filtro que `list_rental_available_equipment`), y que todavía es del parque de alquiler
  (`ingreso_targets_rental_stock`).
- Le pregunta a Bejerman por la partida **y el artículo** del job. Sólo se reencola si el destino
  no tiene stock y el origen sí. Con stock en el destino está bien; sin stock en ninguno de los
  dos se informa para revisar a mano, porque reencolar ahí crearía stock de la nada.
- Con `--apply` los reencola con `enqueue_stock_transfer_for_ingreso(requeue_succeeded=True)`,
  el mismo camino que usa el cambio de ubicación a la estantería. El worker emite la SAL y el
  ENT y verifica el destino; como el job ya decía "SAL hecha" y el origen todavía tiene la
  unidad, la detección de reposición vuelve a emitir la SAL con su propio IdOrigen.

Lo que lista son **sospechosos**, no certezas: la marca de estantería de NEXORA no es inventario
y Depósito alquila y recibe desde Bejerman sin tocarla. Un STR legítimo (el equipo se alquiló y
volvió con un RDA) se ve igual que un STR que quedó por error. Por eso `--apply` exige `--ids`:
antes, mirar en `MovStock` que la partida no tenga un RTA, RTN, RDA o un ingreso posterior al job.

Sin `--apply` no escribe nada. Hace una consulta al SDK por equipo (dos si la serie tiene
letras): acotar con `--ids` o `--limit`.
"""

import json

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from service.bejerman_sync import (
    SYNC_TYPE_STOCK_STR_TO_STL,
    BejermanSDKClient,
    _deposit_from_record,
    _records_for_equipment_article,
    _records_for_partida,
    enqueue_stock_transfer_for_ingreso,
    ingreso_targets_rental_stock,
)


# Los mismos que excluye `list_rental_available_equipment`: el equipo ya no está en la estantería.
ESTADOS_FUERA_DE_ESTANTERIA = ["entregado", "alquilado", "baja", "vendido_pendiente_entrega", "vendido_entregado"]


def _fetchall_dicts(cur):
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class Command(BaseCommand):
    help = "Reencola los STR→STL en verde cuyo equipo está en la estantería pero Bejerman lo tiene en STR."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Reencola los jobs. Sin este flag solo informa.",
        )
        parser.add_argument(
            "--ids",
            default="",
            help="Limita la corrida a estas OS (separadas por coma).",
        )
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument(
            "--actor-user-id",
            type=int,
            default=None,
            help="Usuario con cuyas credenciales Bejerman se emiten los movimientos (obligatorio con --apply).",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        solo_ids = [int(x) for x in (options.get("ids") or "").replace(" ", "").split(",") if x]
        limit = max(1, int(options.get("limit") or 500))
        actor_user_id = options.get("actor_user_id")
        if apply_changes and not actor_user_id:
            # Los jobs viejos no tienen actor y el worker los bloquea con "Requiere ejecutar con
            # sesión de usuario y credenciales Bejerman personales": el 15/09/2026, 6 de los 11
            # reencolados pasaron de verde a bloqueado por eso.
            self.stderr.write(
                "--apply necesita --actor-user-id: el usuario con cuyas credenciales Bejerman se emiten los movimientos."
            )
            return
        if apply_changes and not solo_ids:
            # La marca de estantería de NEXORA no es inventario: Depósito alquila y recibe desde
            # Bejerman sin tocarla. De 15 candidatos del 14/09/2026, 4 tenían un RTA o un RDA
            # posterior al job y su STR era legítimo. Sólo se reencola lo que alguien verificó.
            self.stderr.write(
                "--apply necesita --ids: revisá en Bejerman que la partida no tenga un RTA, RTN, RDA "
                "o un ingreso posterior al job antes de reencolarla."
            )
            return

        filtro_ids = "AND t.id = ANY(%s)" if solo_ids else ""
        params = [SYNC_TYPE_STOCK_STR_TO_STL, ESTADOS_FUERA_DE_ESTANTERIA, "%estanter%", "%alquiler%"]
        if solo_ids:
            params.append(solo_ids)
        params.append(limit)
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id AS job_id,
                       j.ingreso_id,
                       COALESCE(j.numero_serie, '') AS numero_serie,
                       COALESCE(j.article_code, '') AS article_code,
                       COALESCE(NULLIF(j.source_deposit, ''), 'STR') AS source_deposit,
                       COALESCE(NULLIF(j.target_deposit, ''), 'STL') AS target_deposit,
                       NULLIF(j.company_key, '') AS company_key,
                       COALESCE(d.numero_interno, '') AS numero_interno
                  FROM bejerman_sync_jobs j
                  JOIN ingresos t ON t.id = j.ingreso_id
                  LEFT JOIN devices d ON d.id = t.device_id
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                 WHERE j.sync_type = %s
                   AND j.status = 'succeeded'
                   AND COALESCE(t.alquilado, FALSE) = FALSE
                   AND LOWER(COALESCE(t.estado::text, '')) <> ALL(%s)
                   AND LOWER(COALESCE(loc.nombre, '')) LIKE %s
                   AND LOWER(COALESCE(loc.nombre, '')) LIKE %s
                   AND NOT EXISTS (
                       SELECT 1 FROM ingresos t2 WHERE t2.device_id = t.device_id AND t2.id > t.id
                   )
                   {filtro_ids}
                 ORDER BY j.id
                 LIMIT %s
                """,
                params,
            )
            candidatos = _fetchall_dicts(cur)

        if not candidatos:
            self.stdout.write("No hay transferencias STR→STL en verde con el equipo en la estantería.")
            return

        bien, reencolar, revisar, errores = [], [], [], []
        clientes = {}
        try:
            for row in candidatos:
                serial = row["numero_serie"].strip()
                articulo = row["article_code"].strip()
                origen, destino = row["source_deposit"], row["target_deposit"]
                if not serial or not articulo:
                    # Sin artículo no se distingue al equipo de su gemelo con la misma serie.
                    revisar.append((row, "el job no tiene serie o artículo"))
                    continue
                if not ingreso_targets_rental_stock(int(row["ingreso_id"])):
                    revisar.append((row, "ya no es del parque de alquiler"))
                    continue
                company = row["company_key"]
                if company not in clientes:
                    clientes[company] = BejermanSDKClient(company_key=company, allow_system_credentials=True)
                try:
                    response = clientes[company].stock_by_deposit_partida("", serial)
                except Exception as exc:
                    errores.append((row, str(exc)))
                    continue

                def _stock_en(deposito=None):
                    return _records_for_equipment_article(
                        _records_for_partida(response, serial, deposito), articulo
                    )

                if _stock_en(destino):
                    bien.append((row, f"Bejerman lo tiene en {destino}"))
                elif _stock_en(origen):
                    reencolar.append((row, f"en {origen}, no en {destino}"))
                else:
                    otros = sorted({_deposit_from_record(r) for r in _stock_en()} - {""})
                    detalle = f"; está en {', '.join(otros)}" if otros else ""
                    revisar.append((row, f"sin stock en {origen} ni en {destino}{detalle}"))
        finally:
            for cliente in clientes.values():
                cliente.close()

        def _describe(item):
            row, motivo = item
            equipo = row["numero_interno"] or row["numero_serie"] or "?"
            return (
                f"  OS {row['ingreso_id']:>6}  job {row['job_id']:>5}  {equipo:<10} "
                f"serie={row['numero_serie']:<16} art={row['article_code']:<8} {motivo}"
            )

        self.stdout.write(f"Transferencias STR→STL en verde con el equipo en la estantería: {len(candidatos)}.")
        self.stdout.write("")
        self.stdout.write(f"[reencolar] el equipo está en la estantería y Bejerman lo tiene en el origen: {len(reencolar)}")
        for item in reencolar:
            self.stdout.write(_describe(item))
        self.stdout.write(f"[revisar a mano] no se reencola: {len(revisar)}")
        for item in revisar:
            self.stdout.write(_describe(item))
        if errores:
            self.stdout.write(f"[error consultando Bejerman]: {len(errores)}")
            for item in errores:
                self.stdout.write(_describe(item))
        self.stdout.write(f"[bien] ya está en el destino: {len(bien)}")

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write("Dry-run: no se escribió nada. Volvé a correrlo con --apply.")
            return

        marca = json.dumps(
            {
                "reabierto_stock_deshecho": {
                    "fecha": timezone.now().isoformat(),
                    "motivo": "succeeded con el equipo en la estantería y el stock todavía en el origen",
                }
            }
        )
        reencolados = 0
        for row, _motivo in reencolar:
            try:
                job = enqueue_stock_transfer_for_ingreso(int(row["ingreso_id"]), requeue_succeeded=True)
            except Exception as exc:
                self.stderr.write(f"  OS {row['ingreso_id']}: no se pudo reencolar: {exc}")
                continue
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_sync_jobs
                       SET request_payload = COALESCE(request_payload, '{}'::jsonb) || %s::jsonb,
                           actor_user_id = COALESCE(actor_user_id, %s)
                     WHERE id = %s
                    """,
                    [marca, actor_user_id, job["id"]],
                )
            reencolados += 1

        self.stdout.write("")
        self.stdout.write(f"Aplicado: {reencolados} de {len(reencolar)} jobs reencolados. El worker los procesa solo.")
