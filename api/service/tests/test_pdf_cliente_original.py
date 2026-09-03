"""El cliente que sale impreso cuando el equipo ya es nuestro.

Tomar un equipo en parte de pago pisa `devices.customer_id` con MG BIO, y **todos los papeles
de la OS resuelven el cliente por ese join**: la orden de salida que el cliente firma al
llevarse el equipo del cambio salía a nombre de 'MG BIO S.R.L.'. Con un particular se salvaba
por `propietario_nombre`, pero un cliente empresa no deja nombre ahí.

La lógica de reemplazo (`_apply_owner_display`) ya tiene tests sin base en
`test_cliente_original.py`. Lo que se prueba acá es la otra mitad: que el SELECT y el LEFT
JOIN nuevos de `_get_data` traigan realmente el snapshot desde la base.
"""

from unittest import skipUnless

from django.db import connection
from django.test import TestCase

from service import pdf


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class GetDataClienteOriginalTest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cliente_original_id "
                "INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
            )
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT")
            # `_get_data` es una consulta ancha: se agregan las columnas que lee y que la
            # base sembrada no trae. Todas como TEXT/BOOLEAN según corresponda.
            for col in ("cuit", "contacto", "telefono", "telefono_2", "email"):
                cur.execute(f"ALTER TABLE customers ADD COLUMN IF NOT EXISTS {col} TEXT")
            for col in (
                "mg_venta_fecha",
                "propietario",
                "propietario_contacto",
                "propietario_doc",
                "variante",
            ):
                cur.execute(f"ALTER TABLE devices ADD COLUMN IF NOT EXISTS {col} TEXT")
            for col in (
                "accesorios",
                "descripcion_problema",
                "informe_preliminar",
                "remito_ingreso",
                "trabajos_realizados",
            ):
                cur.execute(f"ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS {col} TEXT")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS garantia_reparacion BOOLEAN")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN")
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS equipo_variante TEXT")
            # `quotes` puede haberla creado otro módulo con menos columnas (el
            # CREATE ... IF NOT EXISTS de arriba sería un no-op): se completan por ALTER.
            for col in ("moneda", "autorizado_por", "forma_pago", "plazo_entrega_txt",
                        "garantia_txt", "mant_oferta_txt", "estado"):
                cur.execute(f"ALTER TABLE quotes ADD COLUMN IF NOT EXISTS {col} TEXT")
            for col in ("subtotal", "iva_21", "total"):
                cur.execute(f"ALTER TABLE quotes ADD COLUMN IF NOT EXISTS {col} NUMERIC")
            for col in ("fecha_emitido", "fecha_aprobado", "fecha_rechazado"):
                cur.execute(f"ALTER TABLE quotes ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ")
            cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INTEGER DEFAULT 1")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id BIGSERIAL PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL,
                    version_num INTEGER NOT NULL DEFAULT 1,
                    estado TEXT NULL,
                    moneda TEXT NULL,
                    subtotal NUMERIC NULL,
                    iva_21 NUMERIC NULL,
                    total NUMERIC NULL,
                    autorizado_por TEXT NULL,
                    forma_pago TEXT NULL,
                    plazo_entrega_txt TEXT NULL,
                    garantia_txt TEXT NULL,
                    mant_oferta_txt TEXT NULL,
                    fecha_emitido TIMESTAMPTZ NULL,
                    fecha_aprobado TIMESTAMPTZ NULL,
                    fecha_rechazado TIMESTAMPTZ NULL
                )
                """
            )
        super().setUpClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM customers")

            cur.execute("INSERT INTO customers (razon_social) VALUES ('MG BIO SRL') RETURNING id")
            self.mg_owner_id = cur.fetchone()[0]
            cur.execute("INSERT INTO customers (razon_social, cuit) VALUES ('Sanatorio Guemes','30-111') RETURNING id")
            self.sanatorio_id = cur.fetchone()[0]

            # El equipo ya es nuestro: `customer_id` apunta a MG BIO.
            cur.execute(
                "INSERT INTO devices (customer_id, numero_serie, tipo_equipo) VALUES (%s,'NS-1','CPAP') RETURNING id",
                [self.mg_owner_id],
            )
            device_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, motivo) VALUES (%s,'reparado','reparación') RETURNING id",
                [device_id],
            )
            self.ingreso_id = cur.fetchone()[0]

    def test_sin_snapshot_el_cliente_sigue_siendo_el_del_equipo(self):
        head, _items = pdf._get_data(self.ingreso_id)

        self.assertEqual(head["cliente"], "MG BIO SRL")
        self.assertEqual(head["cliente_original"], "")

    def test_el_snapshot_llega_desde_la_base(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET cliente_original_id=%s WHERE id=%s",
                [self.sanatorio_id, self.ingreso_id],
            )

        head, _items = pdf._get_data(self.ingreso_id)

        self.assertEqual(head["cliente_original"], "Sanatorio Guemes")
        self.assertEqual(head["cliente_original_cuit"], "30-111")
        # `_get_data` no reemplaza nada: eso lo hace `_apply_owner_display` en el renderer.
        self.assertEqual(head["cliente"], "MG BIO SRL")

    def test_el_papel_termina_diciendo_el_cliente_que_trajo_el_equipo(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET cliente_original_id=%s WHERE id=%s",
                [self.sanatorio_id, self.ingreso_id],
            )
        head, _items = pdf._get_data(self.ingreso_id)

        pdf._apply_owner_display(head)

        self.assertEqual(head["cliente"], "Sanatorio Guemes")
        self.assertEqual(head["cliente_cuit"], "30-111")
