from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

from service import delivery_orders
from service.bejerman_rental_remito import (
    execute_rental_remito_action,
    get_rental_remito_state,
    normalize_rental_remito_number,
    save_rental_remito,
)
from service.models import User


class RentalRemitoNumberTests(SimpleTestCase):
    def test_rejects_short_number(self):
        with self.assertRaisesRegex(Exception, "remito completo"):
            normalize_rental_remito_number("4587")

    def test_accepts_point_and_number(self):
        parsed = normalize_rental_remito_number("00004-00004587")

        self.assertEqual(parsed.remito_number, "RTA R 00004-00004587")
        self.assertEqual(parsed.company_key, "SEPID")
        self.assertEqual(parsed.document_mode, "existing")

    def test_accepts_explicit_rta(self):
        parsed = normalize_rental_remito_number("RTA R 00007-4587")

        self.assertEqual(parsed.remito_number, "RTA R 00007-00004587")
        self.assertEqual(parsed.company_key, "MGBIO")


class RentalDeliveryItemValidationTests(SimpleTestCase):
    """La UI manda el NS en las filas de partida y deja el renglón con partida/depósito/stock en
    null: validar sólo el nivel renglón hacía fallar toda alta de alquiler con RENTAL_STL_REQUIRED."""

    SHELF_CONTEXT = {
        "ingreso_id": 11,
        "device_id": 7,
        "estado": "liberado",
        "alquilado": False,
        "ubicacion_nombre": "Estantería de Alquiler",
        "equipment_serial": "MZJ5S121199",
    }

    def _validate(self, items):
        with (
            patch.object(delivery_orders, "rental_equipment_context", return_value=dict(self.SHELF_CONTEXT)),
            patch.object(delivery_orders, "_rental_duplicate_reservation", return_value=None),
        ):
            return delivery_orders.validate_rental_delivery_items(items)

    def _item(self, **overrides):
        partida_row = {
            "ingresoId": 11,
            "deviceId": 7,
            "partida": "MZJ5S121199",
            "assignedQuantity": Decimal("1"),
            "stockDepositCode": "STL",
            "stockAvailableQuantity": Decimal("1"),
        }
        partida_row.update(overrides.pop("partida_row", {}))
        item = {
            "ingresoId": 11,
            "deviceId": 7,
            "articleCode": "1115003",
            "quantity": Decimal("1"),
            "partida": None,
            "stockDepositCode": None,
            "stockAvailableQuantity": None,
            "partidas": [partida_row],
        }
        item.update(overrides)
        return item

    def test_acepta_el_ns_cargado_en_las_filas_de_partida(self):
        contexts = self._validate([self._item()])

        self.assertEqual([context["ingreso_id"] for context in contexts], [11])

    def test_sigue_aceptando_el_formato_viejo_a_nivel_renglon(self):
        legacy = self._item(
            partidas=[],
            partida="MZJ5S121199",
            stockDepositCode="STL",
            stockAvailableQuantity=Decimal("1"),
        )

        contexts = self._validate([legacy])

        self.assertEqual([context["ingreso_id"] for context in contexts], [11])

    def test_rechaza_una_fila_que_no_sale_de_stl(self):
        with self.assertRaisesRegex(Exception, "depósito STL"):
            self._validate([self._item(partida_row={"stockDepositCode": "VAL"})])

    def test_acepta_un_equipo_con_mg_y_sin_os(self):
        # Alquilar no exige ingreso: el equipo nuevo se da de alta con su MG y la orden
        # lo toma por device, con ingreso_id en NULL.
        device_context = {
            **self.SHELF_CONTEXT,
            "ingreso_id": None,
            "estado": "",
            "mg_estado": "activo",
            "equipment_internal_number": "MG 9001",
        }
        item = self._item(ingresoId=None, partida_row={"ingresoId": None})

        with (
            patch.object(delivery_orders, "rental_device_context", return_value=dict(device_context)),
            patch.object(delivery_orders, "_rental_duplicate_reservation", return_value=None),
        ):
            contexts = delivery_orders.validate_rental_delivery_items([item])

        self.assertEqual([context["device_id"] for context in contexts], [7])
        self.assertEqual([context["ingreso_id"] for context in contexts], [None])

    def test_difiere_el_ns_tipeado_a_mano_sin_os(self):
        # Sin ingreso ni device la fila se resuelve después por número de serie
        # (resolve_rental_item_partidas), no acá.
        contexts = self._validate(
            [self._item(ingresoId=None, deviceId=None, partida_row={"ingresoId": None, "deviceId": None})]
        )

        self.assertEqual(contexts, [])


