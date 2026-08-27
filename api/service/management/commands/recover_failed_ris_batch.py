from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from service.bejerman_documents import parse_bejerman_remito_number
from service.bejerman_remito_pdf_data import comprobante_from_emission
from service.bejerman_ris import _build_batch_payload, _remote_ris_duplicate
from service.bejerman_sdk import BejermanSDKClient


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, cls=DjangoJSONEncoder)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_dict(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([column[0] for column in cur.description], row))


def _sequence_state(cur, table: str) -> dict[str, Any]:
    cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
    sequence = cur.fetchone()[0]
    if not sequence:
        raise CommandError(f"La tabla {table} no tiene una secuencia de ID")
    cur.execute(f"SELECT last_value, is_called FROM {sequence}")
    last_value, is_called = cur.fetchone()
    cur.execute(f"SELECT COALESCE(MAX(id), 0), COUNT(*) FROM {table}")
    max_id, count = cur.fetchone()
    return {
        "sequence": sequence,
        "last_value": int(last_value),
        "is_called": bool(is_called),
        "max_id": int(max_id),
        "count": int(count),
    }


def _normalized_serial(value: Any) -> str:
    return _clean(value).replace(" ", "").replace("-", "").upper()


def _physical_span(comprobante: dict[str, Any]) -> int:
    raw_items = comprobante.get("Comprobante_Items")
    items = raw_items if isinstance(raw_items, list) else []
    highest = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            highest = max(highest, index)
            continue
        try:
            row_number = int(item.get("Item_NumeroRenglon") or index)
        except (TypeError, ValueError):
            row_number = index
        highest = max(highest, row_number)
    return max(len(items), highest)


