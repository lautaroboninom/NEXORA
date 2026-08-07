from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from service.bejerman_pdf_requests import (
    BejermanPdfFolderUnavailableError,
    BejermanPdfNotPublishedError,
    BejermanPdfRequestPending,
    BejermanPdfWorkerStalledError,
    document_key_for,
    fetch_or_enqueue_comprobante_pdf,
    pdf_error_metadata,
    process_bejerman_pdf_requests_once,
    record_worker_heartbeat,
    requeue_pdf_request,
    worker_liveness,
)
from service.bejerman_sdk import (
    BejermanPdfReference,
    BejermanSDKClient,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
)


PDF_BYTES = b"%PDF-1.7\nPDF-OFICIAL-BEJERMAN\n%%EOF"
REMOTE_PATH = r"samtronic:C:\3. MGBIO\PDF\REMITOS\RTR00007-00000683-JORGE.pdf"


class BejermanPdfSdkContractTests(SimpleTestCase):
    def test_generacion_pdf_no_reintenta_un_resultado_ambiguo(self):
        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.token = "token"
        client._execute_once = Mock(
            side_effect=BejermanSdkResponseError("Invalid object name 'expCuotasV'.")
        )
        reference = BejermanPdfReference(
            type="RT",
            number="00000683",
            letter="R",
            point_of_sale="00007",
            issue_date="2026-07-14",
        )

        with self.assertRaises(BejermanSdkResponseError):
            client.generar_comprobante_ventas_pdf(reference)

        client._execute_once.assert_called_once_with(
            "VENTAS",
            "WS_GenerarComprobantePDF",
            params_json=["RT", "00000683", "R", "00007", "14/07/2026"],
        )