class RentalStockLotVerdictTests(SimpleTestCase):
    """El recuadro "NS disponibles" ofrecía NS que el guardado después rechazaba: E2321905419
    tenía stock en STL y figuraba alquilado desde 2024. Lo que el recuadro marca como no
    alquilable tiene que ser exactamente lo que rechazaría el guardado, con el mismo mensaje."""

    SERIAL = "E2321905419"

    def _context(self, **overrides):
        row = {
            "ingreso_id": 25863,
            "device_id": 25863,
            "estado": "reparado",
            "alquilado": False,
            "ubicacion_nombre": "Estantería de Alquiler",
            "equipment_serial": self.SERIAL,
            "equipment_internal_number": "MG 7212",
        }
        row.update(overrides)
        return row

    def _save_error(self, rows, device_rows, device_exists):
        with (
            patch.object(delivery_orders, "_rental_equipment_contexts_by_serial", return_value=list(rows)),
            patch.object(delivery_orders, "_rental_device_contexts_by_serial", return_value=list(device_rows)),
            patch.object(delivery_orders, "_device_exists_by_serial", return_value=device_exists),
        ):
            with self.assertRaises(delivery_orders.DeliveryOrderError) as caught:
                delivery_orders._resolve_rental_context_for_partida(self.SERIAL, None, None)
        return caught.exception

    def _verdict(self, rows, device_rows=(), device_exists=False, reserved=None):
        with patch.object(delivery_orders, "_rental_duplicate_reservation", return_value=reserved):
            return delivery_orders._rental_serial_verdict(
                self.SERIAL,
                list(rows) or list(device_rows),
                device_exists=device_exists,
                order_id=None,
            )

    def test_el_motivo_es_el_mismo_que_da_el_guardado(self):
        device_row = self._context(ingreso_id=None, estado="", mg_estado="activo", alquilado=True)
        cases = {
            "sin alta en NEXORA": ([], [], False),
            "sin cliente cargado": ([], [], True),
            "OS alquilada": ([self._context(estado="alquilado", alquilado=True)], [], False),
            "fuera de la estantería": ([self._context(ubicacion_nombre="-")], [], False),
            "equipo sin OS alquilado": ([], [device_row], False),
        }
        for name, (rows, device_rows, device_exists) in cases.items():
            with self.subTest(name):
                expected = self._save_error(rows, device_rows, device_exists)
                _, error = self._verdict(rows, device_rows, device_exists)
                self.assertIsNotNone(error)
                self.assertEqual((error.code, str(error)), (expected.code, str(expected)))

    def test_el_equipo_en_la_estanteria_se_ofrece(self):
        context, error = self._verdict([self._context()])

        self.assertIsNone(error)
        self.assertEqual(context["equipment_internal_number"], "MG 7212")

    def test_el_reservado_en_otra_orden_no_se_ofrece(self):
        _, error = self._verdict([self._context()], reserved="OE-00417")

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_RESERVED")
        self.assertIn("OE-00417", str(error))


