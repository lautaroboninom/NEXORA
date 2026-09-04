"""Cierra los procesos de Bejerman que quedaron trabados sin salida posible.

El panel de sincronización acumulaba filas que ninguna acción podía resolver:

* Intentos de remito rechazados **antes de llegar a Bejerman** que quedaron en
  `failed` mientras la entrega seguía por otro proceso. Diez del mismo remito de
  OXIGENOTERAPIA LOMAS (la ficha del cliente sin situación de IVA), cinco de la
  entrega de MARK. El código ya no los deja acumularse —los cierra al reservar el
  intento nuevo y al completar la entrega— pero los viejos hay que barrerlos.
* Jobs de stock con un `sync_type` que ninguna versión de NEXORA implementa,
  dejados por un script de auditoría del 01/07/2026.

Nunca toca un fallo ambiguo: un timeout o una conexión cortada pueden haber
emitido el remito igual, y ésos tienen que seguir a la vista hasta que alguien
los verifique. Por defecto no escribe nada: imprime lo que haría. Con `--apply`
lo aplica.
"""

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from service.bejerman_delivery import (
    REMITO_REVIEW_STATE_SUPERSEDED,
    _json_object,
    _optional_text,
    _remito_group_confirms_not_imported,
)
from service.bejerman_sdk import is_bejerman_success_message
from service.bejerman_sync import JOB_STATUS_CANCELLED, is_supported_sync_type

ZOMBIE_JOB_REASON = (
    "Tipo de sincronizacion sin implementacion en NEXORA: el job no podia ejecutarse "
    "en ninguna version. Cerrado en la limpieza del panel."
)

CANDIDATE_GROUPS_SQL = """
SELECT g.id,
       g.status,
       g.customer_name,
       g.created_at,
       g.last_error,
       g.response_summary,
       g.order_ids,
       (SELECT string_agg(w.remito_number, ', ' ORDER BY w.created_at)
          FROM bejerman_remito_groups w
         WHERE w.id <> g.id
           AND w.status IN ('generated', 'reconciled')
           AND EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements_text(w.order_ids) AS o(order_id)
                  WHERE o.order_id IN (SELECT jsonb_array_elements_text(g.order_ids))
               )
       ) AS emitidos,
       (SELECT count(*)
          FROM bejerman_remito_groups w
         WHERE w.id <> g.id
           AND w.created_at > g.created_at
           AND EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements_text(w.order_ids) AS o(order_id)
                  WHERE o.order_id IN (SELECT jsonb_array_elements_text(g.order_ids))
               )
       ) AS intentos_posteriores
  FROM bejerman_remito_groups g
 WHERE g.status = 'failed'
 ORDER BY g.created_at
"""


def _rows(cur):
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, values)) for values in cur.fetchall()]