@skipUnless(connection.vendor == "postgresql", "El coordinador de PDFs requiere PostgreSQL")
@override_settings(
    BEJERMAN_PDF_WAIT_DEADLINE_SECONDS=600,
    BEJERMAN_PDF_FOLDER_CHECK_SECONDS=1,
    BEJERMAN_PDF_COPY_RETRY_SECONDS=1,
    BEJERMAN_PDF_GENERATION_CONNECT_TIMEOUT=10,
    BEJERMAN_PDF_GENERATION_READ_TIMEOUT=90,
    BEJERMAN_PDF_SDK_READ_TIMEOUT=30,
    BEJERMAN_PDF_SDK_CHECK_SECONDS=1,
    BEJERMAN_PDF_MAX_REGENERATIONS=2,
    BEJERMAN_PDF_WORKER_STALL_GRACE_SECONDS=90,
    BEJERMAN_PDF_DEADLINE_GRACE_SECONDS=120,
)
class BejermanPdfRequestsTests(TransactionTestCase):
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
                  activo BOOLEAN NOT NULL DEFAULT TRUE,
                  bejerman_seller_code TEXT NULL,
                  bejerman_seller_code_confirmed_at TIMESTAMPTZ NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bejerman_remito_groups (
                  id                         TEXT PRIMARY KEY,
                  company_key                TEXT NULL,
                  comprobante_tipo           TEXT NOT NULL,
                  comprobante_letra          TEXT NOT NULL DEFAULT 'R',
                  comprobante_pto_venta      TEXT NULL,
                  comprobante_numero         TEXT NULL,
                  remito_number              TEXT NULL,
                  customer_code              TEXT NOT NULL,
                  customer_name              TEXT NOT NULL,
                  seller_code                TEXT NOT NULL,
                  payment_term_code          TEXT NOT NULL,
                  operation_code             TEXT NOT NULL,
                  deposit_code               TEXT NOT NULL,
                  status                     TEXT NOT NULL DEFAULT 'pending',
                  order_ids                  JSONB NOT NULL DEFAULT '[]'::jsonb,
                  response_summary           JSONB NOT NULL DEFAULT '{}'::jsonb,
                  created_at                 TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  generated_at               TIMESTAMPTZ NULL
                )
                """
            )
        call_command("apply_bejerman_worker_heartbeat_schema", verbosity=0)
        call_command("apply_bejerman_pdf_requests_schema", verbosity=0)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_pdf_requests")
            cur.execute("DELETE FROM bejerman_worker_heartbeats")

    @staticmethod
    def _beat(stage: str = "tick") -> None:
        """Deja al worker como vivo para los caminos que no prueban el latido."""

        record_worker_heartbeat(stage=stage)

    def _set_regenerations(self, document_key: str, count: int) -> None:
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_pdf_requests SET regeneration_count=%s WHERE document_key=%s",
                [count, document_key],
            )

    @staticmethod
    def _reference() -> BejermanPdfReference:
        return BejermanPdfReference(
            type="RT",
            number="00000683",
            letter="R",
            point_of_sale="00007",
            issue_date="2026-07-14",
            customer_code="JORGE",
        )

    @staticmethod
    def _client() -> Mock:
        client = Mock()
        client.company_key = "MGBIO"
        client.actor_user_id = None
        return client

    def _row(self, document_key: str) -> dict | None:
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM bejerman_pdf_requests WHERE document_key = %s", [document_key])
            raw = cur.fetchone()
            if raw is None:
                return None
            return dict(zip([column[0] for column in cur.description], raw))

    def _count_rows(self) -> int:
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bejerman_pdf_requests")
            return int(cur.fetchone()[0])

    def _enqueue(self, client: Mock, reference: BejermanPdfReference) -> BejermanPdfRequestPending:
        with self.assertRaises(BejermanPdfRequestPending) as ctx:
            fetch_or_enqueue_comprobante_pdf(client, reference)
        return ctx.exception

    def _force_due(self, document_key: str) -> None:
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_pdf_requests SET next_folder_check_at = CURRENT_TIMESTAMP WHERE document_key = %s",
                [document_key],
            )

    def test_carpeta_existente_devuelve_pdf_sin_fila_ni_soap(self):
        client = self._client()
        reference = self._reference()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(PDF_BYTES, "ready"),
            ) as probe,
        ):
            pdf, content_type = fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(pdf, PDF_BYTES)
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(self._count_rows(), 0)
        probe.assert_called_once_with(client, reference, interactive=True, allow_directory_scan=False)
        client.register.assert_not_called()
        client.generar_comprobante_ventas_pdf.assert_not_called()

    def test_carpeta_ausente_encola_una_solicitud_sin_soap(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
        ):
            pending = self._enqueue(client, reference)

        row = self._row(document_key)
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["generation_attempt_count"], 0)
        self.assertEqual(row["customer_code"], "JORGE")
        self.assertEqual(row["remote_path"], REMOTE_PATH)
        self.assertEqual(pending.pdf_state, "queued")
        self.assertFalse(pending.generation_requested)
        probe.assert_called_once_with(client, reference, interactive=True, allow_directory_scan=False)
        client.register.assert_not_called()
        client.generar_comprobante_ventas_pdf.assert_not_called()

    def test_error_de_carpeta_devuelve_503_y_no_crea_fila(self):
        client = self._client()
        reference = self._reference()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "error"),
            ) as probe,
        ):
            with self.assertRaises(BejermanPdfFolderUnavailableError) as ctx:
                fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_FOLDER_UNAVAILABLE")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(ctx.exception.generation_requested)
        self.assertEqual(self._count_rows(), 0)
        probe.assert_called_once_with(client, reference, interactive=True, allow_directory_scan=False)
        client.register.assert_not_called()
        client.generar_comprobante_ventas_pdf.assert_not_called()

    def test_worker_genera_una_sola_vez_y_deja_el_pdf_en_carpeta(self):
        request_client = self._client()
        worker_client = self._client()
        reference = self._reference()
        document_key = document_key_for(request_client, reference)
        worker_client.generar_comprobante_ventas_pdf.return_value = {"DatosJSON": {}}

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
            patch("service.bejerman_pdf_requests.BejermanSDKClient", return_value=worker_client),
            patch("service.bejerman_pdf_requests.extract_pdf_bytes", return_value=PDF_BYTES),
            patch("service.bejerman_pdf_requests.store_comprobante_pdf_copy", return_value=True) as store,
        ):
            self._enqueue(request_client, reference)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)
            self.assertEqual(process_bejerman_pdf_requests_once(), 0)

        row = self._row(document_key)
        self.assertEqual(row["state"], "ready_folder")
        self.assertEqual(row["generation_attempt_count"], 1)
        self.assertIsNone(row["pdf_payload"])
        worker_client.register.assert_called_once_with()
        worker_client.generar_comprobante_ventas_pdf.assert_called_once()
        store.assert_called_once()
        # Primer acceso (HTTP) + primer paso del worker: ninguno debe escanear la carpeta.
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertFalse(call.kwargs["allow_directory_scan"])

    def test_timeout_ambiguo_no_vuelve_a_generar(self):
        request_client = self._client()
        worker_client = self._client()
        reference = self._reference()
        document_key = document_key_for(request_client, reference)
        worker_client.generar_comprobante_ventas_pdf.side_effect = BejermanSdkUnavailable("Read timed out")
        worker_client.consultar_comprobante_ventas_pdf.return_value = {"DatosJSON": {}}

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
            patch("service.bejerman_pdf_requests.BejermanSDKClient", return_value=worker_client),
        ):
            self._enqueue(request_client, reference)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)

            row = self._row(document_key)
            self.assertEqual(row["state"], "waiting_sdk")
            self.assertEqual(row["generation_attempt_count"], 1)
            self.assertEqual(row["last_error_code"], "BEJERMAN_PDF_GENERATION_AMBIGUOUS")

            with self.assertRaises(BejermanPdfRequestPending) as pending_ctx:
                fetch_or_enqueue_comprobante_pdf(request_client, reference)
            self.assertTrue(pending_ctx.exception.generation_requested)

            self._force_due(document_key)
            self.assertEqual(process_bejerman_pdf_requests_once(), 0)

        worker_client.register.assert_called_once_with()
        worker_client.generar_comprobante_ventas_pdf.assert_called_once()
        worker_client.consultar_comprobante_ventas_pdf.assert_called_once()
        self.assertEqual(self._row(document_key)["state"], "waiting_sdk")
        # Primer acceso (HTTP) + primer paso del worker: ninguno debe escanear la carpeta
        # (el reintento posterior de "waiting_sdk" pasa por _process_sdk_wait, que no
        # toca la carpeta en absoluto, así que el conteo no debe subir de 2).
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertFalse(call.kwargs["allow_directory_scan"])

    def test_worker_consulta_bytes_oficiales_y_guarda_directo_en_remitos(self):
        request_client = self._client()
        worker_client = self._client()
        reference = self._reference()
        document_key = document_key_for(request_client, reference)
        worker_client.generar_comprobante_ventas_pdf.return_value = {"DatosJSON": {}}
        worker_client.consultar_comprobante_ventas_pdf.return_value = {"DatosJSON": {}}

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
            patch("service.bejerman_pdf_requests.BejermanSDKClient", return_value=worker_client),
            patch(
                "service.bejerman_pdf_requests.extract_pdf_bytes",
                side_effect=[None, PDF_BYTES],
            ),
            patch("service.bejerman_pdf_requests.store_comprobante_pdf_copy", return_value=True) as store,
        ):
            self._enqueue(request_client, reference)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)
            self.assertEqual(self._row(document_key)["state"], "waiting_sdk")

            self._force_due(document_key)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)

        row = self._row(document_key)
        self.assertEqual(row["state"], "ready_folder")
        self.assertEqual(row["customer_code"], "JORGE")
        worker_client.generar_comprobante_ventas_pdf.assert_called_once()
        worker_client.consultar_comprobante_ventas_pdf.assert_called_once()
        consulted_reference = worker_client.consultar_comprobante_ventas_pdf.call_args.args[0]
        self.assertEqual(consulted_reference.customer_code, "JORGE")
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertFalse(call.kwargs["allow_directory_scan"])
        store.assert_called_once()
        self.assertEqual(store.call_args.args[2], PDF_BYTES)

    def test_ready_direct_conserva_payload_y_reintenta_solamente_la_copia(self):
        request_client = self._client()
        worker_client = self._client()
        reference = self._reference()
        document_key = document_key_for(request_client, reference)
        worker_client.generar_comprobante_ventas_pdf.return_value = {"DatosJSON": {}}

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
            patch("service.bejerman_pdf_requests.BejermanSDKClient", return_value=worker_client),
            patch("service.bejerman_pdf_requests.extract_pdf_bytes", return_value=PDF_BYTES),
            patch(
                "service.bejerman_pdf_requests.store_comprobante_pdf_copy",
                side_effect=[False, True],
            ) as store,
        ):
            self._enqueue(request_client, reference)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)

            row = self._row(document_key)
            self.assertEqual(row["state"], "ready_direct")
            self.assertEqual(bytes(row["pdf_payload"]), PDF_BYTES)

            pdf, content_type = fetch_or_enqueue_comprobante_pdf(request_client, reference)
            self.assertEqual(pdf, PDF_BYTES)
            self.assertEqual(content_type, "application/pdf")

            self._force_due(document_key)
            self.assertEqual(process_bejerman_pdf_requests_once(), 1)

        final_row = self._row(document_key)
        self.assertEqual(final_row["state"], "ready_folder")
        self.assertIsNone(final_row["pdf_payload"])
        worker_client.generar_comprobante_ventas_pdf.assert_called_once()
        self.assertEqual(store.call_count, 2)
        self.assertEqual([call.args[2] for call in store.call_args_list], [PDF_BYTES, PDF_BYTES])
        # Primer acceso (HTTP) + primer paso del worker; el "ready_direct" y el
        # reintento de copia no vuelven a tocar la carpeta remota.
        self.assertEqual(probe.call_count, 2)
        for call in probe.call_args_list:
            self.assertFalse(call.kwargs["allow_directory_scan"])

    def test_deadline_devuelve_504_y_admite_recuperacion_tardia_desde_carpeta(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)
        self._beat()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                side_effect=[(None, "missing"), (None, "missing"), (PDF_BYTES, "ready")],
            ) as probe,
        ):
            self._enqueue(client, reference)
            # regeneration_count al tope: este caso cubre el final del camino, ya
            # sin ciclos de generación disponibles (el reintento tiene su test).
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_pdf_requests
                       SET state='waiting_folder', generation_attempt_count=1,
                           regeneration_count=2,
                           deadline_at=CURRENT_TIMESTAMP - INTERVAL '1 second'
                     WHERE document_key=%s
                    """,
                    [document_key],
                )

            with self.assertRaises(BejermanPdfNotPublishedError) as ctx:
                fetch_or_enqueue_comprobante_pdf(client, reference)

            self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_NOT_PUBLISHED")
            self.assertEqual(ctx.exception.status_code, 504)
            self.assertTrue(ctx.exception.generation_requested)
            self.assertEqual(self._row(document_key)["state"], "failed")

            pdf, content_type = fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(pdf, PDF_BYTES)
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(self._row(document_key)["state"], "ready_folder")
        client.register.assert_not_called()
        client.generar_comprobante_ventas_pdf.assert_not_called()
        # (1) primer acceso: sin escaneo. (2) vencimiento de waiting_folder y
        # (3) recheck del estado terminal "failed": ambos son chequeos de
        # última oportunidad, sí escanean la carpeta completa.
        self.assertEqual(
            [call.kwargs["allow_directory_scan"] for call in probe.call_args_list],
            [False, True, True],
        )

    # --- Recuperación: un PDF trabado tiene que poder destrabarse -------------

    def test_reencolar_no_genera_si_el_archivo_ya_esta_en_la_carpeta(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)
        self._beat()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                side_effect=[(None, "missing"), (PDF_BYTES, "ready")],
            ),
        ):
            self._enqueue(client, reference)
            result = requeue_pdf_request(document_key, reason="test")

        self.assertEqual(result["outcome"], "ready")
        self.assertEqual(self._row(document_key)["state"], "ready_folder")
        # Encontrarlo en la carpeta no puede costar una generación nueva.
        self.assertEqual(self._row(document_key)["regeneration_count"], 0)
        client.generar_comprobante_ventas_pdf.assert_not_called()

    def test_reencolar_abre_un_ciclo_nuevo_sin_romper_el_single_shot(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)
        self._beat()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_pdf_requests
                       SET state='failed', generation_attempt_count=1,
                           last_error_code='BEJERMAN_PDF_NOT_PUBLISHED'
                     WHERE document_key=%s
                    """,
                    [document_key],
                )

            result = requeue_pdf_request(document_key, reason="test", actor_user_id=None)

        self.assertEqual(result["outcome"], "requeued")
        row = self._row(document_key)
        self.assertEqual(row["state"], "queued")
        # El contador de intentos vuelve a 0 porque cuenta *dentro* de un ciclo:
        # así el CHECK 0..1 sigue valiendo y Bejerman recibe una sola generación
        # por ciclo, pero el PDF deja de estar perdido para siempre.
        self.assertEqual(row["generation_attempt_count"], 0)
        self.assertEqual(row["regeneration_count"], 1)
        self.assertIsNone(row["last_error_code"])

    def test_reencolar_respeta_el_tope_de_ciclos(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)
        self._beat()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
        ):
            self._enqueue(client, reference)
            self._set_regenerations(document_key, 2)
            result = requeue_pdf_request(document_key, reason="test")
            forced = requeue_pdf_request(document_key, reason="test", force=True)

        self.assertEqual(result["outcome"], "exhausted")
        self.assertEqual(self._row(document_key)["regeneration_count"], 3)
        self.assertEqual(forced["outcome"], "requeued")

    def test_estado_failed_deja_de_ser_definitivo(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)
        self._beat()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE bejerman_pdf_requests SET state='failed', generation_attempt_count=1 WHERE document_key=%s",
                    [document_key],
                )

            # Antes esto devolvía 504 para siempre: el comprobante quedaba sin PDF
            # posible aunque estuviera perfecto en Bejerman.
            with self.assertRaises(BejermanPdfRequestPending):
                fetch_or_enqueue_comprobante_pdf(client, reference)

        row = self._row(document_key)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["generation_attempt_count"], 0)
        client.generar_comprobante_ventas_pdf.assert_not_called()

    # --- Latido del worker ---------------------------------------------------

    def test_queued_sin_latido_corta_el_202_eterno(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE bejerman_pdf_requests SET created_at=CURRENT_TIMESTAMP - INTERVAL '5 minutes' WHERE document_key=%s",
                    [document_key],
                )

            with self.assertRaises(BejermanPdfWorkerStalledError) as ctx:
                fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_WORKER_STALLED")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIs(pdf_error_metadata(ctx.exception)["worker_alive"], False)
        # No toca la fila: cuando el worker vuelve, genera igual y una sola vez.
        row = self._row(document_key)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["generation_attempt_count"], 0)

    def test_queued_con_latido_fresco_sigue_pendiente(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE bejerman_pdf_requests SET created_at=CURRENT_TIMESTAMP - INTERVAL '5 minutes' WHERE document_key=%s",
                    [document_key],
                )
            self._beat()

            with self.assertRaises(BejermanPdfRequestPending) as ctx:
                fetch_or_enqueue_comprobante_pdf(client, reference)

        payload = pdf_error_metadata(ctx.exception)
        self.assertIs(payload["worker_alive"], True)
        self.assertEqual(payload["stage_label"], "En cola para generar el remito")
        self.assertEqual(payload["document_key"], document_key)
        self.assertGreaterEqual(payload["queued_seconds"], 300)

    def test_sin_tabla_de_latidos_el_comportamiento_no_cambia(self):
        """Fail-open: durante el deploy la tabla puede no existir todavía."""

        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
            patch("service.bejerman_pdf_requests._heartbeat_table_ready", return_value=False),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE bejerman_pdf_requests SET created_at=CURRENT_TIMESTAMP - INTERVAL '5 minutes' WHERE document_key=%s",
                    [document_key],
                )

            self.assertIsNone(worker_liveness()["alive"])
            with self.assertRaises(BejermanPdfRequestPending):
                fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(self._row(document_key)["state"], "queued")

    def test_el_worker_registra_latido_por_documento(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ),
            patch("service.bejerman_pdf_requests._client_from_row", return_value=client),
        ):
            self._enqueue(client, reference)
            self._force_due(document_key)
            process_bejerman_pdf_requests_once(limit=1, heartbeat=True)

        worker = worker_liveness()
        self.assertIs(worker["alive"], True)
        self.assertEqual(worker["stage"], "processing:queued")
        self.assertEqual(worker["current_document_key"], document_key)

    def test_veinticuatro_solicitudes_concurrentes_crean_una_sola_fila(self):
        reference = self._reference()

        def request_pdf(_index: int) -> tuple[str, bool]:
            close_old_connections()
            try:
                client = self._client()
                try:
                    fetch_or_enqueue_comprobante_pdf(client, self._reference())
                except BejermanPdfRequestPending as exc:
                    return exc.pdf_state, exc.generation_requested
                raise AssertionError("La carpeta ausente debía dejar la solicitud pendiente")
            finally:
                close_old_connections()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                return_value=(None, "missing"),
            ) as probe,
        ):
            with ThreadPoolExecutor(max_workers=24) as executor:
                results = list(executor.map(request_pdf, range(24)))

        self.assertEqual(len(results), 24)
        self.assertTrue(all(state == "queued" for state, _requested in results))
        self.assertTrue(all(not requested for _state, requested in results))
        self.assertEqual(self._count_rows(), 1)
        row = self._row(document_key_for(self._client(), reference))
        self.assertEqual(row["generation_attempt_count"], 0)
        # Esta es la aserción que realmente cierra el bug: sin el lock por
        # documento, las 24 llamadas concurrentes dispararían 24 probes
        # interactivos reales contra Bejerman (la "tormenta SFTP").
        self.assertEqual(probe.call_count, 1)

    def test_solicitud_concurrente_no_espera_al_probe_en_curso(self):
        """Un sondeo concurrente para el mismo documento no debe esperar a que
        termine el probe en curso: debe volver casi instantáneo por el camino
        de "otra solicitud ya está comprobando este comprobante", sin tocar SFTP."""

        reference = self._reference()
        probe_started = threading.Event()
        release_probe = threading.Event()

        def fake_probe(_client, _reference, *, interactive, allow_directory_scan=True):
            probe_started.set()
            self.assertTrue(release_probe.wait(timeout=5), "el test no liberó el probe a tiempo")
            return None, "missing"

        def first_request():
            close_old_connections()
            try:
                return self._enqueue(self._client(), reference)
            finally:
                close_old_connections()

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch("service.bejerman_pdf_requests.probe_comprobante_pdf_copy", side_effect=fake_probe) as probe,
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                first_future = executor.submit(first_request)
                self.assertTrue(probe_started.wait(timeout=5), "el primer thread nunca entró al probe")

                started_at = time.monotonic()
                second_pending = self._enqueue(self._client(), reference)
                elapsed_seconds = time.monotonic() - started_at

                release_probe.set()
                first_pending = first_future.result(timeout=5)

        self.assertEqual(probe.call_count, 1)
        self.assertLess(elapsed_seconds, 1.0, "el segundo sondeo esperó al probe en curso en vez de volver rápido")
        self.assertEqual(second_pending.pdf_state, "queued")
        self.assertFalse(second_pending.generation_requested)
        self.assertEqual(first_pending.pdf_state, "queued")
        self.assertEqual(self._count_rows(), 1)


COMPROBANTE_EMITIDO = {
    "Comprobante_Tipo": "RT",
    "Comprobante_Letra": "R",
    "Comprobante_PtoVenta": "00004",
    "Comprobante_Numero": "00004345",
    "Comprobante_FechaEmision": "2026-08-07",
    "Cliente_Codigo": "TMD",
    "Cliente_RazonSocial": "TERAPIAS MEDICAS DOMICILIARIAS S.A.",
    "Comprobante_Items": [
        {
            "Item_NumeroRenglon": 1,
            "Item_CodigoArticulo": "1204001",
            "Item_CantidadUM1": "2",
            "Item_DescripArticulo": "Jarra para aspirador",
            "Item_Partida": "20260007",
        }
    ],
}


@skipUnless(connection.vendor == "postgresql", "El coordinador de PDFs requiere PostgreSQL")
@override_settings(BEJERMAN_REMITO_PDF_ENABLED=True, BEJERMAN_REMITO_PDF_FALLBACK_ENABLED=False)
class RemitoPdfGeneradoPorNexoraTests(TransactionTestCase):
    """Bejerman da el número; el papel lo arma NEXORA.

    El servicio de PDF del ERP no responde desde el 20/07, así que esperar su
    archivo es un timeout garantizado en cada remito. Estos tests fijan que para los
    remitos no se le pida nada, y que para las facturas se le siga pidiendo.
    """

    reset_sequences = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("apply_bejerman_worker_heartbeat_schema", verbosity=0)
        call_command("apply_bejerman_pdf_requests_schema", verbosity=0)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_pdf_requests")

    @staticmethod
    def _sin_pm():
        """El PM sale por SSH a Bejerman; en tests no se sale a la red."""

        return patch("service.bejerman_delivery._remote_bejerman_sql_csv", return_value=[])

    def _guardar(self, comprobante=None, **kwargs):
        from service.bejerman_pdf_requests import store_rendered_remito_pdf

        with self._sin_pm():
            return store_rendered_remito_pdf(
                comprobante if comprobante is not None else COMPROBANTE_EMITIDO,
                company_key=kwargs.pop("company_key", "SEPID"),
                **kwargs,
            )

    def _fila(self, numero="00004345"):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT state, pdf_payload, tipo FROM bejerman_pdf_requests WHERE numero=%s",
                [numero],
            )
            return cur.fetchone()

    def test_al_emitir_queda_el_pdf_listo_para_servir(self):
        self.assertTrue(self._guardar())
        state, payload, tipo = self._fila()
        self.assertEqual(state, "ready_direct")
        self.assertEqual(tipo, "RT")
        self.assertTrue(bytes(payload).startswith(b"%PDF"))

    def test_el_request_devuelve_el_pdf_sin_pedirle_nada_a_bejerman(self):
        self._guardar()
        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.company_key = "SEPID"
        client.actor_user_id = None
        reference = BejermanPdfReference(
            type="RT", number="00004345", letter="R",
            point_of_sale="00004", issue_date="2026-08-07", customer_code="TMD",
        )
        with patch.object(BejermanSDKClient, "generar_comprobante_ventas_pdf") as generar:
            pdf, content_type = fetch_or_enqueue_comprobante_pdf(client, reference)
        generar.assert_not_called()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(content_type, "application/pdf")

    def test_las_facturas_se_le_siguen_pidiendo_a_bejerman(self):
        # Candado del guard por tipo: el renderer sólo sabe remitos y una factura
        # impresa con diseño de remito sería un comprobante fiscal mal emitido.
        factura = dict(COMPROBANTE_EMITIDO, Comprobante_Tipo="FC", Comprobante_Numero="00009001")
        self.assertFalse(self._guardar(factura))
        self.assertIsNone(self._fila("00009001"))

    def test_llamarlo_dos_veces_no_regenera(self):
        # Un lote de ingresos comparte remito y llama una vez por ingreso.
        self.assertTrue(self._guardar())
        with patch("service.bejerman_pdf_requests._render_remito_pdf_bytes") as render:
            self.assertTrue(self._guardar())
        render.assert_not_called()

    def test_si_el_render_falla_no_escribe_nada_y_no_levanta(self):
        # La emisión ya ocurrió en Bejerman: un problema de PDF no puede tumbarla.
        with patch(
            "service.bejerman_pdf_requests._render_remito_pdf_bytes",
            side_effect=RuntimeError("boom"),
        ):
            self.assertFalse(self._guardar())
        self.assertIsNone(self._fila())

    def test_un_comprobante_sin_numero_no_deja_basura(self):
        self.assertFalse(self._guardar(dict(COMPROBANTE_EMITIDO, Comprobante_Numero="")))

    @override_settings(BEJERMAN_REMITO_PDF_ENABLED=False, BEJERMAN_REMITO_PDF_FALLBACK_ENABLED=False)
    def test_apagado_no_hace_nada(self):
        self.assertFalse(self._guardar())
        self.assertIsNone(self._fila())

    def test_el_nombre_viejo_del_setting_sigue_valiendo(self):
        from service.bejerman_pdf_requests import remito_pdf_enabled

        with override_settings(BEJERMAN_REMITO_PDF_ENABLED=False, BEJERMAN_REMITO_PDF_FALLBACK_ENABLED=True):
            self.assertTrue(remito_pdf_enabled())