class RentalSerialErrorMessageTests(SimpleTestCase):
    """Cada motivo por el que un NS no se puede alquilar contestaba lo mismo
    ("NS no vinculado a una OS disponible"), que no decía cuál era ni qué hacer.

    Cubre además el equipo dado de alta con su MG y sin OS: alquilar no exige ingreso.
    """

    def _context(self, **overrides):
        row = {
            "ingreso_id": 11,
            "device_id": 7,
            "estado": "reparado",
            "alquilado": False,
            "ubicacion_nombre": "Estantería de Alquiler",
            "equipment_serial": "B3CBE407167",
            "equipment_internal_number": "MG 9001",
        }
        row.update(overrides)
        return row

    def _device_context(self, **overrides):
        row = self._context(ingreso_id=None, estado="", mg_estado="activo")
        row.update(overrides)
        return row

    def _resolve(self, rows, *, device_rows=None, device_exists=False):
        with (
            patch.object(delivery_orders, "_rental_equipment_contexts_by_serial", return_value=rows),
            patch.object(delivery_orders, "_rental_device_contexts_by_serial", return_value=list(device_rows or [])),
            patch.object(delivery_orders, "_device_exists_by_serial", return_value=device_exists),
        ):
            return delivery_orders._resolve_rental_context_for_partida("B3CBE407167", None, None)

    def _error(self, rows, *, device_rows=None, device_exists=False):
        with self.assertRaises(delivery_orders.DeliveryOrderError) as caught:
            self._resolve(rows, device_rows=device_rows, device_exists=device_exists)
        return caught.exception

    def test_serie_que_no_existe_en_nexora(self):
        error = self._error([])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_REGISTERED")
        self.assertIn("no está dado de alta como equipo en NEXORA", str(error))
        self.assertIn("Dalo de alta en Equipos con su MG", str(error))

    def test_equipo_sin_cliente_cargado(self):
        error = self._error([], device_exists=True)

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_WITHOUT_CUSTOMER")
        self.assertIn("no tiene cliente cargado", str(error))

    def test_os_alquilada_dice_que_esta_alquilada(self):
        error = self._error([self._context(alquilado=True)])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_AVAILABLE")
        self.assertIn("OS-00011, que ya figura alquilada", str(error))

    def test_os_cerrada_nombra_el_estado(self):
        error = self._error([self._context(estado="baja")])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_AVAILABLE")
        self.assertIn("figura dada de baja", str(error))

    def test_fuera_de_la_estanteria_dice_donde_esta(self):
        error = self._error([self._context(ubicacion_nombre="Taller")])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_IN_SHELF")
        self.assertIn("está en Taller, no en Estantería de Alquiler", str(error))
        self.assertIn("Movelo a Estantería de Alquiler", str(error))

    def test_ubicacion_vacia_no_se_imprime_como_guion(self):
        error = self._error([self._context(ubicacion_nombre="-")])

        self.assertIn("está en sin ubicación", str(error))

    def test_avisa_cuando_hay_mas_de_una_os_con_el_mismo_ns(self):
        rows = [self._context(alquilado=True), self._context(ingreso_id=9, estado="entregado")]

        error = self._error(rows)

        self.assertIn("OS-00011, que ya figura alquilada", str(error))
        self.assertIn("Las otras 1 OS con ese NS tampoco están disponibles", str(error))

    def test_devuelve_la_primera_os_disponible(self):
        rows = [self._context(alquilado=True), self._context(ingreso_id=9)]

        self.assertEqual(self._resolve(rows)["ingreso_id"], 9)

    def test_toma_el_equipo_con_mg_aunque_no_tenga_os(self):
        context = self._resolve([], device_rows=[self._device_context()])

        self.assertIsNone(context["ingreso_id"])
        self.assertEqual(context["device_id"], 7)

    def test_equipo_sin_os_ya_alquilado_lo_dice_en_masculino(self):
        error = self._error([], device_rows=[self._device_context(alquilado=True)])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_AVAILABLE")
        self.assertIn("El NS B3CBE407167 ya figura alquilado.", str(error))
        self.assertNotIn("OS-", str(error))

    def test_equipo_sin_os_fuera_de_la_estanteria(self):
        error = self._error([], device_rows=[self._device_context(ubicacion_nombre="Depósito SEPID")])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_NOT_IN_SHELF")
        self.assertIn("está en Depósito SEPID, no en Estantería de Alquiler", str(error))

    def test_mg_vendido_no_se_puede_alquilar(self):
        error = self._error([], device_rows=[self._device_context(mg_estado="inactivo_venta")])

        self.assertEqual(error.code, "RENTAL_EQUIPMENT_MG_INACTIVE")
        self.assertIn("dado de baja del parque MG por venta", str(error))
        self.assertIn("Reactivá el MG", str(error))

    def test_la_os_manda_cuando_el_equipo_tiene_una(self):
        # El equipo con historial sigue resolviéndose por su OS: el camino por equipo es
        # sólo para el que no tiene ninguna.
        context = self._resolve([self._context()], device_rows=[self._device_context()])

        self.assertEqual(context["ingreso_id"], 11)

    def test_ningun_mensaje_manda_a_hacer_un_ingreso_para_alquilar(self):
        # Un equipo no necesita ingreso para estar alquilado: se da de alta en Equipos con
        # su MG. Ningún mensaje debe mandar a hacer un ingreso.
        textos = [
            str(self._error([])),
            str(self._error([], device_exists=True)),
            str(self._error([self._context(alquilado=True)])),
            str(self._error([self._context(ubicacion_nombre="Taller")])),
            str(self._error([], device_rows=[self._device_context(alquilado=True)])),
        ]

        for texto in textos:
            self.assertNotIn("ingreso", texto.lower())


