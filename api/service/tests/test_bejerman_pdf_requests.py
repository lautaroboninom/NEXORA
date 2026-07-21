from __future__ import annotations

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
    document_key_for,
    fetch_or_enqueue_comprobante_pdf,
    process_bejerman_pdf_requests_once,
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
        call_command("apply_bejerman_pdf_requests_schema", verbosity=0)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_pdf_requests")

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
        probe.assert_called_once_with(client, reference, interactive=True)
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
            ),
        ):
            with self.assertRaises(BejermanPdfFolderUnavailableError) as ctx:
                fetch_or_enqueue_comprobante_pdf(client, reference)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_FOLDER_UNAVAILABLE")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertFalse(ctx.exception.generation_requested)
        self.assertEqual(self._count_rows(), 0)
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
            ),
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
            ),
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
            ),
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

    def test_deadline_devuelve_504_y_admite_recuperacion_tardia_desde_carpeta(self):
        client = self._client()
        reference = self._reference()
        document_key = document_key_for(client, reference)

        with (
            patch("service.bejerman_pdf_requests.comprobante_pdf_output_path", return_value=REMOTE_PATH),
            patch(
                "service.bejerman_pdf_requests.probe_comprobante_pdf_copy",
                side_effect=[(None, "missing"), (None, "missing"), (PDF_BYTES, "ready")],
            ),
        ):
            self._enqueue(client, reference)
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_pdf_requests
                       SET state='waiting_folder', generation_attempt_count=1,
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
            ),
        ):
            with ThreadPoolExecutor(max_workers=24) as executor:
                results = list(executor.map(request_pdf, range(24)))

        self.assertEqual(len(results), 24)
        self.assertTrue(all(state == "queued" for state, _requested in results))
        self.assertTrue(all(not requested for _state, requested in results))
        self.assertEqual(self._count_rows(), 1)
        row = self._row(document_key_for(self._client(), reference))
        self.assertEqual(row["generation_attempt_count"], 0)
