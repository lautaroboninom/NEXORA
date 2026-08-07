from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase

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
                [self.device_id, "reparado", "reparacion", "reparado", "OXICASTHOMECARE S.R.L.", "SEPID", "SEPID"],
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
            cur.execute("SELECT alquiler_remito FROM ingresos WHERE id=%s", [self.ingreso_id])
            self.assertEqual(cur.fetchone()[0], "RTA R 00004-00004587")

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
                    device_id, "reparado", "reparacion", "reparado", "Particular",
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