class RentalItemsMissingEquipmentTests(SimpleTestCase):
    """Registrar el RTA a mano sin equipo dejaba la orden con número y sin alquiler."""

    def test_marca_los_renglones_sin_equipo(self):
        order = {
            "items": [
                {"partidas": [{"partida": "MZJ5S121220"}]},
                {"partidas": []},
                {"partida": "B3CBE407167", "partidas": []},
                {"partidas": [{"partida": "   "}]},
            ]
        }

        self.assertEqual(delivery_orders.rental_items_missing_equipment(order), [2, 4])

    def test_una_orden_completa_no_tiene_faltantes(self):
        order = {"items": [{"partidas": [{"partida": "MZJ5S121220"}]}]}

        self.assertEqual(delivery_orders.rental_items_missing_equipment(order), [])


class FakeRentalSalesClient:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.calls = []

    def list_comprobantes_ventas(self, filters):
        self.calls.append(("list_comprobantes_ventas", filters))
        values = {item.get("Campo"): item.get("Valor") for item in filters}
        records = []
        for record in self.records:
            if values.get("Comprobante_Tipo") and record.get("Comprobante_Tipo") != values["Comprobante_Tipo"]:
                continue
            if values.get("Comprobante_PtoVenta") and str(record.get("Comprobante_PtoVenta")).zfill(5) != values["Comprobante_PtoVenta"]:
                continue
            if values.get("Comprobante_Numero") and str(record.get("Comprobante_Numero")).zfill(8) != values["Comprobante_Numero"]:
                continue
            records.append(record)
        return {"DatosJSON": records}

    def list_clientes(self):
        self.calls.append(("list_clientes", []))
        return {
            "DatosJSON": [
                {
                    "Cliente_Codigo": "OXI",
                    "Cliente_RazonSocial": "OXICASTHOMECARE S.R.L.",
                    "Cliente_NroDocumento": "30700000000",
                    "Cliente_TipoDocumento": "1",
                    "Cliente_SitIVA": "RI",
                }
            ]
        }

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        self.calls.append(("ingresar_comprobante_ventas_json", comprobante, kwargs))
        self.records.append(
            {
                "Comprobante_Tipo": comprobante["Comprobante_Tipo"],
                "Comprobante_Letra": comprobante["Comprobante_Letra"],
                "Comprobante_PtoVenta": comprobante["Comprobante_PtoVenta"],
                "Comprobante_Numero": comprobante["Comprobante_Numero"],
                "Comprobante_TipoOperacion": comprobante["Comprobante_TipoOperacion"],
                "Comprobante_FechaEmision": comprobante["Comprobante_FechaEmision"],
                "Cliente_Codigo": comprobante["Cliente_Codigo"],
                "Cliente_RazonSocial": comprobante["Cliente_RazonSocial"],
                "Comprobante_Items": comprobante["Comprobante_Items"],
            }
        )
        return {
            "DatosJSON": {
                "Comprobante_Tipo": comprobante["Comprobante_Tipo"],
                "Comprobante_Letra": comprobante["Comprobante_Letra"],
                "Comprobante_PtoVenta": comprobante["Comprobante_PtoVenta"],
                "Comprobante_Numero": comprobante["Comprobante_Numero"],
            }
        }


class FakeRentalStockClient:
    def __init__(self, quantities):
        self.quantities = list(quantities)
        self.calls = []

    def stock_by_deposit_partida(self, deposit, serial):
        self.calls.append(("stock_by_deposit_partida", deposit, serial))
        quantity = self.quantities[0] if len(self.quantities) == 1 else self.quantities.pop(0)
        return {
            "DatosJSON": [
                {
                    "Comprobante_ArtDeposito": "STL",
                    "Comprobante_ArtPartida": serial,
                    "Comprobante_Art_CodGen": "1104003",
                    "Comprobante_DescArticulo": "BPAP G2",
                    "Comprobante_CantidadUM1": quantity,
                    "Disponible": quantity,
                }
            ]
        }

    def ingresar_lista_comprobantes_json(self, payload):
        self.calls.append(("ingresar_lista_comprobantes_json", payload))
        self.quantities = [0]
        return {"DatosJSON": [{"Resultado": "OK"}]}


