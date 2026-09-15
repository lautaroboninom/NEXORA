"""Pruebas del pase a la estantería de alquiler y sincronización de stock con Bejerman.

Verifica:
1. Al marcar 'No aplica' en el presupuesto de un equipo propio que ya está reparado,
   se mueve a la 'Estantería de Alquiler' y se encola la transferencia de stock a STL.
2. Si el equipo propio todavía no está reparado (ej. 'diagnosticado'), 'No aplica'
   no lo mueve a la estantería todavía.
3. Si el equipo es de un cliente, 'No aplica' no lo mueve a la estantería de alquiler.
4. Al cambiar la ubicación a 'Estantería de Alquiler' vía PATCH de la OS, se encola
   el pase de stock a STL para un equipo propio y la respuesta devuelve el objeto rental_shelf.
5. Al cambiar la ubicación a 'Estantería de Alquiler' para un equipo de cliente, la ubicación
   se guarda pero rental_shelf indica que no aplica y no se encola STL.
"""
from unittest import skipUnless
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.bejerman_sync import SYNC_TYPE_STOCK_LOCATION_TRANSFER
from service.constants import (
    canonical_location_name,
    bejerman_deposit_for_location,
    location_display_name,
    is_sepid_location_name,
)
from service.rental_shelf import RENTAL_SHELF_LOCATION_NAME
from service.views.helpers_impl import ensure_default_locations


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class RentalShelfTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute("""
                INSERT INTO locations(nombre) VALUES
                    ('Taller'),
                    ('Estantería de Alquiler'),
                    ('Sarmiento'),
                    ('Sepid')
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

    def test_consolida_duplicado_mojibake_de_estanteria(self):
        """Una variante histórica mal codificada no debe reaparecer en el selector."""
        alias_historico = b"Estanter\xc3\xada de Alquiler".decode("latin-1")
        with connection.cursor() as cur:
            cur.execute("INSERT INTO locations(nombre) VALUES (%s)", [alias_historico])

        ensure_default_locations()

        with connection.cursor() as cur:
            cur.execute("SELECT id, nombre FROM locations ORDER BY id")
            rows = [
                row
                for row in cur.fetchall()
                if canonical_location_name(row[1]) == RENTAL_SHELF_LOCATION_NAME
            ]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], RENTAL_SHELF_LOCATION_NAME)

    def test_consolida_duplicado_deposito_sepid(self):
        """Aliases como 'Depósito SEPID' se consolidan en 'Sepid' y reasignan FKs."""
        with connection.cursor() as cur:
            cur.execute("INSERT INTO locations(nombre) VALUES ('Depósito SEPID') RETURNING id")
            dup_loc_id = cur.fetchone()[0]

        ingreso_id = self._crear_ingreso(ubicacion_nombre="Depósito SEPID")
        with connection.cursor() as cur:
            cur.execute("SELECT device_id FROM ingresos WHERE id = %s", [ingreso_id])
            device_id = cur.fetchone()[0]
            cur.execute("UPDATE devices SET ubicacion_id = %s WHERE id = %s", [dup_loc_id, device_id])

            cur.execute("SELECT ubicacion_id FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], dup_loc_id)
            cur.execute("SELECT ubicacion_id FROM devices WHERE id = %s", [device_id])
            self.assertEqual(cur.fetchone()[0], dup_loc_id)

        ensure_default_locations()

        with connection.cursor() as cur:
            cur.execute("SELECT id, nombre FROM locations WHERE nombre = 'Sepid'")
            sepid_row = cur.fetchone()
            self.assertIsNotNone(sepid_row)
            canonical_id = sepid_row[0]

            cur.execute("SELECT 1 FROM locations WHERE id = %s", [dup_loc_id])
            self.assertIsNone(cur.fetchone())

            cur.execute("SELECT ubicacion_id FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], canonical_id)
            cur.execute("SELECT ubicacion_id FROM devices WHERE id = %s", [device_id])
            self.assertEqual(cur.fetchone()[0], canonical_id)

        self.assertTrue(is_sepid_location_name("Depósito SEPID"))
        self.assertTrue(is_sepid_location_name("Sepid"))
        self.assertEqual(canonical_location_name("Depósito SEPID"), "Sepid")
        self.assertEqual(bejerman_deposit_for_location("Depósito SEPID"), "VAL")
        self.assertEqual(location_display_name("Depósito SEPID"), "Sepid (VAL)")

    def _crear_ingreso(
        self,
        *,
        es_propio=True,
        numero_interno="MG 8801",
        numero_serie=None,
        motivo="reparación alquiler",
        estado="reparado",
        presupuesto_estado="pendiente",
        ubicacion_nombre="Taller",
    ):
        key = uuid4().hex[:8]
        if numero_serie is None:
            numero_serie = f"SER-{key}"
        num_int = numero_interno if es_propio else None

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
                VALUES (%s, %s, %s, %s, %s, 'activo')
                RETURNING id
                """,
                [customer_id, marca_id, model_id, numero_serie, num_int],
            )
            device_id = cur.fetchone()[0]

            cur.execute("SELECT id FROM locations WHERE LOWER(nombre) = LOWER(%s)", [ubicacion_nombre])
            loc_row = cur.fetchone()
            ubicacion_id = loc_row[0] if loc_row else None

            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, resolucion, presupuesto_estado, motivo, ubicacion_id, fecha_ingreso, fecha_servicio)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
                """,
                [device_id, estado, estado, presupuesto_estado, motivo, ubicacion_id],
            )
            ingreso_id = cur.fetchone()[0]
            return ingreso_id

    def test_reparado_con_no_aplica_mueve_a_estanteria_y_encola_stl(self):
        """Un equipo propio que está reparado, al marcar 'No aplica' en el presupuesto,
        se mueve a la Estantería de Alquiler y encola transferencia de stock a STL."""
        ingreso_id = self._crear_ingreso(
            es_propio=True,
            numero_interno="MG 7701",
            motivo="reparación alquiler",
            estado="reparado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Taller",
        )

        res = self.client.post(f"/api/quotes/{ingreso_id}/no-aplica/", HTTP_HOST="localhost")
        self.assertEqual(res.status_code, 200)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.presupuesto_estado, loc.nombre
                  FROM ingresos i
                  LEFT JOIN locations loc ON loc.id = i.ubicacion_id
                 WHERE i.id = %s
                """,
                [ingreso_id],
            )
            presu, ubic = cur.fetchone()
            self.assertEqual(presu, "no_aplica")
            self.assertEqual(ubic, RENTAL_SHELF_LOCATION_NAME)

            # Comprobar que se encoló el job de transferencia a STL
            cur.execute(
                """
                SELECT sync_type, target_deposit, status
                  FROM bejerman_sync_jobs
                 WHERE ingreso_id = %s
                """,
                [ingreso_id],
            )
            job = cur.fetchone()
            self.assertIsNotNone(job, "Debe existir un job de sincronización en bejerman_sync_jobs")
            self.assertEqual(job[0], "stock_str_to_stl")
            self.assertEqual(job[1], "STL")

    def test_no_aplica_en_equipo_propio_no_reparado_no_mueve_a_estanteria(self):
        """Si el equipo propio aún está en taller (ej. diagnosticado), 'No aplica'
        deja el presupuesto resuelto pero no lo mueve a la estantería."""
        ingreso_id = self._crear_ingreso(
            es_propio=True,
            numero_interno="MG 7702",
            motivo="reparación alquiler",
            estado="diagnosticado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Taller",
        )

        res = self.client.post(f"/api/quotes/{ingreso_id}/no-aplica/", HTTP_HOST="localhost")
        self.assertEqual(res.status_code, 200)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.presupuesto_estado, loc.nombre
                  FROM ingresos i
                  LEFT JOIN locations loc ON loc.id = i.ubicacion_id
                 WHERE i.id = %s
                """,
                [ingreso_id],
            )
            presu, ubic = cur.fetchone()
            self.assertEqual(presu, "no_aplica")
            self.assertEqual(ubic, "Taller")

            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], 0)

    def test_no_aplica_en_equipo_de_cliente_no_mueve_a_estanteria(self):
        """Un equipo de cliente reparado no va a la estantería de alquiler."""
        ingreso_id = self._crear_ingreso(
            es_propio=False,
            motivo="reparación",
            estado="reparado",
            presupuesto_estado="pendiente",
            ubicacion_nombre="Taller",
        )

        res = self.client.post(f"/api/quotes/{ingreso_id}/no-aplica/", HTTP_HOST="localhost")
        self.assertEqual(res.status_code, 200)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.presupuesto_estado, loc.nombre
                  FROM ingresos i
                  LEFT JOIN locations loc ON loc.id = i.ubicacion_id
                 WHERE i.id = %s
                """,
                [ingreso_id],
            )
            presu, ubic = cur.fetchone()
            self.assertEqual(presu, "no_aplica")
            self.assertEqual(ubic, "Taller")

    def test_patch_ubicacion_a_estanteria_encola_stl_para_propio(self):
        """Cambiar la ubicación a Estantería de Alquiler en un equipo propio encola
        el pase de stock a STL en Bejerman y devuelve los datos en la respuesta."""
        ingreso_id = self._crear_ingreso(
            es_propio=True,
            numero_interno="MG 7703",
            motivo="reparación alquiler",
            estado="reparado",
            presupuesto_estado="no_aplica",
            ubicacion_nombre="Taller",
        )

        with connection.cursor() as cur:
            cur.execute("SELECT id FROM locations WHERE LOWER(nombre) = LOWER(%s)", [RENTAL_SHELF_LOCATION_NAME])
            estanteria_id = cur.fetchone()[0]

        res = self.client.patch(
            f"/api/ingresos/{ingreso_id}/",
            {"ubicacion_id": estanteria_id, "confirm_bejerman_location_change": True},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("rental_shelf", res.data)
        self.assertTrue(res.data["rental_shelf"]["applies"])
        self.assertTrue(res.data["rental_shelf"]["stock_queued"])

        with connection.cursor() as cur:
            cur.execute("SELECT ubicacion_id FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], estanteria_id)

            cur.execute(
                """
                SELECT sync_type, target_deposit
                  FROM bejerman_sync_jobs
                 WHERE ingreso_id = %s
                """,
                [ingreso_id],
            )
            job = cur.fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "stock_str_to_stl")
            self.assertEqual(job[1], "STL")

    def test_patch_ubicacion_a_estanteria_para_equipo_cliente_no_encola_stl(self):
        """Cambiar la ubicación a Estantería de Alquiler en un equipo de cliente
        actualiza la ubicación pero no encola pase de stock a STL."""
        ingreso_id = self._crear_ingreso(
            es_propio=False,
            motivo="reparación",
            estado="reparado",
            presupuesto_estado="no_aplica",
            ubicacion_nombre="Taller",
        )

        with connection.cursor() as cur:
            cur.execute("SELECT id FROM locations WHERE LOWER(nombre) = LOWER(%s)", [RENTAL_SHELF_LOCATION_NAME])
            estanteria_id = cur.fetchone()[0]

        res = self.client.patch(
            f"/api/ingresos/{ingreso_id}/",
            {"ubicacion_id": estanteria_id, "confirm_bejerman_location_change": True},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("rental_shelf", res.data)
        self.assertFalse(res.data["rental_shelf"]["applies"])
        self.assertFalse(res.data["rental_shelf"]["stock_queued"])

        with connection.cursor() as cur:
            cur.execute("SELECT ubicacion_id FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], estanteria_id)

            cur.execute("SELECT COUNT(*) FROM bejerman_sync_jobs WHERE ingreso_id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], 0)

    def test_patch_ubicacion_a_sarmiento_encola_traslado_confirmado(self):
        """Un cambio entre Taller y Sarmiento usa STR→SAR sólo tras confirmarlo."""
        ingreso_id = self._crear_ingreso(
            es_propio=False,
            motivo="reparación",
            estado="diagnosticado",
            ubicacion_nombre="Taller",
        )
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM locations WHERE LOWER(nombre) = LOWER(%s)", ["Sarmiento"])
            sarmiento_id = cur.fetchone()[0]

        not_confirmed = self.client.patch(
            f"/api/ingresos/{ingreso_id}/",
            {"ubicacion_id": sarmiento_id},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(not_confirmed.status_code, 400)
        self.assertTrue(not_confirmed.data["confirmation_required"])

        res = self.client.patch(
            f"/api/ingresos/{ingreso_id}/",
            {"ubicacion_id": sarmiento_id, "confirm_bejerman_location_change": True},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["bejerman_location_sync"]["queued"])
        self.assertEqual(res.data["bejerman_location_sync"]["source_deposit"], "STR")
        self.assertEqual(res.data["bejerman_location_sync"]["target_deposit"], "SAR")

        with connection.cursor() as cur:
            cur.execute("SELECT ubicacion_id FROM ingresos WHERE id = %s", [ingreso_id])
            self.assertEqual(cur.fetchone()[0], sarmiento_id)
            cur.execute(
                """
                SELECT sync_type, source_deposit, target_deposit
                  FROM bejerman_sync_jobs
                 WHERE ingreso_id = %s
                """,
                [ingreso_id],
            )
            self.assertEqual(
                cur.fetchone(),
                (SYNC_TYPE_STOCK_LOCATION_TRANSFER, "STR", "SAR"),
            )

    def test_general_equipos_export_canonicaliza_filtro_ubicacion(self):
        """La exportación a Excel de GeneralEquipos filtra con nombres canónicos y aliases."""
        ingreso_id = self._crear_ingreso(ubicacion_nombre="Sepid")
        res = self.client.get(
            "/api/ingresos/historico/export/?ubicacion=Dep%C3%B3sito%20SEPID",
            HTTP_HOST="localhost",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_historico_deleted_row_matches_con_alias_sepid(self):
        """_historico_deleted_row_matches compara ubicaciones por forma canónica."""
        from service.views.ingresos_views import _historico_deleted_row_matches

        row = {
            "estado": "eliminado",
            "ubicacion_nombre": "Sepid",
            "ubicacion_id": 14,
        }
        # Filtro con alias histórico "Depósito SEPID"
        self.assertTrue(
            _historico_deleted_row_matches(row, {"ubic": "Depósito SEPID"}),
        )
        # Filtro con etiqueta con código Bejerman "Sepid (VAL)"
        self.assertTrue(
            _historico_deleted_row_matches(row, {"ubic": "Sepid (VAL)"}),
        )
        # Filtro con otra ubicación distinta
        self.assertFalse(
            _historico_deleted_row_matches(row, {"ubic": "Taller"}),
        )
