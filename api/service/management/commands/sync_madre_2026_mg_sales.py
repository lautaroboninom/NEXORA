import re
from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone


DEFAULT_WORKBOOK = r"Z:\MG BIO\MADRES\MADRE 2026.xlsx"
MG_RE = re.compile(r"^(MG|NM|NV|CE)\s*0*(\d{1,4})$", re.IGNORECASE)


def _norm_mg(value):
    raw = "" if value is None else str(value).strip().upper()
    match = MG_RE.match(raw)
    if not match:
        return ""
    return f"{match.group(1).upper()} {int(match.group(2)):04d}"


def _text(value):
    return "" if value is None else str(value).strip()


def _load_workbook(path):
    if not path.exists():
        raise CommandError(f"No existe el archivo: {path}")
    return openpyxl.load_workbook(path, data_only=True, read_only=True)


def _load_sales(wb):
    sheet_name = next((name for name in wb.sheetnames if name.strip().upper() == "ALTAS Y BAJAS"), None)
    if not sheet_name:
        raise CommandError("No existe la hoja 'ALTAS Y BAJAS'.")

    sales = {}
    ws = wb[sheet_name]
    for row_num, row in enumerate(ws.iter_rows(min_row=6, values_only=True), start=6):
        values = list(row[:14])
        code = _norm_mg(values[6] if len(values) > 6 else "")
        motivo = _text(values[10] if len(values) > 10 else "").upper()
        if not code or motivo != "VENTA":
            continue
        sales[code] = {
            "row": row_num,
            "serie": _text(values[5] if len(values) > 5 else ""),
            "fecha": values[9] if len(values) > 9 else None,
            "observaciones": _text(values[13] if len(values) > 13 else ""),
        }
    return sales


def _load_inventory_codes(wb):
    codes = {}
    for sheet_name in ("SEPID 2026", "POR MG"):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        for row_num, row in enumerate(ws.iter_rows(min_row=6, values_only=True), start=6):
            values = list(row[:17])
            code = _norm_mg(values[7] if len(values) > 7 else "")
            if not code or code in codes:
                continue
            codes[code] = {
                "sheet": sheet_name,
                "row": row_num,
                "serie": _text(values[6] if len(values) > 6 else ""),
            }
    return codes


