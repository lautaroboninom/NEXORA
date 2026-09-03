"""El cliente de los papeles cuando el equipo pasó a ser nuestro.

Tomar un equipo en parte de pago (o convertirlo a propio MG) pisa
`devices.customer_id` con MG BIO, y todos los papeles de la OS resuelven el
cliente por ese join: la orden de salida que firma el cliente salía a nombre de
'MG BIO S.R.L.'. Con un particular se salvaba por `propietario_nombre`, pero un
cliente empresa no deja nombre ahí, así que hace falta el snapshot
`ingresos.cliente_original_id`.

Son `SimpleTestCase` a propósito: `_apply_owner_display` es una función pura sobre
el dict de la cabecera, y los armadores de SQL sólo necesitan que se les mienta
sobre qué columnas existen.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from service.pdf import _apply_owner_display
from service.views.ingresos_views import _customer_search_expr, _owner_name_expr


class ApplyOwnerDisplayTests(SimpleTestCase):
    def test_cliente_real_no_se_toca(self):
        head = {"cliente": "Sanatorio Güemes", "cliente_cuit": "30-111", "propietario_nombre": "Ana"}
        _apply_owner_display(head)
        self.assertEqual(head["cliente"], "Sanatorio Güemes")
        self.assertEqual(head["cliente_cuit"], "30-111")

    def test_particular_muestra_el_propietario(self):
        head = {
            "cliente": "Particular",
            "cliente_cuit": "",
            "propietario_nombre": "Ana Gómez",
            "propietario_doc": "27-123",
            "propietario_contacto": "11-5555",
        }
        _apply_owner_display(head)
        self.assertEqual(head["cliente"], "Ana Gómez")
        self.assertEqual(head["cliente_cuit"], "27-123")
        self.assertEqual(head["cliente_contacto"], "11-5555")

    def test_equipo_tomado_a_una_empresa_conserva_el_cliente_que_lo_trajo(self):
        # El caso del bug: parte de pago de un cliente empresa. `propietario_nombre`
        # viene vacío porque nadie carga una persona para un sanatorio.
        head = {
            "cliente": "MG BIO S.R.L.",
            "cliente_cuit": "30-999",
            "propietario_nombre": "",
            "cliente_original": "Sanatorio Güemes",
            "cliente_original_cuit": "30-111",
            "cliente_original_contacto": "Compras",
        }
        _apply_owner_display(head)
        self.assertEqual(head["cliente"], "Sanatorio Güemes")
        self.assertEqual(head["cliente_cuit"], "30-111")
        self.assertEqual(head["cliente_contacto"], "Compras")

    def test_el_propietario_le_gana_al_snapshot(self):
        head = {
            "cliente": "MG BIO",
            "propietario_nombre": "Ana Gómez",
            "cliente_original": "Sanatorio Güemes",
        }
        _apply_owner_display(head)
        self.assertEqual(head["cliente"], "Ana Gómez")

    def test_un_mg_propio_de_siempre_sigue_diciendo_mg_bio(self):
        # Sin snapshot ni propietario no hay a quién mostrar: el equipo es nuestro
        # desde el vamos y el papel tiene que seguir diciendo MG BIO.
        head = {"cliente": "MG BIO S.R.L.", "propietario_nombre": "", "cliente_original": ""}
        _apply_owner_display(head)
        self.assertEqual(head["cliente"], "MG BIO S.R.L.")

    def test_una_cabecera_incompleta_no_rompe_el_pdf(self):
        head = {}
        _apply_owner_display(head)
        self.assertEqual(head, {})


class OwnerSqlExpressionTests(SimpleTestCase):
    def _con_columnas(self, *presentes):
        disponibles = set(presentes)
        return patch(
            "service.views.ingresos_views._has_table_column",
            side_effect=lambda tabla, col: (tabla, col) in disponibles,
        )

    def test_el_snapshot_entra_como_ultimo_recurso(self):
        with self._con_columnas(
            ("ingresos", "propietario_nombre"),
            ("devices", "propietario_nombre"),
            ("ingresos", "cliente_original_id"),
        ):
            expr = _owner_name_expr()
        self.assertIn("t.propietario_nombre", expr)
        self.assertIn("cliente_original_id", expr)
        # Orden: la persona cargada en el ingreso manda sobre el snapshot.
        self.assertLess(expr.index("t.propietario_nombre"), expr.index("cliente_original_id"))

    def test_sin_la_columna_nueva_no_se_nombra(self):
        with self._con_columnas(("ingresos", "propietario_nombre")):
            expr = _owner_name_expr()
        self.assertNotIn("cliente_original_id", expr)

    def test_sin_ninguna_columna_de_dueno_la_expresion_sigue_siendo_valida(self):
        with self._con_columnas():
            self.assertEqual(_owner_name_expr(), "''")
            self.assertEqual(_customer_search_expr(), "COALESCE(c.razon_social,'')")

    def test_la_expresion_de_busqueda_no_usa_porcentajes(self):
        # Un '%' suelto explota al pasar por psycopg2 (hay que escribirlo '%%') y esta
        # expresión se interpola en consultas que a veces corren sin parámetros.
        with self._con_columnas(("ingresos", "propietario_nombre")):
            expr = _customer_search_expr()
        self.assertNotIn("%", expr)
        self.assertIn("STRPOS", expr)

    def test_el_dueno_solo_desplaza_al_cliente_contenedor(self):
        with self._con_columnas(("ingresos", "propietario_nombre")):
            expr = _customer_search_expr()
        self.assertIn("'particular'", expr)
        self.assertIn("t.propietario_nombre", expr)
        self.assertIn("c.razon_social", expr)
