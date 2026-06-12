from django.core.management.base import BaseCommand
from django.db import connection, transaction
import re


def _norm_code(code: str) -> str:
    s = (code or "").strip().upper()
    m = re.match(r"^(MG|NM|NV|CE)[^0-9]*(\d{1,6})$", s)
    if not m:
        raise ValueError("code debe ser MG/NM/NV/CE + número")
    pref, num = m.group(1), m.group(2)
    return f"{pref} {num[-4:].zfill(4)}"


def _ns_key(ns: str) -> str:
    s = (ns or "").strip().upper()
    return s.replace(" ", "").replace("-", "")


class Command(BaseCommand):
    help = "Asigna devices.numero_interno a un equipo por NS o device_id, opcionalmente liberando conflictos (steal)."

    def add_arguments(self, parser):
        parser.add_argument("--ns", default=None, help="Número de serie de fábrica del device (para buscar device_id)")
        parser.add_argument("--device-id", type=int, default=None, help="device_id objetivo (alternativa a --ns)")
        parser.add_argument("--code", required=True, help="Código interno a asignar (ej: 'MG 4722' o 'MG004722')")
        parser.add_argument("--steal", action="store_true", help="Si otro device tiene el mismo código, limpiar su numero_interno")

    def handle(self, *args, **opts):
        code = _norm_code(opts.get("code") or "")
        ns = opts.get("ns")
        device_id = opts.get("device_id")
        if not device_id and not ns:
            raise SystemExit("Debe indicar --ns o --device-id")

        # Resolver device_id por NS si fue provisto
        if not device_id and ns:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id FROM devices WHERE REPLACE(REPLACE(UPPER(numero_serie),' ','') ,'-','')=%s LIMIT 1",
                    [_ns_key(ns)],
                )
                row = cur.fetchone()
                if not row:
                    raise SystemExit(f"Device con NS {ns} no encontrado")
                device_id = int(row[0])

        # Detectar conflicto
        conflict_id = None
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM devices
                 WHERE id<>%s AND REPLACE(REPLACE(UPPER(numero_interno),' ','') ,'-','') = REPLACE(REPLACE(UPPER(%s),' ',''),'-','')
                 LIMIT 1
                """,
                [device_id, code],
            )
            row = cur.fetchone()
            conflict_id = int(row[0]) if row else None

        with transaction.atomic():
            with connection.cursor() as cur:
                if conflict_id:
                    if not opts.get("steal"):
                        raise SystemExit(f"Conflicto: code {code} ya asignado al device_id={conflict_id}. Use --steal para limpiar el otro.")
                    # Limpiar numero_interno del otro
                    cur.execute("UPDATE devices SET numero_interno=NULL WHERE id=%s", [conflict_id])
                # Asignar al target
                cur.execute("UPDATE devices SET numero_interno=%s WHERE id=%s", [code, device_id])

        self.stdout.write(f"OK: code='{code}' asignado a device_id={device_id}" + (f"; liberado conflict_id={conflict_id}" if conflict_id else ""))