def rental_record(point="00004", number="00004587", serial="Y2A19608057", customer="OXICASTHOMECARE S.R.L."):
    return {
        "Comprobante_Tipo": "RTA",
        "Comprobante_Letra": "R",
        "Comprobante_PtoVenta": point,
        "Comprobante_Numero": number,
        "Comprobante_TipoOperacion": "ALQ",
        "Comprobante_FechaEmision": "2026-06-16",
        "Cliente_Codigo": "OXI",
        "Cliente_RazonSocial": customer,
        "Comprobante_Items": [
            {
                "Item_CodigoArticulo": "1104003",
                "Item_DescripArticulo": "BPAP G2",
                "Item_Deposito": "STL",
                "Item_Partida": serial,
                "Item_CantidadUM1": 1,
            }
        ],
    }


class RentalRemitoStateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            for statement in (
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_a TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_remito TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS alquiler_fecha TIMESTAMPTZ NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS empresa_bejerman TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS empresa_facturar TEXT NULL",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS cod_empresa TEXT",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS telefono TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS n_de_control TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS variante TEXT",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquilado BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
            ):
                cur.execute(statement)
        call_command("apply_bejerman_sync_schema", verbosity=0)
        super().setUpClass()

    def setUp(self):
        User.objects.filter(email__endswith="@rental-remito.test").delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_sync_jobs")
            cur.execute("DELETE FROM bejerman_article_mappings")
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
            cur.execute("INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s) RETURNING id", ["OXI", "OXICASTHOMECARE S.R.L.", ""])
            self.customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["BMC"])
            self.marca_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id", [self.marca_id, "BPAP G2", "BPAP", "Y-25T"])
            self.model_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [self.customer_id, self.marca_id, self.model_id, "Y2A19608057", "MG 6757", "Y-25T"],
            )
            self.device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_ingreso, resolucion, alquiler_a, empresa_bejerman, empresa_facturar)
                VALUES (%s,%s,%s,CURRENT_TIMESTAMP,%s,%s,%s,%s)
                RETURNING id
                """,
                [self.device_id, "reparado", "reparación", "reparado", "OXICASTHOMECARE S.R.L.", "SEPID", "SEPID"],
            )
            self.ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO bejerman_article_mappings(model_id, variante, variante_norm, article_code, article_description, match_source)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [self.model_id, "Y-25T", "y-25t", "1104003", "BPAP G2", "manual"],
            )
        self.user = User.objects.create(
            nombre="Admin",
            email="admin@rental-remito.test",
            hash_pw="",
            rol="admin",
            activo=True,
        )

    def test_existing_point_verified_when_stock_left_stl(self):
        state = save_rental_remito(
            self.ingreso_id,
            "00004-00004587",
            sales_client=FakeRentalSalesClient([rental_record()]),
            stock_client=FakeRentalStockClient([0]),
        )

        self.assertEqual(state["status"], "verified")
        with connection.cursor() as cur:
            cur.execute("SELECT alquiler_remito, remito_salida FROM ingresos WHERE id=%s", [self.ingreso_id])
            alquiler_remito, remito_salida = cur.fetchone()
            self.assertEqual(alquiler_remito, "RTA R 00004-00004587")
            self.assertEqual(remito_salida, "RTA R 00004-00004587")

    def test_existing_point_offers_stock_correction_when_stl_positive(self):
        state = get_rental_remito_state(
            self.ingreso_id,
            "00004-00004587",
            sales_client=FakeRentalSalesClient([rental_record()]),
            stock_client=FakeRentalStockClient([1]),
        )

        self.assertEqual(state["status"], "stock_inconsistent")
        self.assertEqual(state["action"]["type"], "correct_stl_exit")

    def test_point_one_can_register_when_rta_missing_and_stl_positive(self):
        state = get_rental_remito_state(
            self.ingreso_id,
            "00001-00004587",
            sales_client=FakeRentalSalesClient([]),
            stock_client=FakeRentalStockClient([1]),
        )

        self.assertEqual(state["status"], "needs_stock_action")
        self.assertEqual(state["action"]["type"], "register_rta_pv1")

    def test_correct_stock_action_is_idempotent_when_stl_is_already_empty(self):
        result = execute_rental_remito_action(
            self.ingreso_id,
            "correct_stl_exit",
            remito_number="00004-00004587",
            sales_client=FakeRentalSalesClient([rental_record()]),
            stock_client=FakeRentalStockClient([0]),
            actor_user_id=self.user.id,
        )

        self.assertTrue(result["idempotent"])

    def test_register_action_is_idempotent_when_rta_already_exists(self):
        result = execute_rental_remito_action(
            self.ingreso_id,
            "register_rta_pv1",
            remito_number="00001-00004587",
            sales_client=FakeRentalSalesClient([rental_record(point="00001")]),
            stock_client=FakeRentalStockClient([0]),
            actor_user_id=self.user.id,
        )

        self.assertTrue(result["idempotent"])

    def test_register_action_writes_trace_job_and_event(self):
        result = execute_rental_remito_action(
            self.ingreso_id,
            "register_rta_pv1",
            remito_number="00001-00004587",
            sales_client=FakeRentalSalesClient([]),
            stock_client=FakeRentalStockClient([1, 0]),
            actor_user_id=self.user.id,
        )

        self.assertEqual(result["status"], "succeeded")
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id=%s AND sync_type=%s", [self.ingreso_id, "rental_remito_rta_register"])
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT COUNT(*) FROM ingreso_events WHERE ticket_id=%s AND comentario ILIKE %s", [self.ingreso_id, "%RTA R 00001-00004587%"])
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT remito_salida FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertEqual(cur.fetchone()[0], "RTA R 00001-00004587")

    def _make_particular_ingreso(self, *, propietario_nombre="Juan Perez", propietario_doc="30710698"):
        with connection.cursor() as cur:
            # 'PTL' a proposito: en produccion la fila Particular quedo con un codigo cargado que no
            # existe en Bejerman. Con NULL aca el test pasaba en verde mientras el circuito real
            # fallaba, porque el camino de cliente ocasional esta detras de guardas
            # `if not customer_code`.
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono) VALUES (%s,%s,%s) RETURNING id",
                ["PTL", "Particular", ""],
            )
            particular_customer_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [particular_customer_id, self.marca_id, self.model_id, "Y2A19608999", "MG 6799", "Y-25T"],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_ingreso, resolucion, alquiler_a,
                    empresa_bejerman, empresa_facturar, propietario_nombre, propietario_doc
                )
                VALUES (%s,%s,%s,CURRENT_TIMESTAMP,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    device_id, "reparado", "reparación", "reparado", "Particular",
                    "SEPID", "SEPID", propietario_nombre, propietario_doc,
                ],
            )
            return int(cur.fetchone()[0])

    def test_point_one_habilita_registro_para_cliente_particular_con_propietario(self):
        ingreso_id = self._make_particular_ingreso()

        state = get_rental_remito_state(
            ingreso_id,
            "00001-00004999",
            sales_client=FakeRentalSalesClient([]),
            stock_client=FakeRentalStockClient([1]),
        )

        self.assertEqual(state["status"], "needs_stock_action")
        self.assertEqual(state["action"]["type"], "register_rta_pv1")

    def test_point_one_sigue_bloqueado_si_particular_no_tiene_propietario_doc(self):
        ingreso_id = self._make_particular_ingreso(propietario_nombre="", propietario_doc="")

        state = get_rental_remito_state(
            ingreso_id,
            "00001-00004999",
            sales_client=FakeRentalSalesClient([]),
            stock_client=FakeRentalStockClient([1]),
        )

        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["code"], "RENTAL_CUSTOMER_REQUIRED")

    def test_register_rta_para_cliente_particular_usa_codigo_ocasional(self):
        ingreso_id = self._make_particular_ingreso(propietario_nombre="Juan Perez", propietario_doc="30.710.698")
        sales_client = FakeRentalSalesClient([])

        result = execute_rental_remito_action(
            ingreso_id,
            "register_rta_pv1",
            remito_number="00001-00004999",
            sales_client=sales_client,
            stock_client=FakeRentalStockClient([1, 0]),
            actor_user_id=self.user.id,
        )

        self.assertEqual(result["status"], "succeeded")
        emitted = next(call for call in sales_client.calls if call[0] == "ingresar_comprobante_ventas_json")
        comprobante = emitted[1]
        self.assertEqual(comprobante["Cliente_Codigo"], "@@@#@@")
        self.assertEqual(comprobante["Cliente_RazonSocial"], "Juan Perez")
        self.assertFalse(any(call[0] == "list_clientes" for call in sales_client.calls))


# Nota: las vistas IngresoAlquilerRemitoView/IngresoAlquilerRemitoAccionView se
# eliminaron de permission_policy; el test de permisos que las cubría se quitó
# junto con ellas.