def _find_customer_id(cur, hint):
    value = _text(hint)
    if not value:
        return None
    aliases = {
        "PUGLISI": "%PUGLISI%",
        "NOVAMED": "%NOVAMED%",
        "TMD": "%TMD%",
        "RESPILIFE": "%RESPILIFE%",
        "IBRAHIM": "%BRAHIM%",
    }
    upper = value.upper()
    pattern = None
    for key, candidate in aliases.items():
        if key in upper:
            pattern = candidate
            break
    if pattern is None:
        pattern = f"%{value}%"
    cur.execute(
        "SELECT id FROM customers WHERE razon_social ILIKE %s ORDER BY id LIMIT 1",
        [pattern],
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _fetch_device_rows(cur):
    cur.execute(
        """
        SELECT
          d.id,
          COALESCE(d.numero_interno, '') AS numero_interno,
          COALESCE(d.numero_serie, '') AS numero_serie,
          COALESCE(d.mg_estado, 'activo') AS mg_estado,
          COALESCE(c.razon_social, '') AS cliente
        FROM devices d
        LEFT JOIN customers c ON c.id = d.customer_id
        WHERE UPPER(TRIM(COALESCE(d.numero_interno, ''))) ~ '^(MG|NM|NV|CE)\\s*0*[0-9]{1,4}$'
        ORDER BY d.numero_interno, d.id
        """
    )
    by_code = {}
    for row in cur.fetchall():
        code = _norm_mg(row[1])
        by_code.setdefault(code, []).append(
            {
                "id": int(row[0]),
                "numero_interno": row[1],
                "numero_serie": row[2],
                "mg_estado": row[3],
                "cliente": row[4],
            }
        )
    return by_code


def _dash_location_id(cur):
    cur.execute("SELECT id FROM locations WHERE nombre='-' ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return int(row[0]) if row else None


class Command(BaseCommand):
    help = "Sincroniza estados de venta MG usando MADRE 2026.xlsx como fuente de control."

    def add_arguments(self, parser):
        parser.add_argument("--workbook", default=DEFAULT_WORKBOOK, help="Ruta al archivo MADRE 2026.xlsx.")
        parser.add_argument("--apply", action="store_true", help="Aplica las correcciones detectadas.")

    def handle(self, *args, **opts):
        workbook_path = Path(opts["workbook"])
        apply_changes = bool(opts.get("apply"))
        wb = _load_workbook(workbook_path)
        sales = _load_sales(wb)
        inventory = _load_inventory_codes(wb)

        with connection.cursor() as cur:
            db_by_code = _fetch_device_rows(cur)

            mark_sold = []
            reactivate = []
            missing = []

            for code, sale in sales.items():
                rows = db_by_code.get(code) or []
                if not rows:
                    missing.append((code, sale))
                    continue
                if any(row["mg_estado"] == "inactivo_venta" for row in rows):
                    continue
                exact = [row for row in rows if row["numero_serie"].strip().upper() == sale["serie"].strip().upper()]
                if exact:
                    buyer_id = _find_customer_id(cur, sale["observaciones"])
                    mark_sold.append((code, sale, exact[0], buyer_id))

            for code, rows in db_by_code.items():
                if code in sales or code not in inventory:
                    continue
                for row in rows:
                    if row["mg_estado"] == "inactivo_venta":
                        reactivate.append((code, row, inventory[code]))

            self.stdout.write(f"Ventas en Excel: {len(sales)}")
            self.stdout.write(f"Para marcar vendidos: {len(mark_sold)}")
            for code, sale, row, buyer_id in mark_sold:
                self.stdout.write(
                    f"- {code} device #{row['id']} serie {row['numero_serie']} "
                    f"fila {sale['row']} comprador '{sale['observaciones'] or '-'}' customer_id {buyer_id or '-'}"
                )

            self.stdout.write(f"Para reactivar por falso vendido: {len(reactivate)}")
            for code, row, inv in reactivate:
                self.stdout.write(
                    f"- {code} device #{row['id']} serie {row['numero_serie']} "
                    f"figura en {inv['sheet']} fila {inv['row']}"
                )

            self.stdout.write(f"Ventas Excel sin device encontrado: {len(missing)}")
            for code, sale in missing:
                self.stdout.write(f"- {code} fila {sale['row']} serie {sale['serie']}")

            if not apply_changes:
                self.stdout.write("Dry-run finalizado. Ejecutá con --apply para aplicar.")
                return

            now_ts = timezone.now()
            dash_id = _dash_location_id(cur)
            with transaction.atomic():
                for code, sale, row, buyer_id in mark_sold:
                    sale_date = sale["fecha"] or now_ts
                    obs = (
                        f"MADRE 2026 ALTAS Y BAJAS fila {sale['row']}: "
                        f"VENTA"
                    )
                    if sale["observaciones"]:
                        obs = f"{obs} - {sale['observaciones']}"
                    sets = [
                        "mg_estado = 'inactivo_venta'",
                        "mg_inactivo_desde = %s",
                        "mg_venta_fecha = %s",
                        "mg_venta_factura_numero = NULL",
                        "mg_venta_remito_numero = NULL",
                        "mg_venta_observaciones = %s",
                        "mg_venta_usuario_id = NULL",
                        "mg_venta_customer_id = %s",
                        "mg_venta_numero_alternativo = NULL",
                        "alquilado = FALSE",
                    ]
                    params = [sale_date, sale_date, obs, buyer_id]
                    if dash_id is not None:
                        sets.append("ubicacion_id = %s")
                        params.append(dash_id)
                    params.append(row["id"])
                    cur.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = %s", params)
                    cur.execute(
                        """
                        INSERT INTO device_mg_events(
                          device_id, accion, numero_interno_snapshot, fecha_evento,
                          factura_numero, remito_numero, observaciones, usuario_id, ingreso_id,
                          venta_customer_id, venta_numero_alternativo, source
                        )
                        VALUES (%s, 'venta', %s, %s, NULL, NULL, %s, NULL, NULL, %s, NULL, 'equipos')
                        """,
                        [row["id"], code, sale_date, obs, buyer_id],
                    )

                for code, row, inv in reactivate:
                    obs = (
                        f"Corrección MADRE 2026: figura vigente en {inv['sheet']} "
                        f"fila {inv['row']} y no está en ALTAS Y BAJAS como VENTA."
                    )
                    cur.execute(
                        """
                        UPDATE devices
                           SET mg_estado = 'activo',
                               mg_inactivo_desde = NULL,
                               mg_venta_fecha = NULL,
                               mg_venta_factura_numero = NULL,
                               mg_venta_remito_numero = NULL,
                               mg_venta_observaciones = NULL,
                               mg_venta_usuario_id = NULL,
                               mg_venta_customer_id = NULL,
                               mg_venta_numero_alternativo = NULL
                         WHERE id = %s
                        """,
                        [row["id"]],
                    )
                    cur.execute(
                        """
                        INSERT INTO device_mg_events(
                          device_id, accion, numero_interno_snapshot, fecha_evento,
                          factura_numero, remito_numero, observaciones, usuario_id, ingreso_id,
                          venta_customer_id, venta_numero_alternativo, source
                        )
                        VALUES (%s, 'reactivacion', %s, %s, NULL, NULL, %s, NULL, NULL, NULL, NULL, 'equipos')
                        """,
                        [row["id"], code, now_ts, obs],
                    )

        self.stdout.write(
            f"APLICADO OK: vendidos marcados {len(mark_sold)}, reactivados {len(reactivate)}, faltantes {len(missing)}."
        )
