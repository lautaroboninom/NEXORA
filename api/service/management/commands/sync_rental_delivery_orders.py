"""Backfill de los alquileres que quedaron a medio registrar.

Gemelo de `sync_delivered_service_release_ingresos`, para el otro lado del mismo agujero.
Un alquiler se escribe en dos lados: la **orden de entrega** (`delivery_orders`, tipo
`rental`) y el **equipo** (la hoja de servicio en `ingresos`, o `devices` cuando el equipo
nunca entro al taller). Hasta el 25/08/2026 la carga manual de un RTA ya emitido en
Bejerman escribia solo el primero, asi que el equipo se quedaba figurando disponible en la
Estanteria de Alquiler con el cliente ya usandolo.

Dos desfasajes, dos fases, dry-run por defecto:

- `ordenes`   la orden tiene el remito y el equipo vinculado, pero el equipo nunca quedo
              marcado. Se cierra con el mismo helper que usa el circuito.
- `bejerman`  la orden tiene el remito y **ningun equipo vinculado**: el caso que dejo el
              bug. El RTA se busca en Bejerman, y sus renglones de STL dicen que equipo
              salio. Se marca solo lo que resuelve sin ambiguedad; el resto se informa.

**La fecha de alquiler es la del comprobante, no la de hoy**, por lo mismo que en el
backfill de entregas: un alquiler de julio sellado con la fecha del backfill falsea
cualquier cuenta de dias alquilados.

**Dos cosas que este backfill NO hace a proposito** (verificado el 07/09/2026 sobre el dump
de produccion, y las dos necesitan a una persona):

- Si el equipo **reingreso despues** del RTA, no se toca: volvio y el alquiler ya termino.
  Marcarlo alquilado hoy lo sacaria de la estanteria estando en el taller.
- Si la partida **no existe en `devices`**, no se da de alta el equipo: se informa para que
  Recepcion lo cargue. Son equipos que salieron de STL sin pasar nunca por NEXORA y darlos
  de alta desde el articulo de Bejerman inventaria marca y modelo.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time

from django.core.management.base import BaseCommand
from django.db import connection

from service.bejerman_documents import parse_bejerman_remito_number
from service.delivery_orders import _sync_rental_ingresos_delivery

FASES = ("ordenes", "bejerman")
# Estados de orden que ya no esperan nada: un alquiler cancelado no marca ningun equipo.
ESTADOS_ORDEN_MUERTA = ("cancelado",)
# El RTA existe desde 2026; el piso solo protege del año tipeado en dos digitos.
_PISO_FECHA = date(2020, 1, 1)
_SQL_RTA_POR_NUMERO = """
SET NOCOUNT ON;
SELECT LTRIM(RTRIM(t.spv_Letra)) AS letra,
       LTRIM(RTRIM(t.spv_CodPvt)) AS punto,
       LTRIM(RTRIM(t.spv_Nro)) AS numero,
       CONVERT(varchar(10), c.scv_FEmision, 23) AS fecha,
       LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) AS cliente_codigo,
       LTRIM(RTRIM(COALESCE(c.scvcli_RazSoc, ''))) AS cliente_nombre,
       LTRIM(RTRIM(COALESCE(d.sdvdep_Cod, ''))) AS deposito,
       UPPER(LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, '')))) AS partida,
       LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS descripcion
  FROM dbo.SegCabV c
  JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
  JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
 WHERE LTRIM(RTRIM(t.spvtco_Cod)) = 'RTA'
   AND TRY_CONVERT(int, t.spv_Nro) IN ({numeros})
   AND LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) <> ''
 ORDER BY TRY_CONVERT(int, t.spv_Nro), d.sdv_NReng;
