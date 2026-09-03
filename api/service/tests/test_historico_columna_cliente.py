"""La columna Cliente del Histórico: se busca y se ordena por lo que se muestra.

'Particular' y 'MG BIO' son clientes contenedores. El listado ya mostraba el dueño real
(`CustomerCell`), pero **buscaba, filtraba y ordenaba por `c.razon_social`**: escribías el
nombre que estabas viendo en pantalla y no aparecía nada.

`GeneralEquiposView` no tenía ningún test, y es el endpoint de `/api/ingresos/`. Eso importa
especialmente acá porque las cláusulas de búsqueda se arman a mano con `%s` contados a mano:
si el número de placeholders y el de parámetros se desincroniza, psycopg falla la consulta
entera. Estos tests recorren los tres caminos (`q`, `cliente`, `sort`) contra la base real,
así que ese desfasaje aparece como error y no como una lista vacía.

Necesita PostgreSQL con el esquema sembrado (ver la receta de tests del proyecto).
"""

from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class HistoricoColumnaClienteTest(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
            # Columnas de compat que el helper del dueño lee con guarda de esquema.
            cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS propietario_nombre TEXT")
            cur.execute(
                "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cliente_original_id "
                "INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
            )
            cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS tipo_equipo TEXT")
            # El listado hace LEFT JOIN con estas dos; la base sembrada no las trae.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id BIGSERIAL PRIMARY KEY,
                    ingreso_id INTEGER NOT NULL,
                    version_num INTEGER NOT NULL DEFAULT 1,
                    estado TEXT NULL,
                    fecha_emitido TIMESTAMPTZ NULL,
                    fecha_aprobado TIMESTAMPTZ NULL,
                    fecha_rechazado TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id BIGSERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL DEFAULT ''
                )
                """
            )
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        User.objects.all().delete()
        cls.admin = User.objects.create(
            nombre="Admin Historico",
            email="admin-historico@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.token = issue_token(cls.admin)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_events")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM customers")

            cur.execute("INSERT INTO customers (razon_social) VALUES ('Particular') RETURNING id")
            self.particular_id = cur.fetchone()[0]
            cur.execute("INSERT INTO customers (razon_social) VALUES ('Sanatorio Guemes') RETURNING id")
            self.sanatorio_id = cur.fetchone()[0]
            cur.execute("INSERT INTO customers (razon_social) VALUES ('MG BIO SRL') RETURNING id")
            self.mg_owner_id = cur.fetchone()[0]

            self.os_particular = self._alta(
                self.particular_id, "NS-PART-1", propietario="Zulema Ortiz"
            )
            self.os_empresa = self._alta(self.sanatorio_id, "NS-EMP-1")
            # Equipo tomado en parte de pago: el `customer_id` ya es MG BIO y el cliente que
            # lo trajo quedó en el snapshot. Un cliente empresa no deja `propietario_nombre`.
            self.os_tomado = self._alta(self.mg_owner_id, "NS-TOMADO-1")
            cur.execute(
                "UPDATE ingresos SET cliente_original_id=%s WHERE id=%s",
                [self.sanatorio_id, self.os_tomado],
            )

    def _alta(self, customer_id, serie, propietario=""):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO devices (customer_id, numero_serie, tipo_equipo) VALUES (%s,%s,'CPAP') RETURNING id",
                [customer_id, serie],
            )
            device_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO ingresos (device_id, estado, motivo, propietario_nombre)
                VALUES (%s, 'reparado', 'reparación', %s) RETURNING id
                """,
                [device_id, propietario or None],
            )
            return cur.fetchone()[0]

    def _ids(self, **params):
        resp = self.client.get("/api/ingresos/historico/", params)
        self.assertEqual(resp.status_code, 200, resp.data)
        filas = resp.data["items"] if isinstance(resp.data, dict) else resp.data
        return [int(f["id"]) for f in filas]

    # ---- búsqueda libre ----

    def test_se_encuentra_por_el_dueno_que_muestra_la_columna(self):
        self.assertEqual(self._ids(q="Zulema Ortiz"), [self.os_particular])

    def test_el_cliente_contenedor_sigue_siendo_buscable(self):
        # El dueño se agregó *además* de la razón social, no en su lugar.
        self.assertIn(self.os_particular, self._ids(q="Particular"))

    def test_un_equipo_tomado_se_encuentra_por_el_cliente_que_lo_trajo(self):
        self.assertIn(self.os_tomado, self._ids(q="Guemes"))

    def test_la_busqueda_por_cliente_real_no_cambio(self):
        self.assertIn(self.os_empresa, self._ids(q="Guemes"))

    def test_una_busqueda_sin_resultados_no_rompe(self):
        self.assertEqual(self._ids(q="no-existe-nada"), [])

    # ---- filtro de la columna ----

    def test_el_filtro_de_columna_tambien_busca_por_el_dueno(self):
        self.assertEqual(self._ids(cliente="Zulema"), [self.os_particular])

    def test_el_filtro_de_columna_sigue_encontrando_al_cliente(self):
        self.assertIn(self.os_empresa, self._ids(cliente="Guemes"))

    # ---- orden ----

    def test_ordena_por_el_nombre_que_se_ve_y_no_por_el_contenedor(self):
        # Lo que muestra la columna: "Zulema Ortiz", "Sanatorio Guemes" y "Sanatorio Guemes"
        # (el tomado, por el cliente original). Ascendente, las dos Sanatorio van primero y
        # Zulema al final. Si ordenara por razón social, el de 'Particular' abriría la lista
        # y el de 'MG BIO SRL' quedaría segundo, que es justo lo que se está arreglando.
        orden = self._ids(sort="cliente")
        self.assertEqual(orden[-1], self.os_particular)
        self.assertEqual(sorted(orden[:2]), sorted([self.os_empresa, self.os_tomado]))

        invertido = self._ids(sort="-cliente")
        self.assertEqual(invertido[0], self.os_particular)

    # ---- el export tiene que devolver lo mismo que la pantalla ----

    def test_el_export_aplica_los_mismos_filtros(self):
        resp = self.client.get("/api/ingresos/historico/export/", {"q": "Zulema Ortiz"})
        self.assertEqual(resp.status_code, 200)