class Command(BaseCommand):
    help = "Cierra procesos de Bejerman trabados sin salida (remitos superados y jobs sin implementacion)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Escribe los cambios (por defecto solo informa)")
        parser.add_argument(
            "--incluir-jobs",
            action="store_true",
            dest="incluir_jobs",
            help="Tambien descarta los jobs cuyo sync_type no tiene implementacion",
        )

    def handle(self, *args, **opts):
        apply_changes = bool(opts.get("apply"))
        include_jobs = bool(opts.get("incluir_jobs"))
        with transaction.atomic():
            # El numero va primero: si no, el motivo que se le escribe al grupo
            # cerrado cita el remito con el numero todavia mal formado.
            numeros = self._fix_malformed_numbers(apply_changes)
            closing = self._sweep_remito_groups(apply_changes)
            revisiones = self._promote_imported_but_failed_ris(apply_changes)
            zombies = self._sweep_zombie_jobs(apply_changes) if include_jobs else []
            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write("")
        estado = "APLICADO" if apply_changes else "SIMULACION, no se escribio nada"
        self.stdout.write(
            f"Grupos de remito a cerrar: {len(closing)} - Numeros a corregir: {len(numeros)} - "
            f"RIS a pasar a revision: {len(revisiones)} - Jobs a descartar: {len(zombies)} ({estado})"
        )

    def _fix_malformed_numbers(self, apply_changes):
        """El punto de venta pegado adelante del numero.

        Adoptar un comprobante a mano guardaba lo que el operador tipeaba tal cual:
        "RSS R 00004-00004741" quedaba como numero `0000400004741`. El codigo ya
        no lo hace (`_adopted_number_parts`), pero las filas viejas siguen mal.
        Solo se corrige cuando el numero es exactamente el punto de venta seguido
        de 8 digitos: cualquier otra cosa se informa y se deja.
        """

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                       comprobante_numero, remito_number
                  FROM bejerman_remito_groups
                 WHERE length(COALESCE(comprobante_numero, '')) > 8
                """
            )
            rows = _rows(cur)

        fixed = []
        for row in rows:
            punto = _optional_text(row.get("comprobante_pto_venta"))
            numero = _optional_text(row.get("comprobante_numero"))
            if not punto or not numero.startswith(punto) or len(numero) != len(punto) + 8:
                self.stdout.write(f"  SE DEJA  {row['id']}: numero raro que no es punto+numero ({numero})")
                continue
            corregido = numero[len(punto):]
            nuevo_remito = (
                f"{_optional_text(row.get('comprobante_tipo'))} "
                f"{_optional_text(row.get('comprobante_letra')) or 'R'} {punto}-{corregido}"
            )
            self.stdout.write(
                f"  CORRIGE  {row['id']}: {row.get('remito_number')} -> {nuevo_remito}"
            )
            fixed.append(row)
            if not apply_changes:
                continue
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups
                       SET comprobante_numero = %s,
                           remito_number = %s
                     WHERE id = %s
                    """,
                    [corregido, nuevo_remito, row["id"]],
                )
        return fixed

    def _promote_imported_but_failed_ris(self, apply_changes):
        """RIS marcados como fallados cuando Bejerman dijo que se importaron.

        Bejerman devuelve error y en el mismo mensaje avisa "El comprobante se
        importo correctamente. La Fecha de Contabilizacion se modifico...". El
        comprobante existe; el numero no vino. Dejarlos en `failed` pone en pantalla
        un boton de reintentar que emitiria el remito por segunda vez.
        """

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, ingreso_id, last_error
                  FROM bejerman_ingreso_remitos
                 WHERE status = 'failed'
                   AND last_error IS NOT NULL
                """
            )
            rows = _rows(cur)

        promoted = [row for row in rows if is_bejerman_success_message(row.get("last_error"))]
        for row in promoted:
            self.stdout.write(
                f"  REVISION RIS #{row['id']} OS-{row['ingreso_id']}: Bejerman aviso que el comprobante "
                "se importo, no se puede reintentar a ciegas"
            )
            if not apply_changes:
                continue
            mensaje = (
                "Bejerman devolvio un error pero avisa que el comprobante se importo: "
                f"«{_optional_text(row.get('last_error'))}». El remito puede haberse emitido igual, "
                "asi que NEXORA no lo reintenta solo. Busquelo en Bejerman: si esta, carguelo con el "
                "buscador por numero de serie; si no esta, reintente la emision."
            )
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_ingreso_remitos
                       SET status = 'failed_review',
                           last_error = %s,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = %s
                    """,
                    [mensaje, row["id"]],
                )
        return promoted

    def _sweep_remito_groups(self, apply_changes):
        with connection.cursor() as cur:
            cur.execute(CANDIDATE_GROUPS_SQL)
            candidates = _rows(cur)

        closing = []
        for row in candidates:
            summary = _json_object(row.get("response_summary"))
            if _optional_text(summary.get("reviewState")) == REMITO_REVIEW_STATE_SUPERSEDED:
                continue
            emitidos = _optional_text(row.get("emitidos"))
            posteriores = int(row.get("intentos_posteriores") or 0)
            if not emitidos and posteriores == 0:
                # Sigue siendo el unico intento de esa entrega: es trabajo pendiente de verdad.
                continue
            if not _remito_group_confirms_not_imported(row):
                detalle = (_optional_text(row.get("last_error")) or _optional_text(summary.get("error")))[:90]
                self.stdout.write(
                    f"  SE DEJA  {row['id']} ({row.get('customer_name')}): el rechazo no confirma "
                    f"que el comprobante no se creo -> {detalle}"
                )
                continue
            reason = (
                f"La entrega se completo con el remito {emitidos}."
                if emitidos
                else f"Reemplazado por {posteriores} intento(s) posterior(es) de las mismas ordenes."
            )
            closing.append((row, reason))

        for row, reason in closing:
            fecha = row["created_at"].strftime("%d/%m/%Y") if row.get("created_at") else "-"
            self.stdout.write(f"  CIERRA   {row['id']} ({row.get('customer_name')}, {fecha}): {reason}")
            if not apply_changes:
                continue
            patch = {
                "reviewState": REMITO_REVIEW_STATE_SUPERSEDED,
                "reviewRequired": False,
                "reconciliationRequired": False,
                "supersededAt": timezone.now().isoformat(),
                "supersededReason": reason,
                "supersededBy": "limpiar_procesos_bejerman_trabados",
                "blockedReason": (
                    "Cerrado en la limpieza del panel: el intento no llego a crear ningun comprobante "
                    f"en Bejerman. {reason}"
                ),
            }
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups
                       SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb,
                           next_retry_at = NULL
                     WHERE id = %s
                    """,
                    [json.dumps(patch), row["id"]],
                )
        return closing

    def _sweep_zombie_jobs(self, apply_changes):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, sync_type, ingreso_id, numero_serie, status, created_at
                  FROM bejerman_sync_jobs
                 WHERE status IN ('pending', 'failed', 'blocked')
                 ORDER BY created_at
                """
            )
            rows = _rows(cur)

        zombies = [row for row in rows if not is_supported_sync_type(row.get("sync_type"))]
        for row in zombies:
            self.stdout.write(
                f"  DESCARTA job #{row['id']} OS-{row['ingreso_id']} {row['numero_serie']} ({row['sync_type']})"
            )
            if not apply_changes:
                continue
            payload = {"cancellation": {"reason": ZOMBIE_JOB_REASON, "at": timezone.now().isoformat()}}
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_sync_jobs
                       SET status = %s,
                           last_error = %s,
                           response_payload = COALESCE(response_payload, '{}'::jsonb) || %s::jsonb,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = %s
                    """,
                    [JOB_STATUS_CANCELLED, ZOMBIE_JOB_REASON, json.dumps(payload), row["id"]],
                )
        return zombies