""".strip()


def _rows(cur) -> list[dict]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _serie_key(valor) -> str:
    """La identidad de la partida. Hay seriales cargados con espacios de mas en los dos lados."""
    return re.sub(r"\s+", "", str(valor or "").strip()).upper()


def _mediodia_local(dia: date) -> datetime:
    """La fecha del comprobante como instante, al mediodia de la zona de la app.

    Misma razon que en el backfill de entregas: una fecha suelta se guarda a medianoche UTC
    y en hora argentina se lee como el dia anterior.
    """
    from django.utils import timezone

    return timezone.make_aware(datetime.combine(dia, time(12, 0)), timezone.get_current_timezone())


def _fecha_comprobante(valor) -> date | None:
    texto = str(valor or "").strip()[:10]
    try:
        parsed = date.fromisoformat(texto)
    except ValueError:
        return None
    return parsed if parsed >= _PISO_FECHA else None


def _documento_de_orden(remito_number) -> tuple[str, int] | None:
    """(punto, numero) del remito de la orden, o None si no se puede leer."""
    try:
        parsed = parse_bejerman_remito_number(remito_number, default_type="RTA", allow_number_only=True)
    except ValueError:
        return None
    try:
        return parsed["point"], int(parsed["number"])
    except (KeyError, TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = (
        "Informa (y con --apply corrige) los alquileres cuyo RTA quedo solo en la orden de "
        "entrega y nunca llego al equipo. Dry-run por defecto."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios de las fases pedidas.")
        parser.add_argument(
            "--fase",
            default=",".join(FASES),
            help="Fases a correr, separadas por coma: " + ", ".join(FASES) + ". Default: todas.",
        )
        parser.add_argument(
            "--orden",
            default="",
            help="Limita la corrida a estas ordenes (numeros o ids separados por coma).",
        )
        parser.add_argument(
            "--usuario",
            type=int,
            default=None,
            help="Id de usuario a registrar como autor de los cambios (opcional).",
        )

    def handle(self, *args, **options):
        fases = [f.strip() for f in (options["fase"] or "").split(",") if f.strip()]
        desconocidas = [f for f in fases if f not in FASES]
        if desconocidas:
            self.stderr.write(
                "Fase desconocida: " + ", ".join(desconocidas) + ". Validas: " + ", ".join(FASES)
            )
            return
        apply_changes = options["apply"]
        actor = options["usuario"]
        filtro = [t.strip() for t in (options["orden"] or "").split(",") if t.strip()]
        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY-RUN: no se escribe nada. Agregar --apply para aplicar."))
        if "ordenes" in fases:
            self._fase_ordenes(filtro, apply_changes, actor)
        if "bejerman" in fases:
            self._fase_bejerman(filtro, apply_changes, actor)

    # --------------------------------------------------------------------- comun
    def _ordenes_de_alquiler(self, filtro: list[str], *, con_equipo: bool) -> list[dict]:
        """Ordenes de alquiler con remito cargado, con o sin equipo vinculado.

        `con_equipo` parte el universo en las dos fases: la orden que tiene de donde colgar
        el alquiler y la que no tiene nada.
        """
        equipo_sql = """
            EXISTS (SELECT 1 FROM delivery_order_items di
                     WHERE di.order_id = o.id AND (di.ingreso_id IS NOT NULL OR di.device_id IS NOT NULL))
            OR EXISTS (SELECT 1 FROM delivery_order_items di
                         JOIN delivery_order_item_partidas dp ON dp.order_item_id = di.id
                        WHERE di.order_id = o.id AND (dp.ingreso_id IS NOT NULL OR dp.device_id IS NOT NULL))
            OR o.ingreso_id IS NOT NULL OR o.device_id IS NOT NULL
        """
        where = [
            "o.delivery_type = 'rental'",
            "COALESCE(o.remito_number, '') <> ''",
            "o.status <> ALL(%s)",
            ("(" + equipo_sql + ")") if con_equipo else ("NOT (" + equipo_sql + ")"),
        ]
        params: list = [list(ESTADOS_ORDEN_MUERTA)]
        if filtro:
            where.append("(o.id = ANY(%s) OR o.order_number = ANY(%s))")
            params.extend([filtro, filtro])
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.order_number, o.status, o.remito_number, o.customer_name,
                       o.delivered_at, o.company_key
                  FROM delivery_orders o
                 WHERE """
                + "\n                   AND ".join(where)
                + "\n                 ORDER BY o.order_number",
                params,
            )
            return _rows(cur)

    def _equipo_de_partida(self, serie: str, fecha_rta: date | None) -> dict:
        """Resuelve la partida a un equipo de NEXORA y dice si se puede tocar.

        Devuelve siempre un dict con `motivo`: el backfill informa igual los que saltea,
        que es la mitad util de la corrida.
        """
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.id AS device_id, d.numero_serie, d.alquilado
                  FROM devices d
                 WHERE UPPER(REGEXP_REPLACE(COALESCE(d.numero_serie, ''), '[[:space:]]+', '', 'g')) = %s
                 ORDER BY d.id
                """,
                [serie],
            )
            devices = _rows(cur)
        if not devices:
            return {"motivo": "sin equipo en NEXORA"}
        if len(devices) > 1:
            ids = ", ".join(str(d["device_id"]) for d in devices)
            return {"motivo": f"la serie resuelve a mas de un equipo ({ids})"}
        device = devices[0]
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.estado, COALESCE(i.fecha_ingreso::date, i.fecha_creacion::date) AS ingreso
                  FROM ingresos i
                 WHERE i.device_id = %s
                 ORDER BY COALESCE(i.fecha_ingreso, i.fecha_creacion) DESC, i.id DESC
                """,
                [device["device_id"]],
            )
            ingresos = _rows(cur)
        if fecha_rta and any(i["ingreso"] and i["ingreso"] > fecha_rta for i in ingresos):
            return {"motivo": "reingreso posterior al remito: el alquiler ya termino", "device": device}
        if not ingresos:
            return {"motivo": "equipo sin hoja de servicio (habria que marcarlo en Equipos)", "device": device}
        return {"device": device, "ingreso": ingresos[0]}

    def _aplicar(
        self,
        orden: dict,
        equipo: dict,
        remito: str,
        fecha_rta: date | None,
        cliente: str,
        apply_changes: bool,
        actor,
    ) -> bool:
        ingreso = equipo["ingreso"]
        etiqueta = (
            f"  {orden['order_number']}  {remito}  {equipo['device']['numero_serie']}  "
            f"OS {ingreso['id']} ({ingreso['estado']}) -> alquilado a {cliente or '-'}"
        )
        if not apply_changes:
            self.stdout.write(etiqueta)
            return True
        sincronizados = _sync_rental_ingresos_delivery(
            [int(ingreso["id"])],
            remito,
            actor,
            alquiler_a=cliente,
            alquiler_fecha=_mediodia_local(fecha_rta) if fecha_rta else None,
        )
        if not sincronizados:
            self.stdout.write(self.style.WARNING(etiqueta + "  [SALTEADO: estado no elegible]"))
            return False
        self.stdout.write(self.style.SUCCESS(etiqueta + "  [OK]"))
        return True

    # ------------------------------------------------------------------ fase ordenes
    def _fase_ordenes(self, filtro: list[str], apply_changes: bool, actor) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n[ordenes] La orden tiene el remito y el equipo vinculado, pero el equipo no quedo marcado"
            )
        )
        ordenes = self._ordenes_de_alquiler(filtro, con_equipo=True)
        pendientes = 0
        for orden in ordenes:
            fecha = orden["delivered_at"].date() if orden.get("delivered_at") else None
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT i.id, i.estado
                      FROM ingresos i
                     WHERE i.id IN (
                            SELECT o.ingreso_id FROM delivery_orders o WHERE o.id = %s AND o.ingreso_id IS NOT NULL
                            UNION
                            SELECT di.ingreso_id FROM delivery_order_items di
                             WHERE di.order_id = %s AND di.ingreso_id IS NOT NULL
                            UNION
                            SELECT dp.ingreso_id FROM delivery_order_item_partidas dp
                              JOIN delivery_order_items di ON di.id = dp.order_item_id
                             WHERE di.order_id = %s AND dp.ingreso_id IS NOT NULL
                           )
                       -- Le falta el marcado o le falta el evento. Lo segundo hace que una
                       -- corrida cortada a la mitad se arregle volviendo a correr: el
                       -- 08/09/2026 el UPDATE del equipo entró y el INSERT del evento
                       -- reventó, y sin esta condición el equipo ya no volvía a aparecer.
                       AND (
                            i.alquilado IS NOT TRUE
                         OR i.estado <> 'alquilado'
                         OR NOT EXISTS (
                              SELECT 1 FROM ingreso_events e
                               WHERE e.ticket_id = i.id AND e.a_estado = 'alquilado'
                            )
                       )
                       -- Misma regla que la fase `bejerman`: si el equipo reingreso despues
                       -- de la entrega, el alquiler ya termino y marcarlo hoy lo sacaria de
                       -- la estanteria estando en el taller.
                       AND NOT EXISTS (
                            SELECT 1 FROM ingresos p
                             WHERE p.device_id = i.device_id
                               AND p.id <> i.id
                               AND COALESCE(p.fecha_ingreso::date, p.fecha_creacion::date) > %s::date
                           )
                     ORDER BY i.id
                    """,
                    [orden["id"], orden["id"], orden["id"], fecha],
                )
                sin_marcar = _rows(cur)
            if not sin_marcar:
                continue
            for ingreso in sin_marcar:
                pendientes += 1
                self._aplicar(
                    orden,
                    {"device": {"numero_serie": "-"}, "ingreso": ingreso},
                    orden["remito_number"],
                    fecha,
                    (orden.get("customer_name") or "").strip(),
                    apply_changes,
                    actor,
                )
        self.stdout.write(f"  {len(ordenes)} ordenes revisadas, {pendientes} equipos sin marcar.")

    # ------------------------------------------------------------------ fase bejerman
    def _consultar_rta(self, numeros: set[int]) -> dict[tuple[str, str, int], list[dict]]:
        """Todos los RTA en una sola consulta por empresa.

        Una conexion SSH por remito es el patron que tumbo produccion el 27/07/2026: van
        todos los numeros en un solo lote y se reparten despues por (empresa, punto, numero).
        """
        from service.bejerman_delivery import REMITO_QUERY_COMPANY_KEYS, _remote_bejerman_sql_csv

        if not numeros:
            return {}
        sql = _SQL_RTA_POR_NUMERO.format(numeros=", ".join(str(n) for n in sorted(numeros)))
        emitidos: dict[tuple[str, str, int], list[dict]] = {}
        for company_key in REMITO_QUERY_COMPANY_KEYS:
            outcome: dict = {}
            filas = _remote_bejerman_sql_csv(sql, company_key=company_key, outcome=outcome, timeout_seconds=180)
            if outcome.get("status") != "success":
                self.stderr.write(
                    f"  {company_key}: no se pudo consultar Bejerman ({outcome.get('status')})."
                )
                continue
            self.stdout.write(f"  {company_key}: {len(filas)} renglones RTA con partida.")
            for fila in filas:
                try:
                    numero = int(str(fila.get("numero") or "0").strip())
                except ValueError:
                    continue
                punto = str(fila.get("punto") or "").strip().zfill(5)
                emitidos.setdefault((company_key, punto, numero), []).append(fila)
        return emitidos

    def _fase_bejerman(self, filtro: list[str], apply_changes: bool, actor) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n[bejerman] La orden tiene el remito y ningun equipo vinculado: el equipo lo dice el RTA"
            )
        )
        ordenes = self._ordenes_de_alquiler(filtro, con_equipo=False)
        if not ordenes:
            self.stdout.write("  No hay ordenes de alquiler sin equipo vinculado.")
            return

        documentos: dict[str, tuple[str, int]] = {}
        ilegibles: list[dict] = []
        for orden in ordenes:
            documento = _documento_de_orden(orden["remito_number"])
            if documento:
                documentos[orden["id"]] = documento
            else:
                ilegibles.append(orden)
        for orden in ilegibles:
            self.stdout.write(
                self.style.WARNING(
                    f"  {orden['order_number']}: no se puede leer el remito '{orden['remito_number']}'."
                )
            )

        emitidos = self._consultar_rta({numero for _, numero in documentos.values()})
        marcados = 0
        salteados: list[str] = []
        # Un mismo equipo en dos RTA sin baja en el medio no es un backfill, es una
        # devolucion sin registrar o un error de partida: se informa y no se toca.
        vistos: dict[str, str] = {}
        for orden in ordenes:
            documento = documentos.get(orden["id"])
            if not documento:
                continue
            punto, numero = documento
            company_key = "MGBIO" if punto == "00007" else (orden.get("company_key") or "SEPID")
            renglones = emitidos.get((company_key, punto, numero)) or []
            if not renglones:
                salteados.append(f"{orden['order_number']} ({orden['remito_number']}): el RTA no aparece en Bejerman")
                continue
            fecha_rta = _fecha_comprobante(renglones[0].get("fecha"))
            cliente = (renglones[0].get("cliente_nombre") or orden.get("customer_name") or "").strip()
            for renglon in renglones:
                serie = _serie_key(renglon.get("partida"))
                if not serie:
                    continue
                if serie in vistos and vistos[serie] != orden["order_number"]:
                    salteados.append(
                        f"{orden['order_number']} ({serie}): la misma partida ya salio en {vistos[serie]}"
                    )
                    continue
                vistos[serie] = orden["order_number"]
                equipo = self._equipo_de_partida(serie, fecha_rta)
                if equipo.get("motivo"):
                    salteados.append(f"{orden['order_number']} ({serie}): {equipo['motivo']}")
                    continue
                if self._aplicar(orden, equipo, orden["remito_number"], fecha_rta, cliente, apply_changes, actor):
                    marcados += 1

        self.stdout.write(f"\n  {len(ordenes)} ordenes sin equipo, {marcados} equipos marcados.")
        if salteados:
            self.stdout.write(self.style.WARNING(f"  {len(salteados)} renglones sin tocar:"))
            for linea in salteados:
                self.stdout.write(f"    - {linea}")