class Command(BaseCommand):
    help = (
        "Reconstruye localmente un lote RIS cuya emisión Bejerman quedó confirmada "
        "pero cuya transacción NEXORA fue revertida. Es dry-run salvo --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--audit-log-id", type=int, required=True)
        parser.add_argument("--remito", required=True)
        parser.add_argument("--bejerman-id", type=int, required=True)
        parser.add_argument("--first-ingreso-id", type=int, required=True)
        parser.add_argument("--first-new-device-id", type=int, required=True)
        parser.add_argument("--first-ris-row-id", type=int, required=True)
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--incident-at", required=True, help="Fecha y hora ISO-8601 con zona horaria")
        parser.add_argument("--expected-lines", type=int, default=37)
        parser.add_argument(
            "--verify-transaction",
            action="store_true",
            help="Ejecuta todos los inserts y postchecks, pero revierte la transacción al final.",
        )
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        self._assert_safe_environment()
        incident_at = self._parse_incident_at(options["incident_at"])
        document = self._parse_document(options["remito"])
        body = self._load_audit_body(options["audit_log_id"])
        plan = self._build_plan(
            body,
            audit_log_id=options["audit_log_id"],
            first_ingreso_id=options["first_ingreso_id"],
            first_new_device_id=options["first_new_device_id"],
            first_ris_row_id=options["first_ris_row_id"],
            user_id=options["user_id"],
        )
        remote = self._load_remote_document(
            document,
            user_id=options["user_id"],
            expected_bejerman_id=options["bejerman_id"],
            expected_lines=options["expected_lines"],
            expected_partidas=plan["serials"],
        )
        plan["document"] = document
        plan["incident_at"] = incident_at
        plan["remote"] = remote

        recovered = self._already_recovered(plan)
        self._print_plan(plan, recovered=recovered)
        if recovered:
            self.stdout.write(self.style.SUCCESS("El lote ya estaba reconstruido exactamente; no se modificó nada."))
            return
        if options["apply"] and options["verify_transaction"]:
            raise CommandError("Usá --apply o --verify-transaction, no ambos")
        if options["verify_transaction"]:
            self._apply(plan, rollback=True)
            self.stdout.write(self.style.SUCCESS("Transacción completa verificada y revertida; no se persistió nada."))
            return
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry-run finalizado. Repetí el comando con --apply para persistir."))
            return

        self._apply(plan)
        self.stdout.write(
            self.style.SUCCESS(
                f"Recuperación aplicada: OS {plan['ingreso_ids'][0]:05d}–{plan['ingreso_ids'][-1]:05d} "
                f"asociadas a {document['remitoNumber']}."
            )
        )

    def _assert_safe_environment(self) -> None:
        database_name = _clean(connection.settings_dict.get("NAME"))
        wsdl_url = _clean(getattr(settings, "BEJERMAN_WSDL_URL", ""))
        if not settings.DEBUG:
            raise CommandError("Este comando sólo puede ejecutarse con DEBUG activo en DEV")
        if "dev" not in database_name.lower():
            raise CommandError(f"Base no permitida para esta recuperación: {database_name}")
        if "test" not in wsdl_url.lower():
            raise CommandError("El WSDL configurado no es el entorno TEST de Bejerman")

    def _parse_incident_at(self, value: str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise CommandError("--incident-at debe ser una fecha y hora ISO-8601 válida")
        if timezone.is_naive(parsed):
            raise CommandError("--incident-at debe incluir zona horaria")
        return parsed

    def _parse_document(self, value: str) -> dict[str, str]:
        try:
            document = parse_bejerman_remito_number(
                value,
                default_type="RIS",
                default_letter="R",
                allowed_types={"RIS"},
                require_complete=True,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if document["point"] != "00004":
            raise CommandError("Esta recuperación exige el punto digital TEST 00004")
        return document

    def _load_audit_body(self, audit_log_id: int) -> dict[str, Any]:
        with connection.cursor() as cur:
            cur.execute("SELECT method, path, body FROM audit_log WHERE id=%s", [audit_log_id])
            row = cur.fetchone()
        if not row:
            raise CommandError(f"No existe audit_log {audit_log_id}")
        method, path, body = row
        if method != "POST" or path != "/api/ingresos/nuevo/lote/":
            raise CommandError("La auditoría no corresponde al alta de un lote RIS")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise CommandError("El body auditado no contiene JSON válido") from exc
        if not isinstance(body, dict):
            raise CommandError("El body auditado no es un objeto")
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise CommandError("El body auditado no contiene equipos")
        return body

    def _build_plan(
        self,
        body: dict[str, Any],
        *,
        audit_log_id: int,
        first_ingreso_id: int,
        first_new_device_id: int,
        first_ris_row_id: int,
        user_id: int,
    ) -> dict[str, Any]:
        items = body.get("items") or []
        customer = body.get("cliente") if isinstance(body.get("cliente"), dict) else {}
        customer_id = int(customer.get("id") or 0)
        if customer_id <= 0:
            raise CommandError("El lote auditado no tiene cliente por ID")
        company = _clean(body.get("empresa_bejerman") or body.get("empresa_facturar") or "SEPID").upper()
        if company != "SEPID":
            raise CommandError(f"Empresa inesperada para esta recuperación: {company}")
        if _clean(body.get("ris_mode") or body.get("document_mode") or "emit").lower() != "emit":
            raise CommandError("La auditoría no corresponde a una emisión digital")
        propietario = body.get("propietario") if isinstance(body.get("propietario"), dict) else {}
        if any(_clean(propietario.get(key)) for key in ("nombre", "contacto", "doc")):
            raise CommandError("Esta recuperación esperaba un propietario vacío")

        serials: list[str] = []
        equipment_rows: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise CommandError(f"El equipo {index + 1} no es un objeto")
            equipment = item.get("equipo") if isinstance(item.get("equipo"), dict) else {}
            serial = _clean(equipment.get("numero_serie"))
            if not serial:
                raise CommandError(f"El equipo {index + 1} no tiene número de serie")
            if _normalized_serial(serial) in {_normalized_serial(value) for value in serials}:
                raise CommandError(f"Serie repetida en el lote: {serial}")
            marca_id = int(equipment.get("marca_id") or 0)
            modelo_id = int(equipment.get("modelo_id") or 0)
            if marca_id <= 0 or modelo_id <= 0:
                raise CommandError(f"El equipo {index + 1} no tiene marca/modelo")
            serials.append(serial)
            equipment_rows.append(
                {
                    "serial": serial,
                    "marca_id": marca_id,
                    "modelo_id": modelo_id,
                    "motivo": _clean(item.get("motivo")),
                    "garantia_reparacion": bool(item.get("garantia_reparacion")),
                    "etiq_garantia_ok": bool(item.get("etiq_garantia_ok")),
                    "informe_preliminar": _clean(item.get("informe_preliminar")),
                    "comentarios": _clean(item.get("comentarios")) or None,
                    "equipo_variante": _clean(item.get("equipo_variante")) or None,
                    "accesorios_items": item.get("accesorios_items") or [],
                }
            )
        if any(row["accesorios_items"] for row in equipment_rows):
            raise CommandError("Esta recuperación segura sólo admite el lote auditado sin accesorios")
        catalog_pairs = {(row["marca_id"], row["modelo_id"]) for row in equipment_rows}
        if len(catalog_pairs) != 1:
            raise CommandError("Todos los equipos deben compartir marca y modelo")

        ingreso_ids = list(range(first_ingreso_id, first_ingreso_id + len(equipment_rows)))
        ris_row_ids = list(range(first_ris_row_id, first_ris_row_id + len(equipment_rows)))
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, numero_serie
                  FROM devices
                 WHERE REPLACE(REPLACE(UPPER(numero_serie), ' ', ''), '-', '') = ANY(%s)
                 ORDER BY id
                """,
                [[_normalized_serial(serial) for serial in serials]],
            )
            found = {_normalized_serial(serial): int(device_id) for device_id, serial in cur.fetchall()}

        historical_ids = {device_id for device_id in found.values() if device_id < first_new_device_id}
        expected_new_count = len(serials) - len(historical_ids)
        new_device_ids = list(range(first_new_device_id, first_new_device_id + expected_new_count))
        occupied_target_ids = {device_id for device_id in found.values() if device_id in new_device_ids}
        available_target_ids = iter([device_id for device_id in new_device_ids if device_id not in occupied_target_ids])
        device_ids: list[int] = []
        missing_serials: list[str] = []
        for serial in serials:
            existing_id = found.get(_normalized_serial(serial))
            if existing_id is not None:
                if existing_id >= first_new_device_id and existing_id not in new_device_ids:
                    raise CommandError(f"El device existente de {serial} quedó fuera del rango recuperable: {existing_id}")
                device_ids.append(existing_id)
                continue
            try:
                device_ids.append(next(available_target_ids))
            except StopIteration as exc:
                raise CommandError("No alcanzan los IDs reservados para reconstruir devices") from exc
            missing_serials.append(serial)

        return {
            "audit_log_id": audit_log_id,
            "body": body,
            "items": equipment_rows,
            "serials": serials,
            "customer_id": customer_id,
            "company": company,
            "user_id": user_id,
            "ingreso_ids": ingreso_ids,
            "ris_row_ids": ris_row_ids,
            "device_ids": device_ids,
            "new_device_ids": new_device_ids,
            "missing_serials": missing_serials,
            "first_ingreso_id": first_ingreso_id,
            "first_new_device_id": first_new_device_id,
            "first_ris_row_id": first_ris_row_id,
        }

    def _load_remote_document(
        self,
        document: dict[str, str],
        *,
        user_id: int,
        expected_bejerman_id: int,
        expected_lines: int,
        expected_partidas: list[str],
    ) -> dict[str, Any]:
        client = BejermanSDKClient(company_key="SEPID", actor_user_id=user_id)
        found = _remote_ris_duplicate(document, "SEPID", client)
        if not found:
            raise CommandError(f"Bejerman TEST no contiene {document['remitoNumber']}")
        raw = found.get("raw") if isinstance(found.get("raw"), dict) else {}
        try:
            remote_id = int(raw.get("Comprobante_ID") or 0)
        except (TypeError, ValueError):
            remote_id = 0
        if remote_id != expected_bejerman_id:
            raise CommandError(f"ID Bejerman inesperado: {remote_id}; se esperaba {expected_bejerman_id}")
        comprobante = comprobante_from_emission(raw) or raw
        span = _physical_span(comprobante)
        if span != expected_lines:
            raise CommandError(f"El comprobante remoto ocupa {span} renglones; se esperaban {expected_lines}")
        raw_items = comprobante.get("Comprobante_Items")
        items = raw_items if isinstance(raw_items, list) else []
        partidas = [
            _clean(item.get("Item_Partida"))
            for item in items
            if isinstance(item, dict) and _clean(item.get("Item_Tipo")).upper() == "A"
        ]
        if partidas != expected_partidas:
            raise CommandError(f"Las partidas remotas no coinciden: {partidas}")
        if _clean(comprobante.get("Cliente_Codigo") or raw.get("Cliente_Codigo")).upper() != "NOV":
            raise CommandError("El RIS remoto no pertenece al cliente NOV")
        if _clean(comprobante.get("Comprobante_ActualizaStock") or raw.get("Comprobante_ActualizaStock")).upper() != "N":
            raise CommandError("El RIS remoto no refleja la recuperación de partidas con stock N")
        return {"raw": raw, "comprobante": comprobante, "span": span, "id": remote_id}

    def _already_recovered(self, plan: dict[str, Any]) -> bool:
        ingreso_ids = plan["ingreso_ids"]
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.device_id, d.numero_serie, i.remito_ingreso,
                       r.status, r.pdf_status, r.document_mode, r.remito_number,
                       r.id, r.created_by, r.request_payload
                  FROM ingresos i
                  JOIN devices d ON d.id=i.device_id
                  LEFT JOIN bejerman_ingreso_remitos r ON r.ingreso_id=i.id
                 WHERE i.id = ANY(%s)
                 ORDER BY i.id
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
        if not rows:
            return False
        if len(rows) != len(ingreso_ids):
            raise CommandError("Existe una recuperación parcial en los IDs de ingreso objetivo")
        for index, row in enumerate(rows):
            request_payload = _json_object(row[10])
            expected_prefix = (
                ingreso_ids[index], plan["device_ids"][index], plan["serials"][index],
                plan["document"]["remitoNumber"], "generated",
            )
            if (
                row[:5] != expected_prefix
                or row[5] not in {"pending", "ready", "failed"}
                or row[6] != "emit"
                or row[7] != plan["document"]["remitoNumber"]
                or row[8] != plan["ris_row_ids"][index]
                or row[9] != plan["user_id"]
                or request_payload.get("ingresoIds") != ingreso_ids
            ):
                raise CommandError(f"La fila recuperada no coincide para OS {ingreso_ids[index]:05d}: {row}")
        return True

    def _print_plan(self, plan: dict[str, Any], *, recovered: bool) -> None:
        self.stdout.write(
            json.dumps(
                {
                    "database": connection.settings_dict.get("NAME"),
                    "remito": plan["document"]["remitoNumber"],
                    "bejerman_id": plan["remote"]["id"],
                    "line_count": plan["remote"]["span"],
                    "ingreso_ids": plan["ingreso_ids"],
                    "device_ids": plan["device_ids"],
                    "new_device_ids": plan["new_device_ids"],
                    "serials": plan["serials"],
                    "user_id": plan["user_id"],
                    "incident_at": plan["incident_at"].isoformat(),
                    "already_recovered": recovered,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def _assert_preconditions_locked(self, cur, plan: dict[str, Any]) -> None:
        expected_sequences = {
            "devices": plan["new_device_ids"][-1],
            "ingresos": plan["ingreso_ids"][-1],
            "bejerman_ingreso_remitos": plan["ris_row_ids"][-1],
        }
        for table, expected_last in expected_sequences.items():
            state = _sequence_state(cur, table)
            if state["last_value"] != expected_last or not state["is_called"]:
                raise CommandError(f"Secuencia inesperada para {table}: {state}")

        for table, ids in (
            ("devices", plan["new_device_ids"]),
            ("ingresos", plan["ingreso_ids"]),
            ("bejerman_ingreso_remitos", plan["ris_row_ids"]),
        ):
            cur.execute(f"SELECT id FROM {table} WHERE id=ANY(%s) ORDER BY id", [ids])
            occupied = [int(row[0]) for row in cur.fetchall()]
            if occupied:
                raise CommandError(f"IDs ocupados en {table}: {occupied}")

        cur.execute(
            """
            SELECT id, numero_serie
              FROM devices
             WHERE id=ANY(%s)
             ORDER BY id
            """,
            [[device_id for device_id in plan["device_ids"] if device_id not in plan["new_device_ids"]]],
        )
        existing = {int(device_id): _clean(serial) for device_id, serial in cur.fetchall()}
        for device_id, serial in zip(plan["device_ids"], plan["serials"]):
            if device_id in plan["new_device_ids"]:
                continue
            if _normalized_serial(existing.get(device_id)) != _normalized_serial(serial):
                raise CommandError(f"El device {device_id} ya no corresponde a {serial}")

        cur.execute(
            """
            SELECT d.numero_serie, i.id
              FROM ingresos i
              JOIN devices d ON d.id=i.device_id
             WHERE REPLACE(REPLACE(UPPER(d.numero_serie), ' ', ''), '-', '') = ANY(%s)
               AND i.estado NOT IN ('entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
            """,
            [[_normalized_serial(serial) for serial in plan["serials"]]],
        )
        open_rows = cur.fetchall()
        if open_rows:
            raise CommandError(f"Aparecieron ingresos abiertos para el lote: {open_rows}")

        cur.execute("SELECT id, cod_empresa, razon_social FROM customers WHERE id=%s", [plan["customer_id"]])
        customer = cur.fetchone()
        if not customer or _clean(customer[1]).upper() != "NOV":
            raise CommandError(f"Cliente inesperado: {customer}")
        cur.execute("SELECT id FROM users WHERE id=%s AND activo=true", [plan["user_id"]])
        if not cur.fetchone():
            raise CommandError("El usuario de recuperación no existe o está inactivo")
        cur.execute(
            """
            SELECT m.id, mo.id, m.tecnico_id, mo.tecnico_id
              FROM marcas m
              JOIN models mo ON mo.marca_id=m.id
             WHERE m.id=%s AND mo.id=%s
            """,
            [plan["items"][0]["marca_id"], plan["items"][0]["modelo_id"]],
        )
        catalog = cur.fetchone()
        if not catalog or catalog[2] is not None or catalog[3] is not None:
            raise CommandError(f"Marca/modelo o técnico predeterminado inesperado: {catalog}")
        cur.execute("SELECT id FROM locations WHERE id=1 AND LOWER(nombre)='taller'")
        if not cur.fetchone():
            raise CommandError("La ubicación Taller id=1 no está disponible")

    def _apply(self, plan: dict[str, Any], *, rollback: bool = False) -> None:
        incident_at = plan["incident_at"]
        document = plan["document"]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                cur.execute("SELECT set_config('app.user_id', %s, true), set_config('app.user_role', 'jefe', true)", [str(plan["user_id"])])
                cur.execute(
                    "LOCK TABLE devices, ingresos, ingreso_accesorios, bejerman_ingreso_remitos "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
                self._assert_preconditions_locked(cur, plan)

                existing_ids = [device_id for device_id in plan["device_ids"] if device_id not in plan["new_device_ids"]]
                cur.execute(
                    "UPDATE devices SET customer_id=%s, marca_id=%s, model_id=%s, garantia_vence=NULL WHERE id=ANY(%s)",
                    [
                        plan["customer_id"],
                        plan["items"][0]["marca_id"],
                        plan["items"][0]["modelo_id"],
                        existing_ids,
                    ],
                )
                if cur.rowcount != len(existing_ids):
                    raise CommandError("No se actualizaron todos los devices históricos")

                new_rows = [
                    (
                        device_id,
                        plan["customer_id"],
                        item["marca_id"],
                        item["modelo_id"],
                        item["serial"],
                    )
                    for device_id, item in zip(plan["device_ids"], plan["items"])
                    if device_id in plan["new_device_ids"]
                ]
                cur.executemany(
                    """
                    INSERT INTO devices (
                      id, customer_id, marca_id, model_id, numero_serie, numero_interno,
                      mg_estado, garantia_vence, alquilado
                    ) OVERRIDING SYSTEM VALUE
                    VALUES (%s,%s,%s,%s,%s,NULL,'activo',NULL,FALSE)
                    """,
                    new_rows,
                )

                ingreso_rows = []
                for ingreso_id, device_id, item in zip(plan["ingreso_ids"], plan["device_ids"], plan["items"]):
                    ingreso_rows.append(
                        (
                            ingreso_id,
                            device_id,
                            item["motivo"],
                            incident_at,
                            incident_at,
                            item["informe_preliminar"],
                            "",
                            item["equipo_variante"],
                            document["remitoNumber"],
                            plan["user_id"],
                            item["comentarios"],
                            item["garantia_reparacion"],
                            item["etiq_garantia_ok"],
                        )
                    )
                cur.executemany(
                    """
                    INSERT INTO ingresos (
                      id, device_id, estado, motivo, empresa_bejerman, empresa_facturar,
                      permite_reparacion, fecha_ingreso, fecha_creacion, ubicacion_id,
                      disposicion, informe_preliminar, accesorios, equipo_variante,
                      remito_ingreso, recibido_por, comentarios, garantia_reparacion,
                      garantia_fabrica, etiq_garantia_ok, presupuesto_estado, asignado_a,
                      alquilado, propietario_nombre, propietario_contacto, propietario_doc
                    ) OVERRIDING SYSTEM VALUE
                    VALUES (
                      %s,%s,'ingresado',%s,'SEPID','SEPID',TRUE,%s,%s,1,
                      'normal',%s,%s,%s,%s,%s,%s,%s,
                      NULL,%s,'pendiente',NULL,NULL,NULL,NULL,NULL
                    )
                    """,
                    ingreso_rows,
                )

                payload = _build_batch_payload(plan["ingreso_ids"])
                payload = {
                    **payload,
                    "documentMode": "emit",
                    "operation": _clean(getattr(settings, "BEJERMAN_RIS_OPERATION", "IngresarComprobanteJSON")) or "IngresarComprobanteJSON",
                    "numeraFlex": _clean(getattr(settings, "BEJERMAN_RIS_NUMERA_FLEX", "S")) or "S",
                    "emiteReg": _clean(getattr(settings, "BEJERMAN_RIS_EMITE_REG", "E")) or "E",
                    "partidaRejectionRecovered": True,
                    "existingBejermanRemito": True,
                    "recoveredExistingBejermanRemito": True,
                    "recoveryAuditLogId": plan["audit_log_id"],
                    "comprobante": plan["remote"]["comprobante"],
                }
                response = {
                    "success": True,
                    "requestId": payload["requestId"],
                    "documentMode": "emit",
                    "companyKey": payload["companyKey"],
                    "companyLabel": payload["companyLabel"],
                    "bejermanCompany": payload["bejermanCompany"],
                    "issueDate": incident_at.date().isoformat(),
                    "remitoNumber": document["remitoNumber"],
                    "response": {
                        "comprobanteTipo": document["type"],
                        "comprobanteLetra": document["letter"],
                        "comprobantePtoVenta": document["point"],
                        "comprobanteNumero": document["number"],
                        "remitoNumber": document["remitoNumber"],
                    },
                    "profile": payload.get("documentProfile") or {},
                    "lineCount": plan["remote"]["span"],
                    "raw": plan["remote"]["raw"],
                    "recoveredExistingBejermanRemito": True,
                    "recoveryAuditLogId": plan["audit_log_id"],
                    "bejermanId": plan["remote"]["id"],
                }
                request_json = _json_text(payload)
                response_json = _json_text(response)
                ris_rows = []
                for ris_row_id, ingreso_id in zip(plan["ris_row_ids"], plan["ingreso_ids"]):
                    ris_rows.append(
                        (
                            ris_row_id, ingreso_id, request_json, response_json,
                            document["type"], document["letter"], document["point"],
                            document["number"], document["remitoNumber"],
                            payload["companyKey"], payload["companyLabel"], payload["bejermanCompany"],
                            payload["customerCode"], payload["customerName"], incident_at.date(),
                            incident_at, plan["user_id"], incident_at, incident_at,
                        )
                    )
                cur.executemany(
                    """
                    INSERT INTO bejerman_ingreso_remitos (
                      id, ingreso_id, status, pdf_status, document_mode,
                      manual_remito_number, attempts, last_error,
                      request_payload, response_payload,
                      comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                      comprobante_numero, remito_number,
                      company_key, company_label, bejerman_company,
                      customer_code, customer_name, issue_date,
                      generated_at, created_by, created_at, updated_at
                    ) OVERRIDING SYSTEM VALUE
                    VALUES (
                      %s,%s,'generated','pending','emit',NULL,1,NULL,
                      %s::jsonb,%s::jsonb,
                      %s,%s,%s,%s,%s,
                      %s,%s,%s,%s,%s,%s,
                      %s,%s,%s,%s
                    )
                    """,
                    ris_rows,
                )

                cur.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT i.device_id),
                           COUNT(*) FILTER (WHERE r.status='generated' AND r.document_mode='emit'
                                            AND r.remito_number=%s)
                      FROM ingresos i
                      JOIN bejerman_ingreso_remitos r ON r.ingreso_id=i.id
                     WHERE i.id=ANY(%s)
                    """,
                    [document["remitoNumber"], plan["ingreso_ids"]],
                )
                total, devices, associated = cur.fetchone()
                if (int(total), int(devices), int(associated)) != (
                    len(plan["ingreso_ids"]),
                    len(plan["ingreso_ids"]),
                    len(plan["ingreso_ids"]),
                ):
                    raise CommandError(
                        f"Falló el postcheck de recuperación: total={total}, devices={devices}, asociados={associated}"
                    )
                for table, expected_last in (
                    ("devices", plan["new_device_ids"][-1]),
                    ("ingresos", plan["ingreso_ids"][-1]),
                    ("bejerman_ingreso_remitos", plan["ris_row_ids"][-1]),
                ):
                    state = _sequence_state(cur, table)
                    if state["last_value"] != expected_last or not state["is_called"]:
                        raise CommandError(f"La secuencia de {table} cambió durante la recuperación: {state}")
                if rollback:
                    transaction.set_rollback(True)
