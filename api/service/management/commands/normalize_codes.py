from django.core.management.base import BaseCommand
from django.db import connection, transaction
import re


PAT_NS_FIX = re.compile(r"^(MG|NM|NV|CE)[\s-]?(\d{1,4})$", re.IGNORECASE)
PAT_NUMINT_LONG = re.compile(r"^(MG|NM|NV|CE)\s*(\d{5,})$", re.IGNORECASE)
PAT_NUMINT_OK = re.compile(r"^(MG|NM|NV|CE)\s\d{4}$", re.IGNORECASE)


def _norm4(n: str) -> str:
    n = re.sub(r"\D", "", n or "")
    if not n:
        return "0000"
    return n[-4:].zfill(4)


def _normkey(s: str) -> str:
    return (s or "").strip().upper().replace(" ", "").replace("-", "")


class Command(BaseCommand):
    help = (
        "Normaliza codigos: (1) devices.numero_serie a 'XX ####' cuando falte el espacio o tenga NV0318/MG1847; "
        "(2) devices.numero_interno con 5-6 digitos a 'XX ####' usando los ultimos 4. Reporta pendientes/conflictos."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica cambios (default: dry-run)")

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))

        fixed_ns = []
        fixed_numint = []
        pending_numint = []
        conflicts_numint = []

        # 1) Normalizar numero_serie a 'XX ####' cuando sea 'XX####' o 'XX-####' o 'XX ###'
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, numero_serie
                  FROM devices
                 WHERE numero_serie ~* '^(MG|NM|NV|CE)'
                """
            )
            rows = cur.fetchall() or []
        ns_conflicts = []
        for rid, ns in rows:
            s = (ns or "").strip()
            m = PAT_NS_FIX.match(s)
            if not m:
                continue
            pref, num = m.group(1).upper(), _norm4(m.group(2))
            desired = f"{pref} {num}"
            if s == desired:
                continue
            # conflict?
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM devices
                     WHERE id<>%s AND REPLACE(REPLACE(UPPER(numero_serie),' ','') ,'-','') = %s
                     LIMIT 1
                    """,
                    [rid, _normkey(desired)],
                )
                rowc = cur.fetchone()
            if rowc:
                ns_conflicts.append((rid, s, desired, int(rowc[0])))
                continue
            fixed_ns.append((rid, desired, s))
        if apply and fixed_ns:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.executemany(
                        "UPDATE devices SET numero_serie=%s WHERE id=%s",
                        [(new, rid) for rid, new, _old in fixed_ns],
                    )

        # 2) Normalizar numero_interno de 'XX d{5,}' a 'XX ####' (ultimos 4)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, numero_interno
                  FROM devices
                 WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{5,}$'
                """
            )
            rows2 = cur.fetchall() or []
        for rid, ni in rows2:
            s = (ni or "").strip()
            m = PAT_NUMINT_LONG.match(s)
            if not m:
                pending_numint.append((rid, s, "no_match"))
                continue
            pref, digits = m.group(1).upper(), m.group(2)
            desired = f"{pref} {_norm4(digits)}"
            # conflict?
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM devices
                     WHERE id<>%s AND REPLACE(REPLACE(UPPER(numero_interno),' ','') ,'-','') = %s
                     LIMIT 1
                    """,
                    [rid, _normkey(desired)],
                )
                rowc = cur.fetchone()
            if rowc:
                conflicts_numint.append((rid, s, desired, int(rowc[0])))
                continue
            if s != desired:
                fixed_numint.append((rid, desired, s))
        if apply and fixed_numint:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.executemany(
                        "UPDATE devices SET numero_interno=%s WHERE id=%s",
                        [(new, rid) for rid, new, _old in fixed_numint],
                    )

        # 3) Reporte de pendientes de numero_interno que aun no cumplen 'XX ####'
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, numero_interno
                  FROM devices
                 WHERE numero_interno IS NOT NULL
                   AND NOT (numero_interno ~* '^(MG|NM|NV|CE)\s\d{4}$')
                 ORDER BY id DESC
                 LIMIT 50
                """
            )
            still = cur.fetchall() or []

        # Output resumen
        self.stdout.write(f"NS normalizados: {len(fixed_ns)}" + (" (aplicados)" if apply else " (dry-run)"))
        if ns_conflicts:
            self.stdout.write(f"Conflictos numero_serie: {len(ns_conflicts)}")
            for rid, oldv, want, other in ns_conflicts[:50]:
                self.stdout.write(f"  device_id={rid} '{oldv}' -> '{want}' conflict_with_device_id={other}")
        self.stdout.write(f"Números internos normalizados (>=5 dígitos -> ####): {len(fixed_numint)}" + (" (aplicados)" if apply else " (dry-run)"))
        self.stdout.write(f"Conflictos numero_interno: {len(conflicts_numint)}")
        if conflicts_numint:
            for rid, oldv, want, other in conflicts_numint[:50]:
                self.stdout.write(f"  device_id={rid} '{oldv}' -> '{want}' conflict_with_device_id={other}")
        self.stdout.write(f"Pendientes numero_interno (no 'XX ####'): {len(still)} (muestra)")
        for rid, val in still:
            self.stdout.write(f"  id={rid} numero_interno='{(val or '').strip()}'")
