"""Un grupo que falló sin que nadie registrara por qué rompía el barrido entero.

`_optional_text` devuelve `None`, no `""`. Con `last_error` nulo y sin `error` en el
resumen, la línea que arma el detalle hacía `None[:90]` y el comando abortaba con un
`TypeError` **antes de tocar nada** — así que ninguno de los procesos trabados se
limpiaba, ni siquiera los que no tenían ningún problema.

Apareció al correrlo contra la base de desarrollo, donde hay veinte grupos de
Terapias Médicas Domiciliarias en ese estado. En producción no se había visto porque
todos los candidatos tenían su error cargado.
"""

from io import StringIO
from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.test import TestCase


@skipUnless(connection.vendor == "postgresql", "El barrido requiere PostgreSQL")
class BarridoConGruposSinErrorTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("apply_delivery_orders_schema", verbosity=0)
        call_command("apply_bejerman_sync_schema", verbosity=0)
        call_command("apply_bejerman_ris_schema", verbosity=0)

    # La tabla tiene media docena de columnas NOT NULL que a este barrido no le
    # importan; se rellenan con vacío para que el insert pase.
    OBLIGATORIAS = (
        "comprobante_tipo, comprobante_letra, customer_code, seller_code, "
        "payment_term_code, operation_code, deposit_code"
    )
    VACIAS = "'RSS', 'R', 'TMD', '', '', '', 'STC'"

    def _grupo(self, group_id, *, order_ids, status, last_error, remito_number=None):
        with connection.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO bejerman_remito_groups
                       (id, status, customer_name, order_ids, last_error, remito_number,
                        {self.OBLIGATORIAS})
                VALUES (%s, %s, 'TERAPIAS MEDICAS DOMICILIARIAS S.A.', %s::jsonb, %s, %s,
                        {self.VACIAS})
                """,
                [group_id, status, order_ids, last_error, remito_number],
            )

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_remito_groups")

    def test_un_grupo_fallado_sin_error_no_tumba_el_barrido(self):
        # El que revienta: falló, no dice por qué, y tiene un intento posterior que lo
        # vuelve candidato a mirarse.
        self._grupo("brg-sin-error", order_ids='["do-1"]', status="failed", last_error=None)
        self._grupo(
            "brg-emitido",
            order_ids='["do-1"]',
            status="generated",
            last_error=None,
            remito_number="RT R 00004-00009999",
        )

        salida = StringIO()
        call_command("limpiar_procesos_bejerman_trabados", stdout=salida)

        texto = salida.getvalue()
        # Lo importante es que haya llegado al resumen: antes moría antes.
        self.assertIn("SIMULACION", texto)
        self.assertIn("sin detalle registrado", texto)

    def test_la_simulacion_no_escribe_nada(self):
        self._grupo("brg-sin-error-2", order_ids='["do-2"]', status="failed", last_error=None)

        call_command("limpiar_procesos_bejerman_trabados", stdout=StringIO())

        with connection.cursor() as cur:
            cur.execute("SELECT status FROM bejerman_remito_groups WHERE id = 'brg-sin-error-2'")
            self.assertEqual(cur.fetchone()[0], "failed")

    def test_un_tipo_de_comprobante_vacio_no_escribe_la_palabra_None(self):
        # La columna es NOT NULL pero acepta cadena vacía, y `_optional_text("")`
        # también devuelve None: sin el `or ''` el remito quedaba guardado en la base
        # como el texto literal "None R 00004-...".
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups
                       (id, status, customer_name, order_ids,
                        comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                        comprobante_numero, remito_number,
                        customer_code, seller_code, payment_term_code,
                        operation_code, deposit_code)
                VALUES ('brg-numero', 'generated', 'X', '["do-3"]'::jsonb,
                        '', '', '00004', '0000400004741', 'viejo',
                        'X', '', '', '', 'STC')
                """
            )

        call_command("limpiar_procesos_bejerman_trabados", "--apply", stdout=StringIO())

        with connection.cursor() as cur:
            cur.execute("SELECT remito_number FROM bejerman_remito_groups WHERE id = 'brg-numero'")
            numero = cur.fetchone()[0]

        self.assertNotIn("None", numero)
        self.assertEqual(numero, "R 00004-00004741")
