from __future__ import annotations

import json

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase

from service.bejerman_delivery import BillingError, DeliveryOrderError
from service.delivery_orders import ensure_service_release_order_for_ingreso
from service.delivery_remito_jobs import (
    RemitoJobNotFound,
    STALE_SECONDS,
    SWEEP_AFTER_SECONDS,
    enqueue_remito_job,
    get_remito_job,
    sweep_stale_remito_jobs,
    _execute,
)


@skipUnless(connection.vendor == "postgresql", "Los trabajos de emisión requieren PostgreSQL")
class ServiceReleaseOrderTransactionTests(TransactionTestCase):
    def test_ensure_service_release_abre_transaccion_desde_autocommit(self):
        atomic_states = []

        def existing_order(_ingreso_id):
            atomic_states.append(connection.in_atomic_block)
            return "do-existing"

        self.assertFalse(connection.in_atomic_block)
        with (
            patch("service.delivery_orders._service_release_context", return_value={"estado": "liberado"}),
            patch("service.delivery_orders._ingreso_belongs_to_rental_fleet", return_value=False),
            patch("service.delivery_orders._article_mapping_for_service_release", return_value={}),
            patch("service.delivery_orders._existing_service_release_order_id", side_effect=existing_order),
            patch("service.delivery_orders._refresh_service_release_order", return_value={"id": "do-existing"}),
            patch("service.delivery_orders.connection.cursor") as cursor,
        ):
            result = ensure_service_release_order_for_ingreso(21, 1)

        self.assertEqual(result, {"id": "do-existing"})
        self.assertEqual(atomic_states, [True])
        cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
            "LOCK TABLE delivery_orders IN SHARE ROW EXCLUSIVE MODE"
        )
        self.assertFalse(connection.in_atomic_block)


@skipUnless(connection.vendor == "postgresql", "Los trabajos de emisión requieren PostgreSQL")
class _TrabajosDeRemitoBase(TransactionTestCase):
    """Esquema y actor compartidos. Sin tests propios: sólo el andamiaje."""

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


class DeliveryRemitoJobsTests(_TrabajosDeRemitoBase):
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

    def test_pedidos_concurrentes_reutilizan_una_sola_emision(self):
        inicio = Barrier(2)

        def encolar():
            try:
                inicio.wait(timeout=10)
                return enqueue_remito_job(["do-concurrente"], self.actor_id, {})
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futuros = [pool.submit(encolar) for _ in range(2)]
            resultados = [f.result(timeout=20) for f in futuros]
        self.assertEqual(resultados[0]["jobId"], resultados[1]["jobId"])
        self.assertEqual(self.spawn.call_count, 1)

    def test_error_post_envio_conserva_la_evidencia_confirmada(self):
        job = enqueue_remito_job(["do-evidencia"], self.actor_id, {})
        visible_durante_envio = []

        def envio_incierto(*args):
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE delivery_remito_jobs SET request_payload = %s::jsonb WHERE id = %s",
                    ['{"evidencia": "enviado"}', job["jobId"]],
                )
            observador = connection.copy(alias="observador_emision")
            try:
                with observador.cursor() as cur:
                    cur.execute("SELECT request_payload FROM delivery_remito_jobs WHERE id = %s", [job["jobId"]])
                    raw = cur.fetchone()[0]
                    visible_durante_envio.append(json.loads(raw) if isinstance(raw, str) else raw)
            finally:
                observador.close()
            raise DeliveryOrderError("BEJERMAN_REMITO_REVIEW_REQUIRED", "Envío sin respuesta", status_code=409)

        with patch("service.delivery_remito_jobs.generate_bejerman_remito", side_effect=envio_incierto):
            _execute(job["jobId"])
        with connection.cursor() as cur:
            cur.execute("SELECT request_payload FROM delivery_remito_jobs WHERE id = %s", [job["jobId"]])
            evidencia = cur.fetchone()[0]
            if isinstance(evidencia, str):
                evidencia = json.loads(evidencia)
        self.assertEqual(evidencia, {"evidencia": "enviado"})
        self.assertEqual(visible_durante_envio, [{"evidencia": "enviado"}])
        self.assertEqual(get_remito_job(job["jobId"])["status"], "error")

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


class BarridoDeTrabajosColgadosTests(_TrabajosDeRemitoBase):
    """El operador ya se destrababa solo; la fila no.

    Cuando la API se reinicia en el medio de una emisión, el hilo se va con ella y
    la fila queda `running` para siempre. La pestaña deja de esperar a los 10
    minutos, pero en la base el trabajo sigue figurando en curso: el panel se llena
    de emisiones eternas y nadie sabe cuáles son reales. Esto lo cierra sin
    necesidad de que alguien entre a mirar.
    """

    def _envejecer(self, job_id, segundos, estado="running"):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_remito_jobs
                   SET status = %s,
                       started_at = clock_timestamp() - (%s * INTERVAL '1 second'),
                       created_at = clock_timestamp() - (%s * INTERVAL '1 second')
                 WHERE id = %s
                """,
                [estado, segundos, segundos, job_id],
            )

    def _estado_crudo(self, job_id):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, error_payload, finished_at FROM delivery_remito_jobs WHERE id = %s",
                [job_id],
            )
            return cur.fetchone()

    def test_cierra_el_trabajo_que_quedo_corriendo_sin_nadie_atras(self):
        job = enqueue_remito_job(["do-1"], self.actor_id, {})
        self._envejecer(job["jobId"], SWEEP_AFTER_SECONDS + 60)

        self.assertEqual(sweep_stale_remito_jobs(), 1)

        status, error, finished_at = self._estado_crudo(job["jobId"])
        self.assertEqual(status, "error")
        self.assertIsNotNone(finished_at)
        payload = error if isinstance(error, dict) else __import__("json").loads(error)
        self.assertEqual(payload["code"], "DELIVERY_REMITO_JOB_STALE")
        # El mensaje no puede afirmar que falló: el remito pudo salir igual.
        self.assertIn("puede haber salido", payload["detail"])

    def test_no_toca_una_emision_que_recien_arranco(self):
        job = enqueue_remito_job(["do-2"], self.actor_id, {})
        self._envejecer(job["jobId"], 30)

        self.assertEqual(sweep_stale_remito_jobs(), 0)
        self.assertEqual(self._estado_crudo(job["jobId"])[0], "running")

    def test_espera_mas_que_la_ventana_que_ve_el_operador(self):
        # A los 10 minutos la pestaña ya dejó de esperar, pero el hilo podría seguir
        # vivo: cerrar la fila ahí sería adelantarse.
        job = enqueue_remito_job(["do-3"], self.actor_id, {})
        self._envejecer(job["jobId"], STALE_SECONDS + 60)

        self.assertEqual(sweep_stale_remito_jobs(), 0)

    def test_no_pisa_los_que_ya_terminaron(self):
        job = enqueue_remito_job(["do-4"], self.actor_id, {})
        self._envejecer(job["jobId"], SWEEP_AFTER_SECONDS + 600, estado="done")

        self.assertEqual(sweep_stale_remito_jobs(), 0)
        self.assertEqual(self._estado_crudo(job["jobId"])[0], "done")

    def test_correr_el_barrido_dos_veces_no_vuelve_a_contar_lo_mismo(self):
        job = enqueue_remito_job(["do-5"], self.actor_id, {})
        self._envejecer(job["jobId"], SWEEP_AFTER_SECONDS + 60)

        self.assertEqual(sweep_stale_remito_jobs(), 1)
        self.assertEqual(sweep_stale_remito_jobs(), 0)
