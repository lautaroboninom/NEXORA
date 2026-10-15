"""Pruebas de PendientesPresupuestoView, PresupuestadosView y ColasNovedadesView.

Verifica que equipos propios de alquiler (ej. MG 9902) o en ubicaciones como
'Estantería de Alquiler' aparezcan en la lista de pendientes de presupuesto
mientras su presupuesto_estado sea 'pendiente', que se reflejen en los contadores
de novedades de colas, y que al marcar 'no_aplica' o 'presupuestado' se comporten
correctamente.
"""

from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.views.colas_views import _SQL_PENDIENTES_PRESUPUESTO, _ids


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class PendientesPresupuestoTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            # Asegurar ubicaciones mínimas
            cur.execute("""
                INSERT INTO locations(nombre) VALUES
                    ('Taller'),
                    ('Estantería de Alquiler'),
                    ('Sarmiento')
                ON CONFLICT (nombre) DO NOTHING;
            """)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create(
            email=f"jefe_{uuid4().hex[:8]}@test.com",
            nombre="Jefe Test",
            rol="jefe",
            activo=True,
        )
        token = issue_token(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _crear_equipo_e_ingreso(
        self,
        *,
        numero_interno=None,
        numero_serie=None,
        motivo="reparación alquiler",
        estado="reparado",
        presupuesto_estado="pendiente",
        ubicacion_nombre="Estantería de Alquiler",
    ):
        key = uuid4().hex[:8]
        if numero_interno is None:
            numero_interno = f"MG {uuid4().int % 8000 + 1000}"
        if numero_serie is None:
            numero_serie = f"NS-{key}"
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(razon_social) VALUES (%s) RETURNING id",
                [f"Cliente {key}"],
            )
            customer_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO marcas(nombre) VALUES (%s) RETURNING id",
                [f"Marca {key}"],
            )
            marca_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo) VALUES (%s, %s, %s) RETURNING id",
                [marca_id, f"Modelo {key}", "Concentrador"],
            )
            model_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, mg_estado)
                VALUES (%s, %s, %s, %s, %s, 'activo') RETURNING id
                """,
                [customer_id, marca_id, model_id, numero_serie, numero_interno],
            )
            device_id = cur.fetchone()[0]

            cur.execute("SELECT id FROM locations WHERE LOWER(nombre) = LOWER(%s)", [ubicacion_nombre])
            loc_row = cur.fetchone()
            ubicacion_id = loc_row[0] if loc_row else None

            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, resolucion, presupuesto_estado, motivo, ubicacion_id, fecha_ingreso, fecha_servicio)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id
                """,
                [device_id, estado, estado, presupuesto_estado, motivo, ubicacion_id],
            )
            ingreso_id = cur.fetchone()[0]
            return ingreso_id

    def test_equipo_propio_alquiler_en_estanteria_aparece_en_pendientes_presupuesto(self):
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9902",
            motivo="reparación alquiler",
            estado="reparado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Estantería de Alquiler",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)

        ids = [row["id"] for row in response.data]
        self.assertIn(ingreso_id, ids)

        item = next(row for row in response.data if row["id"] == ingreso_id)
        self.assertEqual(item["numero_interno"], "MG 9902")
        self.assertEqual(item["estado"], "reparado")
        self.assertEqual(item["presupuesto_estado"], "pendiente")
        self.assertEqual(item["ubicacion_nombre"], "Estantería de Alquiler")
        self.assertEqual(item["motivo"], "reparación alquiler")

        # Verificar también el contador de colas del sidebar
        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertIn(ingreso_id, cola_ids)

        colas_resp = self.client.get("/api/colas/novedades/", HTTP_HOST="localhost")
        self.assertEqual(colas_resp.status_code, 200)
        self.assertIn(ingreso_id, colas_resp.data.get("colas", {}).get("pendientes_presupuesto", []))

    def test_equipo_propio_reparado_y_alquilado_sigue_pendiente_de_presupuesto(self):
        """El alquiler nuevo no salda el presupuesto por la reparación anterior."""
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9903",
            motivo="reparación alquiler",
            estado="alquilado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Estantería de Alquiler",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertIn(ingreso_id, [row["id"] for row in response.data])

        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertIn(ingreso_id, cola_ids)

    def test_equipo_de_cliente_alquilado_sigue_fuera_de_pendientes(self):
        """La excepción es sólo para el parque propio, no para cualquier alquiler."""
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="",
            motivo="reparación",
            estado="alquilado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Estantería de Alquiler",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(ingreso_id, [row["id"] for row in response.data])

        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertNotIn(ingreso_id, cola_ids)

    def test_no_aplica_directo_sin_quote_previa_y_quitar(self):
        """Verifica que marcar 'No aplica' funcione directamente aunque la orden
        no tenga aún una fila creada en la tabla quotes, y que quitarlo la restaure."""
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9902",
            motivo="baja alquiler",
            estado="reparado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Estantería de Alquiler",
        )

        # Confirmar que todavía no existe quote en DB
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM quotes WHERE ingreso_id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], 0)

        # Marcar 'No aplica' directamente vía API
        res_no_aplica = self.client.post(f"/api/quotes/{ingreso_id}/no-aplica/", HTTP_HOST="localhost")
        self.assertEqual(res_no_aplica.status_code, 200)
        self.assertEqual(res_no_aplica.data.get("estado"), "no_aplica")

        # Ya no debe figurar en pendientes de presupuesto ni en el contador
        res_pend = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertNotIn(ingreso_id, [r["id"] for r in res_pend.data])

        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertNotIn(ingreso_id, cola_ids)

        # Quitar 'No aplica' debe restaurar a pendiente
        res_quitar = self.client.post(f"/api/quotes/{ingreso_id}/no-aplica/quitar/", HTTP_HOST="localhost")
        self.assertEqual(res_quitar.status_code, 200)
        self.assertEqual(res_quitar.data.get("estado"), "pendiente")

        res_pend2 = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertIn(ingreso_id, [r["id"] for r in res_pend2.data])

        with connection.cursor() as cur:
            cola_ids2 = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertIn(ingreso_id, cola_ids2)

    def test_equipo_con_presupuesto_no_aplica_no_aparece_en_pendientes(self):
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9902",
            motivo="baja alquiler",
            estado="reparado",
            presupuesto_estado="no_aplica",
            ubicacion_nombre="Estantería de Alquiler",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data]
        self.assertNotIn(ingreso_id, ids)

        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertNotIn(ingreso_id, cola_ids)

    def test_equipo_en_estanteria_con_presupuesto_emitido_aparece_en_presupuestados(self):
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9902",
            motivo="reparación alquiler",
            estado="reparado",
            presupuesto_estado="presupuestado",
            ubicacion_nombre="Estantería de Alquiler",
        )

        # Pendientes no lo debe tener
        res_pend = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertNotIn(ingreso_id, [r["id"] for r in res_pend.data])

        # Presupuestados sí lo debe tener
        res_pres = self.client.get("/api/ingresos/presupuestados/", HTTP_HOST="localhost")
        self.assertEqual(res_pres.status_code, 200)
        pres_ids = [r["id"] for r in res_pres.data]
        self.assertIn(ingreso_id, pres_ids)

        item = next(r for r in res_pres.data if r["id"] == ingreso_id)
        self.assertEqual(item["ubicacion_nombre"], "Estantería de Alquiler")
        self.assertEqual(item["motivo"], "reparación alquiler")

    def test_equipo_propio_dado_de_baja_sigue_pendiente_de_presupuesto(self):
        """La baja no salda el presupuesto por los daños: sale de la cola cuando Comercial
        lo resuelve, por ejemplo con "no aplica"."""
        pendiente_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9905",
            motivo="baja alquiler",
            estado="baja",
            presupuesto_estado="pendiente",
        )
        no_aplica_id = self._crear_equipo_e_ingreso(
            numero_interno="MG 9906",
            motivo="baja alquiler",
            estado="baja",
            presupuesto_estado="no_aplica",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data]
        self.assertIn(pendiente_id, ids)
        self.assertNotIn(no_aplica_id, ids)

        with connection.cursor() as cur:
            cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
            self.assertIn(pendiente_id, cola_ids)
            self.assertNotIn(no_aplica_id, cola_ids)

    def test_estados_cerrados_no_aparecen_en_pendientes(self):
        for estado_cerrado in ("ingresado", "entregado", "liberado", "no_se_repara"):
            with self.subTest(estado=estado_cerrado):
                ingreso_id = self._crear_equipo_e_ingreso(
                    motivo="baja alquiler",
                    estado=estado_cerrado,
                    presupuesto_estado="pendiente",
                    ubicacion_nombre="Estantería de Alquiler",
                )
                with connection.cursor() as cur:
                    cola_ids = _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, [])
                    self.assertNotIn(ingreso_id, cola_ids)

    def test_mg_alquilado_se_reconoce_por_el_codigo_no_solo_por_el_motivo(self):
        """El `{1,6}` dentro del f-string llegaba a Postgres como `(1, 6)`: la lista nunca
        reconocía un MG, y sólo dejaba pasar los alquilados por el motivo. El contador sí lo
        contaba, así que los dos no coincidían. Va sin espacio y con ceros para cubrir el
        criterio único."""
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="MG009904",
            motivo="reparación",
            estado="alquilado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Estantería de Alquiler",
        )

        response = self.client.get("/api/presupuestos/pendientes/", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)
        self.assertIn(ingreso_id, [row["id"] for row in response.data])

        with connection.cursor() as cur:
            self.assertIn(ingreso_id, _ids(cur, _SQL_PENDIENTES_PRESUPUESTO, []))

    def test_no_se_repara_con_presupuesto_emitido_aparece_en_presupuestados(self):
        """La salida la frena el presupuesto emitido: si no figura acá, nadie lo resuelve."""
        ingreso_id = self._crear_equipo_e_ingreso(
            numero_interno="",
            motivo="reparación",
            estado="no_se_repara",
            presupuesto_estado="presupuestado",
            ubicacion_nombre="Taller",
        )

        res_pres = self.client.get("/api/ingresos/presupuestados/", HTTP_HOST="localhost")
        self.assertEqual(res_pres.status_code, 200)
        self.assertIn(ingreso_id, [r["id"] for r in res_pres.data])
