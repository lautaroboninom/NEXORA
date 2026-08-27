from __future__ import annotations

from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from service.bejerman_delivery import BillingError, DeliveryOrderError
from service.delivery_remito_jobs import (
    RemitoJobNotFound,
    STALE_SECONDS,
    enqueue_remito_job,
    get_remito_job,
    _execute,
)


@skipUnless(connection.vendor == "postgresql", "Los trabajos de emisión requieren PostgreSQL")
class DeliveryRemitoJobsTests(TransactionTestCase):
    reset_sequences = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  nombre TEXT NOT NULL,
                  email TEXT NOT NULL UNIQUE,
                  hash_pw TEXT NOT NULL,
                  rol TEXT NOT NULL,
                  activo BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
        call_command("apply_delivery_remito_jobs_schema")

    def setUp(self):
        # TransactionTestCase sí corre los `on_commit`, así que encolar largaría un
        # hilo de verdad contra la emisión real. Acá se prueba el circuito del
        # trabajo; la ejecución se dispara a mano con `_execute`.
        spawn = patch("service.delivery_remito_jobs._spawn")
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)
        with connection.cursor() as cur:
            cur.execute("DELETE FROM delivery_remito_jobs")
            # El actor lleva FK a users: sin alguien real no se puede encolar nada.
            cur.execute(
                """
                INSERT INTO users (nombre, email, hash_pw, rol)
                VALUES ('Recepción', 'recepcion@test.local', 'x', 'recepcion')
                ON CONFLICT (email) DO UPDATE SET nombre = EXCLUDED.nombre
                RETURNING id
                """
            )
            self.actor_id = cur.fetchone()[0]

    # -- encolar ---------------------------------------------------------------

    def test_encolar_devuelve_un_trabajo_pendiente_sin_emitir_todavia(self):
        with patch("service.delivery_remito_jobs.generate_bejerman_remito") as emitir:
            job = enqueue_remito_job(["do-1"], self.actor_id, {"notes": "x"})
        self.assertEqual(job["status"], "pending")
        self.assertFalse(job["done"])
        self.assertTrue(job["jobId"].startswith("drj-"))
        # Lo importante: contestar no puede haber hablado con Bejerman.
        emitir.assert_not_called()

    def test_sin_ordenes_falla_en_el_acto(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            enqueue_remito_job([], self.actor_id, {})
        self.assertEqual(ctx.exception.code, "ORDER_IDS_REQUIRED")

    def test_un_segundo_pedido_de_las_mismas_ordenes_reusa_el_trabajo_en_curso(self):
        primero = enqueue_remito_job(["do-1", "do-2"], self.actor_id, {})
        segundo = enqueue_remito_job(["do-2", "do-1"], self.actor_id, {})
        self.assertEqual(primero["jobId"], segundo["jobId"])

    def test_ordenes_distintas_abren_trabajos_distintos(self):
        primero = enqueue_remito_job(["do-1"], self.actor_id, {})
        segundo = enqueue_remito_job(["do-9"], self.actor_id, {})
        self.assertNotEqual(primero["jobId"], segundo["jobId"])

    # -- ejecutar --------------------------------------------------------------

    def test_el_resultado_queda_guardado_para_quien_lo_venga_a_buscar(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {"notes": "n"})
        with patch(
            "service.delivery_remito_jobs.generate_bejerman_remito",
            return_value={"remitoNumber": "00004-00005040", "success": True},
        ) as emitir:
            _execute(job["jobId"])

        emitir.assert_called_once_with(["do-1"], self.actor_id, {"notes": "n"})
        estado = get_remito_job(job["jobId"])
        self.assertEqual(estado["status"], "done")
        self.assertTrue(estado["done"])
        self.assertEqual(estado["result"]["remitoNumber"], "00004-00005040")

    def test_un_error_de_negocio_conserva_codigo_y_status(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        with patch(
            "service.delivery_remito_jobs.generate_bejerman_remito",
            side_effect=DeliveryOrderError("DELIVERY_REMITO_STOCK", "Falta stock", status_code=409),
        ):
            _execute(job["jobId"])

        estado = get_remito_job(job["jobId"])
        self.assertEqual(estado["status"], "error")
        self.assertEqual(estado["error"]["code"], "DELIVERY_REMITO_STOCK")
        self.assertEqual(estado["error"]["status"], 409)
        self.assertEqual(estado["error"]["detail"], "Falta stock")

    def test_un_billing_error_conserva_su_mensaje_accionable(self):
        # BillingError no hereda de DeliveryOrderError: si se lo trata como error
        # inesperado, el operador pierde el "reintentá en unos minutos" y ve un
        # "error interno" que no le dice qué hacer.
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        with patch(
            "service.delivery_remito_jobs.generate_bejerman_remito",
            side_effect=BillingError(
                "DELIVERY_REMITO_STOCK_SYNC_UNKNOWN",
                "No se pudo verificar si el stock terminó de sincronizarse.",
                status_code=503,
            ),
        ):
            _execute(job["jobId"])

        estado = get_remito_job(job["jobId"])
        self.assertEqual(estado["status"], "error")
        self.assertEqual(estado["error"]["code"], "DELIVERY_REMITO_STOCK_SYNC_UNKNOWN")
        self.assertEqual(estado["error"]["status"], 503)
        self.assertIn("sincronizarse", estado["error"]["detail"])

    def test_un_error_inesperado_no_filtra_el_detalle_pero_avisa_que_puede_haber_salido(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        with patch(
            "service.delivery_remito_jobs.generate_bejerman_remito",
            side_effect=RuntimeError("psycopg2: connection refused a las tripas"),
        ):
            _execute(job["jobId"])

        estado = get_remito_job(job["jobId"])
        self.assertEqual(estado["status"], "error")
        self.assertEqual(estado["error"]["code"], "DELIVERY_REMITO_JOB_FAILED")
        self.assertNotIn("psycopg2", estado["error"]["detail"])
        self.assertIn("revise el estado de la orden", estado["error"]["detail"])

    def test_no_se_emite_dos_veces_si_dos_hilos_toman_el_mismo_trabajo(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        with patch(
            "service.delivery_remito_jobs.generate_bejerman_remito",
            return_value={"remitoNumber": "R-1"},
        ) as emitir:
            _execute(job["jobId"])
            _execute(job["jobId"])  # el segundo ya no lo puede reclamar
        self.assertEqual(emitir.call_count, 1)

    # -- consultar -------------------------------------------------------------

    def test_un_trabajo_que_no_existe_da_404_y_no_revienta(self):
        with self.assertRaises(RemitoJobNotFound) as ctx:
            get_remito_job("drj-inventado")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_un_trabajo_abandonado_deja_de_prometer_que_siga_esperando(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        # Simula el reinicio del servidor: quedó 'running' y nadie lo terminó.
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_remito_jobs
                   SET status = 'running',
                       started_at = clock_timestamp() - (%s * INTERVAL '1 second')
                 WHERE id = %s
                """,
                [STALE_SECONDS + 60, job["jobId"]],
            )

        estado = get_remito_job(job["jobId"])
        self.assertEqual(estado["status"], "unknown")
        self.assertTrue(estado["done"])
        self.assertEqual(estado["error"]["code"], "DELIVERY_REMITO_JOB_STALE")
        # No se afirma que falló: el remito puede haber salido igual en Bejerman.
        self.assertIn("puede haber salido", estado["error"]["detail"])

    def test_un_trabajo_abandonado_no_bloquea_una_emision_nueva(self):
        viejo = enqueue_remito_job(["do-1"], self.actor_id, {})
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_remito_jobs
                   SET started_at = clock_timestamp() - (%s * INTERVAL '1 second'),
                       created_at = clock_timestamp() - (%s * INTERVAL '1 second')
                 WHERE id = %s
                """,
                [STALE_SECONDS + 60, STALE_SECONDS + 60, viejo["jobId"]],
            )

        nuevo = enqueue_remito_job(["do-1"], self.actor_id, {})
        self.assertNotEqual(viejo["jobId"], nuevo["jobId"])
