import base64
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from service.auth import issue_token
from service import bejerman_catalog_cache
from service import bejerman_delivery
from service import delivery_orders
from service.bejerman_companies import list_ingress_companies, require_company
from service.bejerman_documents import (
    REGISTERED_REMITO_NO_PDF_CODE,
    RIS_REGISTERED_NO_PDF_CODE,
    cobranzas_remito_pdf_metadata,
    is_transient_pdf_lookup_message,
    parse_bejerman_remito_number,
    registered_remito_no_pdf_detail,
    retry_after_header_value,
)
from service.bejerman_delivery import (
    BillingError,
    _assert_partidas_ready,
    _assert_remito_payload_print_limits,
    _bejerman_sql_database_for_company,
    _bridge_order,
    _collapse_recovery_candidate_rows,
    _delivery_article_descriptions,
    _delivery_article_sales_vat_map,
    _diagnose_bejerman_error,
    _ensure_blocking_stock_sync_jobs_resolved,
    _find_blocking_stock_sync_jobs,
    _mark_group_failed,
    _mark_group_review_required,
    _mark_group_sent_unknown,
    _order_print_line_count,
    _partition_remito_orders_for_printing,
    _profile_for_order,
    _recovery_record_group_stamps,
    _recovery_record_matches_delivery_request,
    _recovery_stamp_belongs_to_group,
    _recover_delivery_partida_rejection,
    _sdk_error,
    _remito_requires_billing,
    _remito_print_item_chunks,
    _remito_print_line_limit,
    _split_remito_orders_for_printing,
    _validate_remito_orders,
    check_delivery_remito_stock_sync,
    get_delivery_order_invoice_pdf,
    get_delivery_order_remito_pdf,
    get_facturacion_pdf,
    get_remito_group_pdf,
    generate_bejerman_remito,
    list_bejerman_article_stock,
    list_bejerman_articles,
    list_bejerman_depositos,
    list_facturacion_from_bejerman,
    list_remitos_from_bejerman,
    resolve_bejerman_remito_review,
    resolve_delivery_order_invoice_document,
    resolve_delivery_order_remito_document,
)
from service.bejerman_remito_price_fix import (
    RemitoPriceLineTarget,
    build_bejerman_price_fix_sql,
    perform_bejerman_remito_price_fix,
)
from service.bejerman_pdf_settings import (
    BejermanPdfOutputSettingsError,
    resolve_pdf_output_path,
    validate_pdf_output_dir,
)
from service.bejerman_pdf_requests import BejermanPdfRequestPending, BejermanPdfWorkerStalledError
from service.delivery_orders import (
    DeliveryOrderError,
    _service_release_equipment_detail,
    get_delivery_order,
    insert_item_partidas,
    list_delivery_orders,
    remito_profile_for_type,
    serialize_order,
)
from service.bejerman_particular_customers import has_other_open_ingresos
from service.models import User
from service.views.delivery_orders_views import (
    CobranzasRemitoPdfView,
    CobranzasRemitoPrintView,
    DeliveryOrderBejermanArticleStockView,
    _remito_print_wait_page,
)
from service.bejerman_ris import _apply_registered_document, _build_payload
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanPdfPendingError,
    BejermanPdfReference,
    BejermanSdkResponseError,
    BejermanSdkTransportUnavailable,
    BejermanSdkUnavailable,
    build_article_description_map,
    build_article_filters,
    build_article_stock_lots,
    build_delivery_remito_comprobante,
    build_document_id,
    build_facturacion_result,
    build_remitos_result,
    build_partida_rows,
    build_sales_filters,
    build_service_ingress_comprobante,
    build_stock_rows,
    decode_document_id,
    _comprobante_pdf_filename_candidates,
    delivery_remito_config,
    extract_pdf_bytes,
    fetch_comprobante_pdf,
    is_ok_response,
    sdk_emission_article_quantity,
    sdk_signed_article_quantity,
    sdk_uses_negative_article_quantity,
    _validate_delivery_comprobante_amounts,
)


class BejermanDocumentHelpersTests(SimpleTestCase):
    def test_registered_no_pdf_message_is_shared_across_remito_flows(self):
        detail = registered_remito_no_pdf_detail("RSS R 00004-00004715")

        self.assertEqual(REGISTERED_REMITO_NO_PDF_CODE, "BEJERMAN_REMITO_REGISTERED_NO_PDF")
        self.assertEqual(RIS_REGISTERED_NO_PDF_CODE, "RIS_REGISTERED_NO_PDF")
        self.assertIn("RSS R 00004-00004715", detail)
        self.assertIn("registrado manualmente", detail)
        self.assertIn("no generan PDF de Bejerman", detail)

    def test_parse_remito_number_supports_delivery_and_ris_shapes(self):
        self.assertEqual(
            parse_bejerman_remito_number(
                "00004-4715",
                default_type="RSS",
                default_letter="R",
                allowed_types=("RSS", "RT"),
            ),
            {
                "type": "RSS",
                "letter": "R",
                "point": "00004",
                "number": "00004715",
                "remitoNumber": "RSS R 00004-00004715",
            },
        )
        self.assertEqual(
            parse_bejerman_remito_number(
                "RT R 4-42",
                default_type="RSS",
                default_letter="R",
                allowed_types=("RSS", "RT"),
            )["remitoNumber"],
            "RT R 00004-00000042",
        )
        with self.assertRaises(ValueError):
            parse_bejerman_remito_number("FC A 4-42", default_type="RSS", allowed_types=("RSS", "RT"))

    def test_parse_remito_number_can_enforce_ris_profile(self):
        with self.assertRaisesRegex(ValueError, "RDA R"):
            parse_bejerman_remito_number(
                "RIS R 00004-00004715",
                default_type="RDA",
                default_letter="R",
                require_explicit_match=True,
                explicit_mismatch_message="El remito debe corresponder a RDA R para este motivo de ingreso",
            )
        self.assertEqual(
            parse_bejerman_remito_number(
                "4715",
                default_type="RDA",
                default_letter="R",
                allow_number_only=True,
                default_point="00001",
            )["remitoNumber"],
            "RDA R 00001-00004715",
        )

    def test_pdf_retry_helpers_are_shared(self):
        self.assertEqual(retry_after_header_value(1800), "2")
        self.assertTrue(is_transient_pdf_lookup_message("No se encontró el comprobante asociado"))
        self.assertEqual(
            cobranzas_remito_pdf_metadata("abc/123", customer_code="CLI", company_key="SEPID"),
            {
                "pdfUrl": "/api/cobranzas/remitos/abc%2F123/pdf/?customerCode=CLI&companyKey=SEPID",
                "printUrl": "/api/cobranzas/remitos/abc%2F123/print/?customerCode=CLI&companyKey=SEPID",
            },
        )

    def test_bejerman_remito_price_fix_sql_respalda_actualiza_y_recalcula_totales(self):
        target = RemitoPriceLineTarget(
            company_key="SEPID",
            group_id="brg-test",
            remito_number="RT R 00004-00012345",
            tipo="RT",
            letra="R",
            punto="00004",
            numero="00012345",
            customer_code="CLI",
            customer_name="Cliente",
            order_number="OE-TEST",
            item_id="doi-test",
            article_code="ART001",
            partida="P1",
            quantity=Decimal("2"),
            unit_price=Decimal("1500.00"),
            discount_percent=Decimal("10"),
            line_net=Decimal("2700.00"),
        )

        sql = build_bejerman_price_fix_sql([target], database="SBDASEP", apply=True, backup_name="BKP_TEST")

        self.assertIn("USE [SBDASEP];", sql)
        self.assertIn("@BackupPrefix sysname = N'BKP_TEST'", sql)
        self.assertIn("_SegDetV", sql)
        self.assertIn("_SegTotV", sql)
        self.assertIn("UPDATE d", sql)
        self.assertIn("sdv_PrecioUn = lm.unit_price", sql)
        self.assertIn("sdv_ImpTot = lm.line_net", sql)
        self.assertIn("DELETE tv", sql)
        self.assertIn("INSERT INTO SegTotV", sql)
        self.assertIn("line_matches <> 1", sql)
        self.assertIn("mismo renglón Bejerman", sql)

    def test_bejerman_remito_price_fix_perform_usa_executor_y_devuelve_backup(self):
        target = RemitoPriceLineTarget(
            company_key="SEPID",
            group_id="brg-test",
            remito_number="RT R 00004-00012345",
            tipo="RT",
            letra="R",
            punto="00004",
            numero="00012345",
            customer_code="CLI",
            customer_name="Cliente",
            order_number="OE-TEST",
            item_id="doi-test",
            article_code="ART001",
            partida="P1",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            discount_percent=Decimal("0"),
            line_net=Decimal("100.00"),
        )
        calls = []

        def executor(sql, **kwargs):
            calls.append((sql, kwargs))
            return [{"estado": "DRY_RUN", "backup_name": "BKP_TEST"}]

        result = perform_bejerman_remito_price_fix(
            [target],
            company_key="SEPID",
            database="SBDASEP",
            backup_name="BKP_TEST",
            apply=False,
            executor=executor,
        )

        self.assertEqual(result["backupName"], "BKP_TEST")
        self.assertEqual(result["lineCount"], 1)
        self.assertEqual(result["rows"], [{"estado": "DRY_RUN", "backup_name": "BKP_TEST"}])
        self.assertEqual(calls[0][1], {"company_key": "SEPID"})
        self.assertIn("DECLARE @Apply bit = 0", calls[0][0])

    @override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=22, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=3)
    def test_rss_print_split_usa_tres_lineas_por_equipo_tmd(self):
        order = {
            "deliveryType": "service_release",
            "items": [
                {
                    "id": f"item-{index}",
                    "quantity": 1,
                    "partidas": [{"partida": f"NS-{index:03d}", "quantity": 1}],
                }
                for index in range(46)
            ],
        }

        chunks = _remito_print_item_chunks(order)

        self.assertEqual([len(chunk) for chunk in chunks], [7, 7, 7, 7, 7, 7, 4])

    @override_settings(BEJERMAN_RSS_MAX_PRINT_LINES=22, BEJERMAN_RSS_PRINT_LINES_PER_ITEM=3)
    def test_rss_print_split_cuarenta_y_ocho_equipos_en_siete_rss(self):
        order = {
            "deliveryType": "service_release",
            "items": [
                {
                    "id": f"item-{index}",
                    "quantity": 1,
                    "partidas": [{"partida": f"NS-{index:03d}", "quantity": 1}],
                }
                for index in range(48)
            ],
        }

        chunks = _remito_print_item_chunks(order)

        self.assertEqual(len(chunks), 7)
        self.assertEqual([len(chunk) for chunk in chunks], [7, 7, 7, 7, 7, 7, 6])
        self.assertTrue(all(len(chunk) <= 7 for chunk in chunks))

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_venta_print_split_admite_veintidos_articulos_de_un_renglon(self):
        for item_count, expected_chunk_sizes in (
            (22, [22]),
            (23, [22, 1]),
            (45, [22, 22, 1]),
        ):
            with self.subTest(item_count=item_count):
                order = {
                    "deliveryType": "sale",
                    "items": [
                        {
                            "id": f"item-{index}",
                            "quantity": 1,
                            "partidas": [{"partida": f"P-{index:03d}", "quantity": 1}],
                        }
                        for index in range(item_count)
                    ],
                }

                chunks = _remito_print_item_chunks(order)

                self.assertEqual([len(chunk) for chunk in chunks], expected_chunk_sizes)
                self.assertEqual(
                    [sum(len(item["partidas"]) for item in chunk) for chunk in chunks],
                    expected_chunk_sizes,
                )

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_venta_print_split_fragmenta_un_item_con_trece_partidas(self):
        item = {
            "id": "item-partidas",
            "articleCode": "ART-LOTE",
            "quantity": 13,
            "unitPrice": "123.45",
            "discountPercent": "7.5",
            "priceCurrency": "USD",
            "partidas": [
                {
                    "partida": f"LOTE-{index:03d}",
                    "assignedQuantity": 1,
                    "stockDepositCode": "VAL",
                }
                for index in range(13)
            ],
        }
        order = {
            "deliveryType": "sale",
            "items": [item],
        }

        chunks = _remito_print_item_chunks(order)

        fragments = [fragment for chunk in chunks for fragment in chunk]
        self.assertEqual(
            [sum(len(item["partidas"]) for item in chunk) for chunk in chunks],
            [11, 2],
        )
        self.assertEqual([fragment["quantity"] for fragment in fragments], [11, 2])
        self.assertEqual(sum(fragment["quantity"] for fragment in fragments), 13)
        self.assertEqual(
            [partida["partida"] for fragment in fragments for partida in fragment["partidas"]],
            [f"LOTE-{index:03d}" for index in range(13)],
        )
        for fragment in fragments:
            self.assertEqual(fragment["articleCode"], "ART-LOTE")
            self.assertEqual(fragment["unitPrice"], "123.45")
            self.assertEqual(fragment["discountPercent"], "7.5")
            self.assertEqual(fragment["priceCurrency"], "USD")
            self.assertTrue(all(partida["stockDepositCode"] == "VAL" for partida in fragment["partidas"]))
        self.assertEqual(item["quantity"], 13)
        self.assertEqual(len(item["partidas"]), 13)

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_venta_print_split_fragmenta_veintiseis_partidas_sin_perder_cantidades(self):
        order = {
            "deliveryType": "sale",
            "items": [
                {
                    "id": "item-26-partidas",
                    "quantity": 26,
                    "partidas": [
                        {"partida": f"P-{index:03d}", "quantity": 1}
                        for index in range(26)
                    ],
                }
            ],
        }

        chunks = _remito_print_item_chunks(order)

        self.assertEqual(
            [sum(len(item["partidas"]) for item in chunk) for chunk in chunks],
            [22, 4],
        )
        self.assertEqual(
            sum(item["quantity"] for chunk in chunks for item in chunk),
            26,
        )

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_venta_print_split_cuenta_dos_renglones_por_articulo_con_descuento(self):
        for item_count, expected_chunk_sizes, expected_line_count in (
            (11, [11], 22),
            (12, [11, 1], 24),
        ):
            with self.subTest(item_count=item_count):
                order = {
                    "deliveryType": "sale",
                    "items": [
                        {
                            "id": f"item-{index}",
                            "quantity": 1,
                            "discountPercent": 10,
                            "partidas": [{"partida": f"P-{index:03d}", "quantity": 1}],
                        }
                        for index in range(item_count)
                    ],
                }

                chunks = _remito_print_item_chunks(order)

                self.assertEqual([len(chunk) for chunk in chunks], expected_chunk_sizes)
                # El RT no consume un renglón con la referencia de orden: queda
                # en el mensaje. Cada descuento agrega una segunda línea real.
                self.assertEqual(_order_print_line_count(order), expected_line_count)

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_venta_print_split_agrupa_ordenes_por_renglones_reales(self):
        def build_order(order_index, item_count, discount_percent=0):
            return {
                "id": f"order-{order_index}",
                "deliveryType": "sale",
                "items": [
                    {
                        "id": f"item-{order_index}-{item_index}",
                        "quantity": 1,
                        "discountPercent": discount_percent,
                        "partidas": [{"partida": f"P-{order_index}-{item_index}", "quantity": 1}],
                    }
                    for item_index in range(item_count)
                ],
            }

        orders_11_plus_11 = [build_order(1, 11), build_order(2, 11)]
        orders_12_plus_12 = [build_order(1, 12), build_order(2, 12)]
        orders_5_plus_5_with_discount = [build_order(1, 5, 10), build_order(2, 5, 10)]

        self.assertEqual(
            [[order["id"] for order in batch] for batch in _partition_remito_orders_for_printing(orders_11_plus_11)],
            [["order-1", "order-2"]],
        )
        self.assertEqual(
            [[order["id"] for order in batch] for batch in _partition_remito_orders_for_printing(orders_12_plus_12)],
            [["order-1"], ["order-2"]],
        )
        # Dos órdenes de 5 artículos con descuento ocupan 10 renglones cada una
        # y entran en el formulario sin leyendas de orden intermedias.
        self.assertEqual(
            [
                [order["id"] for order in batch]
                for batch in _partition_remito_orders_for_printing(orders_5_plus_5_with_discount)
            ],
            [["order-1", "order-2"]],
        )

    @override_settings(BEJERMAN_SALE_MAX_PRINT_LINES=22)
    def test_reintento_por_partida_bloquea_rt_con_veintitres_articulos_sin_llamar_al_sdk(self):
        client = Mock()
        comprobante = {
            "Comprobante_Items": [
                {
                    "Item_Tipo": "A",
                    "Item_CodigoArticulo": f"ART-{index:03d}",
                    "Item_Partida": f"P-{index:03d}",
                    "Item_Deposito": "VAL",
                    "Item_CantidadUM1": -1,
                    "Item_CantidadUM2": -1,
                }
                for index in range(23)
            ],
        }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _recover_delivery_partida_rejection(
                client,
                comprobante,
                delivery_type="sale",
                numera_flex="N",
                emite_reg="E",
                circuito="VENTAS",
                operacion="RT",
            )

        self.assertEqual(ctx.exception.code, "BEJERMAN_RT_PRINT_LIMIT_EXCEEDED")
        client.obtener_stock_partida.assert_not_called()
        client.ingresar_lista_comprobantes_stock_json.assert_not_called()
        client.ingresar_comprobante_ventas_json.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def _comprobante_con_partida(self):
        return {
            "Comprobante_Items": [
                {
                    "Item_Tipo": "A",
                    "Item_CodigoArticulo": "1115007",
                    "Item_Partida": "5167684",
                    "Item_Deposito": "STR",
                    "Item_CantidadUM1": -1,
                    "Item_CantidadUM2": -1,
                }
            ],
        }

    @override_settings(BEJERMAN_RECOVERY_KEEPS_STOCK_MOVEMENT=True)
    def test_reintento_por_partida_deja_la_partida_en_cero_y_manda_el_remito_con_stock(self):
        # El ENT sólo tiene que hacer existir la partida. Si la unidad que suma se
        # compensa, el remito puede hacer su propio movimiento y queda atado al
        # cliente: es lo que lo vuelve visible en los reportes de alquiler.
        client = Mock()
        client.obtener_stock_partida.return_value = {"DatosJSON": []}
        client.ingresar_lista_comprobantes_stock_json.return_value = {"Resultado": "OK"}
        client.ingresar_comprobante_ventas_json.return_value = {"Resultado": "OK"}
        comprobante = self._comprobante_con_partida()

        with (
            patch(
                "service.bejerman_delivery._delivery_stock_partida_present",
                side_effect=[False, True, True],
            ),
            patch("service.bejerman_delivery.observacion_partida_de_serie", return_value="MG 2245") as observacion,
        ):
            enviado, _respuesta = _recover_delivery_partida_rejection(
                client,
                comprobante,
                delivery_type="rental",
                numera_flex="N",
                emite_reg="E",
                circuito="VENTAS",
                operacion="RTA",
            )

        movimientos = [
            (entry["Comprobante_Tipo"], entry["Comprobante_CantidadUM1"])
            for call in client.ingresar_lista_comprobantes_stock_json.call_args_list
            for entry in call.args[0]
        ]
        # La SAL tiene que ir en NEGATIVO: el importador guarda la cantidad tal como
        # se la mandan y no la deduce del tipo. Con 1 en vez de -1 la partida queda
        # en 3 en lugar de 1 (visto en el simulacro del 13/08/2026).
        self.assertEqual(movimientos, [("ENT", 1), ("SAL", -1)])
        # La partida nueva lleva en la observación el MG de esa serie, no "NEXORA <número>".
        observacion.assert_called_once_with("5167684")
        alta = client.ingresar_lista_comprobantes_stock_json.call_args_list[0].args[0]
        self.assertEqual([entry["Partida_Observaciones"] for entry in alta], ["MG 2245"])
        # El comprobante sale tal cual: con su signo y sin ActualizaStock=N.
        self.assertIs(enviado, comprobante)
        self.assertNotIn("Comprobante_ActualizaStock", enviado)
        self.assertEqual(enviado["Comprobante_Items"][0]["Item_CantidadUM1"], -1)

    @override_settings(BEJERMAN_RECOVERY_KEEPS_STOCK_MOVEMENT=True)
    def test_si_el_remito_con_stock_falla_repone_el_ent_y_usa_el_camino_viejo(self):
        # Nunca hay que quedarse sin la unidad: si el reintento con movimiento no
        # sale, se repone el ENT y se emite como antes, con ActualizaStock=N.
        client = Mock()
        client.ingresar_lista_comprobantes_stock_json.return_value = {"Resultado": "OK"}
        client.ingresar_comprobante_ventas_json.side_effect = [
            BejermanSdkResponseError("Partida inexistente y/o no informada"),
            {"Resultado": "OK"},
        ]
        comprobante = self._comprobante_con_partida()

        with (
            patch(
                "service.bejerman_delivery._delivery_stock_partida_present",
                side_effect=[False, True, True],
            ),
            patch("service.bejerman_delivery._delivery_entries_moved_by_comprobante", return_value=False),
            patch("service.bejerman_delivery.observacion_partida_de_serie", return_value=""),
        ):
            enviado, _respuesta = _recover_delivery_partida_rejection(
                client,
                comprobante,
                delivery_type="rental",
                numera_flex="N",
                emite_reg="E",
                circuito="VENTAS",
                operacion="RTA",
            )

        movimientos = [
            (entry["Comprobante_Tipo"], entry["Comprobante_CantidadUM1"])
            for call in client.ingresar_lista_comprobantes_stock_json.call_args_list
            for entry in call.args[0]
        ]
        # La reposicion vuelve a sumar lo que saco la compensacion.
        self.assertEqual(movimientos, [("ENT", 1), ("SAL", -1), ("ENT", 1)])
        self.assertEqual(enviado["Comprobante_ActualizaStock"], "N")
        self.assertEqual(enviado["Comprobante_Items"][0]["Item_CantidadUM1"], 1)

    @override_settings(BEJERMAN_RECOVERY_KEEPS_STOCK_MOVEMENT=True)
    def test_remito_que_entra_con_error_no_repone_el_ent(self):
        client = Mock()
        client.ingresar_lista_comprobantes_stock_json.return_value = {"Resultado": "OK"}
        client.ingresar_comprobante_ventas_json.side_effect = [RuntimeError("timeout"), {"Resultado": "OK"}]

        with (
            patch("service.bejerman_delivery._delivery_stock_partida_present", side_effect=[False, True, True]),
            patch("service.bejerman_delivery._delivery_entries_moved_by_comprobante", return_value=True),
            patch("service.bejerman_delivery.observacion_partida_de_serie", return_value=""),
        ):
            with self.assertRaises(BejermanSdkUnavailable):
                _recover_delivery_partida_rejection(
                    client,
                    self._comprobante_con_partida(),
                    delivery_type="rental",
                    numera_flex="N",
                    emite_reg="E",
                    circuito="VENTAS",
                    operacion="RTA",
                )

        movimientos = [
            entry["Comprobante_Tipo"]
            for call in client.ingresar_lista_comprobantes_stock_json.call_args_list
            for entry in call.args[0]
        ]
        self.assertEqual(movimientos, ["ENT", "SAL"])
        self.assertEqual(client.ingresar_comprobante_ventas_json.call_count, 1)

    @override_settings(BEJERMAN_RECOVERY_KEEPS_STOCK_MOVEMENT=False)
    def test_con_la_compensacion_apagada_se_comporta_como_antes(self):
        client = Mock()
        client.ingresar_lista_comprobantes_stock_json.return_value = {"Resultado": "OK"}
        client.ingresar_comprobante_ventas_json.return_value = {"Resultado": "OK"}

        with (
            patch(
                "service.bejerman_delivery._delivery_stock_partida_present",
                side_effect=[False, True],
            ),
            patch("service.bejerman_delivery.observacion_partida_de_serie", return_value=""),
        ):
            enviado, _respuesta = _recover_delivery_partida_rejection(
                client,
                self._comprobante_con_partida(),
                delivery_type="rental",
                numera_flex="N",
                emite_reg="E",
                circuito="VENTAS",
                operacion="RTA",
            )

        tipos = [
            entry["Comprobante_Tipo"]
            for call in client.ingresar_lista_comprobantes_stock_json.call_args_list
            for entry in call.args[0]
        ]
        self.assertEqual(tipos, ["ENT"])
        self.assertEqual(enviado["Comprobante_ActualizaStock"], "N")

    @override_settings(
        BEJERMAN_SALE_MAX_PRINT_LINES=22,
        BEJERMAN_REMITO_MAX_PRINT_LINES=21,
    )
    def test_rta_y_rtn_print_split_conservan_el_limite_general(self):
        # Alquileres (RTA) llevan 2 renglones de encabezado por orden (orden y leyenda general
        # "Se entrega en alquiler"). Cada equipo ocupa un solo renglón (no repite la leyenda por equipo).
        # En una orden de 25 equipos entran 19 en el primer remito y 6 en el segundo.
        # Demos (RTN) llevan 1 renglón de encabezado por orden (- Orden -): entran 20 en el primero y 5 en el segundo.
        for delivery_type, expected_chunk_sizes in (("rental", [19, 6]), ("demo", [20, 5])):
            with self.subTest(delivery_type=delivery_type):
                order = {
                    "deliveryType": delivery_type,
                    "items": [
                        {
                            "id": f"item-{index}",
                            "quantity": 1,
                            "partidas": [{"partida": f"NS-{index:03d}", "quantity": 1}],
                        }
                        for index in range(25)
                    ],
                }

                chunks = _remito_print_item_chunks(order)

                self.assertEqual([len(chunk) for chunk in chunks], expected_chunk_sizes)

    @override_settings(BEJERMAN_REMITO_MAX_PRINT_LINES=21)
    def test_rta_y_rtn_reparten_un_renglon_con_varios_ns(self):
        # La pantalla de alquiler carga todos los NS de un artículo en el mismo renglón.
        # El chunker lo reparte respetando las capacidades físicas del remito.
        for delivery_type, expected_ns in (("rental", [19, 6]), ("demo", [20, 5])):
            with self.subTest(delivery_type=delivery_type):
                item = {
                    "id": "item-ns",
                    "articleCode": "1115003",
                    "quantity": 25,
                    "partidas": [
                        {"id": f"doip-{index}", "partida": f"NS-{index:03d}", "assignedQuantity": 1}
                        for index in range(25)
                    ],
                }

                chunks = _remito_print_item_chunks({"deliveryType": delivery_type, "items": [item]})

                self.assertEqual([len(chunk) for chunk in chunks], [1] * len(expected_ns))
                self.assertEqual([len(chunk[0]["partidas"]) for chunk in chunks], expected_ns)
                self.assertEqual([chunk[0]["quantity"] for chunk in chunks], expected_ns)
                self.assertEqual(
                    [partida["partida"] for chunk in chunks for partida in chunk[0]["partidas"]],
                    [f"NS-{index:03d}" for index in range(25)],
                )
                # Cada fragmento conserva el id del renglón: con él se asigna cada
                # partida al remito que la lleva.
                self.assertTrue(all(chunk[0]["id"] == "item-ns" for chunk in chunks))
                self.assertEqual(len(item["partidas"]), 25)

    @override_settings(BEJERMAN_REMITO_MAX_PRINT_LINES=21)
    def test_rta_split_repite_leyenda_de_accesorios_y_no_la_deja_huerfana(self):
        # Cuando un grupo de artículos con accesorios se divide entre remitos:
        # Remito 1: Encabezado (2) + Leyenda accesorios (1) + 18 equipos = 21 líneas.
        # Remito 2: Encabezado (2) + Leyenda accesorios repetida (1) + 2 equipos restantes = 5 líneas.
        order = {
            "orderNumber": "OE-00999",
            "deliveryType": "rental",
            "remitoGeneralLegend": "Se entrega en alquiler",
            "items": [
                {
                    "id": "humidificadores",
                    "articleCode": "HUM01",
                    "description": "Humidificador térmico",
                    "remitoArticleLegend": "Vaso",
                    "quantity": 20,
                    "partidas": [
                        {"id": f"part-{i}", "partida": f"HUM-NS-{i:02d}", "assignedQuantity": 1}
                        for i in range(20)
                    ],
                }
            ],
        }
        chunks = _remito_print_item_chunks(order)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0][0]["partidas"]), 18)
        self.assertEqual(len(chunks[1][0]["partidas"]), 2)

        # Si además se agrega otro grupo de equipos al límite:
        # no debe quedar la leyenda de accesorios huérfana al final del remito 1 sin su equipo.
        order_two_groups = {
            "orderNumber": "OE-00999",
            "deliveryType": "rental",
            "remitoGeneralLegend": "Se entrega en alquiler",
            "items": [
                {
                    "id": "humidificadores",
                    "articleCode": "HUM01",
                    "description": "Humidificador térmico",
                    "remitoArticleLegend": "Vaso",
                    "quantity": 18,
                    "partidas": [
                        {"id": f"part-{i}", "partida": f"HUM-NS-{i:02d}", "assignedQuantity": 1}
                        for i in range(18)
                    ],
                },
                {
                    "id": "cpap",
                    "articleCode": "CPAP01",
                    "description": "Equipo CPAP",
                    "remitoArticleLegend": "Máscara",
                    "quantity": 2,
                    "partidas": [
                        {"id": f"part-c-{i}", "partida": f"CPAP-NS-{i:02d}", "assignedQuantity": 1}
                        for i in range(2)
                    ],
                },
            ],
        }
        # En remito 1 entran: 2 líneas encabezado + 1 leyenda HUM + 18 HUM = 21 líneas.
        # CPAP no entra en remito 1 porque su leyenda (1) + equipo (1) = 2 líneas requeridas superan el límite (21).
        # Por ende, la leyenda de CPAP NO queda huérfana al pie de remito 1: se traslada a remito 2 junto al equipo.
        chunks2 = _remito_print_item_chunks(order_two_groups)
        self.assertEqual(len(chunks2), 2)
        # Remito 1 contiene solo humidificadores
        self.assertEqual(len(chunks2[0]), 1)
        self.assertEqual(chunks2[0][0]["articleCode"], "HUM01")
        self.assertEqual(len(chunks2[0][0]["partidas"]), 18)
        # Remito 2 contiene los CPAP
        self.assertEqual(len(chunks2[1]), 1)
        self.assertEqual(chunks2[1][0]["articleCode"], "CPAP01")
        self.assertEqual(len(chunks2[1][0]["partidas"]), 2)

    @override_settings(
        BEJERMAN_SALE_MAX_PRINT_LINES=22,
        BEJERMAN_REMITO_MAX_PRINT_LINES=21,
        BEJERMAN_RSS_MAX_PRINT_LINES=22,
        BEJERMAN_RSS_PRINT_LINES_PER_ITEM=3,
    )
    def test_cada_remito_partido_entra_en_su_formulario(self):
        # La cuenta del split y el comprobante viven en lugares distintos y nada los ata:
        # el 08/09/2026 una leyenda nueva del RTA duplicó sus renglones sin que fallara
        # ningún test del chunker. Este arma el comprobante real de cada remito.
        profiles = {
            "sale": {"type": "RT", "operation": "MC", "deposit": "VAL"},
            "rental": {"type": "RTA", "operation": "ALQ", "deposit": "STL"},
            "demo": {"type": "RTN", "operation": "DEMO", "deposit": "VAL"},
            "service_release": {"type": "RSS", "operation": "REP", "deposit": "STC"},
        }
        partida_shapes = (
            ("un renglón con 40 NS", [[40]], 0),
            ("25 renglones de un NS", [[1] * 25], 0),
            ("renglones mezclados con descuento", [[7, 1, 12], [3]], 10),
            ("tres órdenes de 8 NS", [[8], [8], [8]], 0),
        )
        # RSS lleva un equipo por renglón y no reparte renglones: sólo se prueba así.
        service_release_shapes = (
            ("25 equipos", [[1] * 25], 0),
            ("tres órdenes de 5 equipos", [[1] * 5] * 3, 0),
        )

        def build_order(delivery_type, order_index, ns_per_item, discount_percent):
            deposit = profiles[delivery_type]["deposit"]
            return {
                "id": f"do-{delivery_type}-{order_index}",
                "orderNumber": f"OE-{order_index:05d}",
                "deliveryType": delivery_type,
                "items": [
                    {
                        "id": f"doi-{order_index}-{item_index}",
                        "articleCode": f"11150{item_index:02d}",
                        "articleName": "Concentrador de oxígeno",
                        "articleRequiresPartida": True,
                        "quantity": ns_count,
                        "unitPrice": 100 if discount_percent else 0,
                        "discountPercent": discount_percent,
                        "stockDepositCode": deposit,
                        "partidas": [
                            {
                                "id": f"doip-{order_index}-{item_index}-{ns_index}",
                                "partida": f"NS-{order_index}-{item_index}-{ns_index:03d}",
                                "assignedQuantity": 1,
                                "stockDepositCode": deposit,
                            }
                            for ns_index in range(ns_count)
                        ],
                    }
                    for item_index, ns_count in enumerate(ns_per_item)
                ],
            }

        for delivery_type, profile in profiles.items():
            shapes = service_release_shapes if delivery_type == "service_release" else partida_shapes
            config = delivery_remito_config(profile)
            limit = _remito_print_line_limit(delivery_type)
            for label, orders_shape, discount_percent in shapes:
                orders = [
                    build_order(delivery_type, order_index, ns_per_item, discount_percent)
                    for order_index, ns_per_item in enumerate(orders_shape, start=1)
                ]
                batches = _partition_remito_orders_for_printing(_split_remito_orders_for_printing(orders, None))
                emitted_ns = []
                for batch_index, batch in enumerate(batches, start=1):
                    comprobante = build_delivery_remito_comprobante(
                        {
                            "groupId": "brg-contrato-renglones",
                            "issueDate": "2026-09-10",
                            "customerCode": "ACU",
                            "customerName": "ACUMAR",
                            "sellerCode": "ADM",
                            "paymentTermCode": "30",
                            "orders": [_bridge_order(order) for order in batch],
                        },
                        config,
                    )["comprobante"]
                    items = comprobante["Comprobante_Items"]
                    counted = sum(_order_print_line_count(order) for order in batch)
                    with self.subTest(delivery_type=delivery_type, shape=label, remito=batch_index):
                        self.assertLessEqual(len(items), limit)
                        if delivery_type == "service_release":
                            # RSS reserva a propósito un renglón de margen por equipo.
                            self.assertLessEqual(len(items), counted)
                        else:
                            self.assertEqual(len(items), counted)
                        _assert_remito_payload_print_limits(delivery_type, comprobante)
                    emitted_ns.extend(item["Item_Partida"] for item in items if item["Item_Tipo"] == "A")
                with self.subTest(delivery_type=delivery_type, shape=label):
                    self.assertEqual(
                        emitted_ns,
                        [partida["partida"] for order in orders for item in order["items"] for partida in item["partidas"]],
                    )

    @override_settings(
        BEJERMAN_SALE_MAX_PRINT_LINES=22,
        BEJERMAN_REMITO_MAX_PRINT_LINES=21,
        BEJERMAN_RSS_MAX_PRINT_LINES=22,
    )
    def test_ningun_remito_sale_con_mas_renglones_que_su_formulario(self):
        def comprobante(tipo, renglones):
            return {
                "Comprobante_Tipo": tipo,
                "Comprobante_Items": [
                    {"Item_Tipo": "A" if index % 2 == 0 else "L", "Item_NumeroRenglon": index}
                    for index in range(1, renglones + 1)
                ],
            }

        for delivery_type, tipo, limit in (("rental", "RTA", 21), ("demo", "RTN", 21), ("service_release", "RSS", 22)):
            with self.subTest(delivery_type=delivery_type):
                _assert_remito_payload_print_limits(delivery_type, comprobante(tipo, limit))
                with self.assertRaises(DeliveryOrderError) as ctx:
                    _assert_remito_payload_print_limits(delivery_type, comprobante(tipo, limit + 1))
                self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_PRINT_LIMIT_EXCEEDED")
        # La venta conserva su código de siempre.
        with self.assertRaises(DeliveryOrderError) as ctx:
            _assert_remito_payload_print_limits("sale", comprobante("RT", 23))
        self.assertEqual(ctx.exception.code, "BEJERMAN_RT_PRINT_LIMIT_EXCEEDED")


class FakeSDKClient:
    def __init__(self, customer_records=None, sales_records=None, fail_sales_types=None):
        self.calls = []
        self.customer_records = customer_records or [
            {
                "Cliente_Codigo": "ACU",
                "Cliente_RazonSocial": "ACUMAR",
                "Cliente_NroDocumento": "30711111111",
            }
        ]
        self.sales_records = sales_records
        self.fail_sales_types = {str(item).upper() for item in (fail_sales_types or [])}

    def list_clientes(self):
        self.calls.append(("list_clientes", []))
        return {"DatosJSON": self.customer_records}

    def list_comprobantes_ventas(self, filters):
        self.calls.append(("list_comprobantes_ventas", filters))
        values = {item.get("Campo"): item.get("Valor") for item in filters}
        if str(values.get("Comprobante_Tipo") or "").upper() in self.fail_sales_types:
            raise BejermanSdkResponseError("Error en la ejecución.")
        if self.sales_records is not None:
            records = []
            for record in self.sales_records:
                if values.get("Comprobante_Tipo") and record.get("Comprobante_Tipo") != values.get("Comprobante_Tipo"):
                    continue
                if values.get("Cliente_Codigo") and record.get("Cliente_Codigo") != values.get("Cliente_Codigo"):
                    continue
                if values.get("Comprobante_TipoOperacion") and record.get("Comprobante_TipoOperacion") != values.get("Comprobante_TipoOperacion"):
                    continue
                records.append(record)
            return {"DatosJSON": records}
        if values.get("Comprobante_Tipo") and values.get("Comprobante_Tipo") != "FC":
            return {"DatosJSON": []}
        customer_code = values.get("Cliente_Codigo") or ""
        if customer_code and customer_code.strip().upper() not in {"ACU", "NOV"}:
            return {"DatosJSON": []}
        response_customer_code = customer_code or "GEN"
        return {
            "DatosJSON": [
                {
                    "Comprobante_Tipo": "FC",
                    "Comprobante_Letra": "A",
                    "Comprobante_PtoVenta": "0001",
                    "Comprobante_Numero": "00000012",
                    "Comprobante_FechaEmision": "2024-06-01",
                    "Cliente_Codigo": response_customer_code,
                    "Cliente_RazonSocial": "NOVAMED S.A." if customer_code.strip().upper() == "NOV" else "ACUMAR",
                    "Comprobante_TipoOperacion": "REP",
                    "Comprobante_ImporteTotal": 10,
                }
            ]
        }


class FakePdfClient:
    def __init__(self, consult_responses):
        self.consult_responses = list(consult_responses)
        self.consult_calls = 0
        self.generate_calls = 0
        self.timeout = 30
        self.timeout_values = []

    def consultar_comprobante_ventas_pdf(self, reference):
        self.consult_calls += 1
        self.timeout_values.append(self.timeout)
        if self.consult_responses:
            response = self.consult_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"DatosJSON": {}}

    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        self.timeout_values.append(self.timeout)
        return {"Resultado": "OK", "DatosJSON": "{}"}


class FakePdfGenerateTimeoutClient(FakePdfClient):
    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        self.timeout_values.append(self.timeout)
        raise BejermanSdkUnavailable("Read timed out")


class FakePdfGenerateResponseErrorClient(FakePdfClient):
    def __init__(self, consult_responses, message):
        super().__init__(consult_responses)
        self.message = message

    def generar_comprobante_ventas_pdf(self, reference):
        self.generate_calls += 1
        self.timeout_values.append(self.timeout)
        raise BejermanSdkResponseError(self.message)


class FakeArticleCatalogClient:
    def __init__(self):
        self.calls = []

    def list_articulos(self, filters, limit):
        self.calls.append((filters, limit))
        return {
            "DatosJSON": [
                {
                    "Articulo_Codigo": "1202018",
                    "Articulo_Descripcion": "Máscara nasal adulto",
                    "Articulo_Nombre": "Máscara nasal adulto",
                    "Art_StockPorPartida": "S",
                    "Art_TipoTasaVentas": "1",
                    "Art_CodTasaIVAVentas": "01",
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Articulo_Descripcion": "Máscara nasal adulto duplicada",
                    "Articulo_Nombre": "Máscara duplicada",
                    "Art_StockPorPartida": "S",
                },
                {
                    "Articulo_Codigo": "999",
                    "Articulo_Descripcion": "Filtro descartable",
                    "Articulo_Nombre": "Filtro descartable",
                    "Art_StockPorPartida": "N",
                    "Art_TipoTasaVentas": "1",
                    "Art_CodTasaIVAVentas": "03",
                },
            ]
        }


class FakeArticleVatLookupClient:
    def __init__(self, include_filtered_articles=True):
        self.calls = []
        self.include_filtered_articles = include_filtered_articles

    def list_articulos(self, filters, limit):
        self.calls.append((filters, limit))
        filters_text = json.dumps(filters, ensure_ascii=False)
        if "1206001" in filters_text and self.include_filtered_articles:
            return {
                "DatosJSON": [
                    {
                        "Articulo_Codigo": "1206001",
                        "Articulo_Descripcion": "Canula nasal",
                        "Art_TipoTasaVentas": "1",
                        "Art_CodTasaIVAVentas": "01",
                    }
                ]
            }
        return {
            "DatosJSON": [
                {
                    "Articulo_Codigo": "1100001",
                    "Articulo_Descripcion": "Articulo inicial",
                    "Art_TipoTasaVentas": "1",
                    "Art_CodTasaIVAVentas": "03",
                }
            ]
        }


class FakeArticleStockClient:
    def __init__(self):
        self.deposit_calls = []
        self.partida_calls = 0

    def obtener_stock_deposito(self, deposit_code):
        self.deposit_calls.append(deposit_code)
        return {
            "DatosJSON": [
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-VAL-1",
                    "Art_DispUM1": 2,
                    "Art_RealUM1": 3,
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-VAL-0",
                    "Art_DispUM1": 0,
                    "Art_RealUM1": 5,
                },
                {
                    "Articulo_Codigo": "1202018",
                    "Art_CodDeposito": "STL",
                    "Art_Partida": "L-STL-1",
                    "Art_DispUM1": 4,
                    "Art_RealUM1": 4,
                },
                {
                    "Articulo_Codigo": "1202019",
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "L-OTRO",
                    "Art_DispUM1": 9,
                    "Art_RealUM1": 9,
                },
            ]
        }

    def obtener_depositos(self):
        return {
            "DatosJSON": [
                {"Deposito_Codigo": "VAL", "Deposito_Descripcion": "Ventas"},
                {"Deposito_Codigo": "STL", "Deposito_Descripcion": "Listos"},
            ]
        }

    def obtener_partidas(self):
        self.partida_calls += 1
        return {
            "DatosJSON": [
                {
                    "Partida_Codigo": "L-VAL-1",
                    "Deposito_Codigo": "VAL",
                    "Partida_FechaVtoIngreso": "2030-12-31",
                },
                {
                    "Partida_Codigo": "L-STL-1",
                    "Deposito_Codigo": "STL",
                    "Partida_FechaVtoIngreso": "2030-12-31",
                },
            ]
        }


def _pdf_response(pdf_bytes: bytes = b"%PDF-1.4\nRIS test"):
    return {
        "DatosJSON": json.dumps(
            {"archivoPdf": base64.b64encode(pdf_bytes).decode("ascii")},
            ensure_ascii=False,
        )
    }


def _non_pdf_response():
    return {
        "DatosJSON": json.dumps(
            {"archivoPdf": base64.b64encode(b"<html>PDF pendiente</html>").decode("ascii")},
            ensure_ascii=False,
        )
    }


class BejermanPdfHelperTests(SimpleTestCase):
    def test_candidatos_cubren_la_convencion_de_nombre_del_erp(self):
        """El ERP rellena el código de cliente a 6 con espacios a la IZQUIERDA.

        NEXORA sanea y quita los espacios, así que buscando sólo su propio nombre
        daba por ausente un PDF que estaba en la carpeta (caso real:
        `RISR00004-00004252-   NOV.pdf`).
        """

        from service.bejerman_sdk import _comprobante_pdf_filename_candidates

        candidatos = _comprobante_pdf_filename_candidates(
            BejermanPdfReference(
                type="RIS", number="00004252", letter="R", point_of_sale="00004",
                issue_date="2026-05-01", customer_code="NOV",
            )
        )

        self.assertIn("RISR00004-00004252-   NOV.pdf", candidatos)  # ERP
        self.assertIn("RISR00004-00004252-NOV.pdf", candidatos)     # NEXORA
        self.assertIn("RISR00004-00004252.pdf", candidatos)         # sin cliente
        # Convención del SDK, documentada en "10. Generación y Consulta de
        # comprobantes emitidos": V-{TIPO:3}-{L}-{PTO:5}-{NRO:8}-{DDMMAAAA}-{CLI:6}
        self.assertIn("V-RIS-R-00004-00004252-01052026-NOV   .pdf", candidatos)

    def test_consultar_y_generar_pdf_filtran_con_la_misma_fecha(self):
        """Mandaban formatos distintos para el mismo comprobante."""

        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.token = "token"
        client.execute = Mock(return_value={})
        client._execute_once = Mock(return_value={})
        reference = BejermanPdfReference(
            type="RIS", number="00004344", letter="R", point_of_sale="00004", issue_date="2026-08-06",
        )

        client.consultar_comprobante_ventas_pdf(reference)
        client.generar_comprobante_ventas_pdf(reference)

        fecha_consulta = client.execute.call_args.kwargs["params_json"][4]
        fecha_generacion = client._execute_once.call_args.kwargs["params_json"][4]
        self.assertEqual(fecha_consulta, "06/08/2026")
        self.assertEqual(fecha_consulta, fecha_generacion)

    def test_extract_pdf_bytes_normaliza_prefijo_bom_o_espacios(self):
        pdf_bytes = extract_pdf_bytes(_pdf_response(b"\xef\xbb\xbf\r\n  %PDF-1.4\nRSS test"))

        self.assertIsNotNone(pdf_bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertNotIn(b"\xef\xbb\xbf", pdf_bytes[:3])

    def test_extract_pdf_bytes_ignora_base64_que_no_es_pdf(self):
        self.assertIsNone(extract_pdf_bytes(_non_pdf_response()))

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_genera_y_reintenta_hasta_encontrar_pdf(self, _read_remote_candidates):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}, _pdf_response()])

        pdf_bytes, content_type = fetch_comprobante_pdf(
            client,
            BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
            retry_attempts=2,
            retry_delay_ms=0,
        )

        self.assertEqual(content_type, "application/pdf")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    def test_fetch_comprobante_pdf_copia_remito_en_carpeta_sepid_configurada(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakePdfClient([_pdf_response()])
            client.company_key = "SEPID"

            with override_settings(BEJERMAN_PDF_SEPID_REMITOS_DIR=tmp):
                pdf_bytes, content_type = fetch_comprobante_pdf(
                    client,
                    BejermanPdfReference(
                        type="RDA",
                        number="00004680",
                        letter="R",
                        point_of_sale="00004",
                        issue_date="2026-06-24",
                        customer_code="NATIVA",
                    ),
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

            destination = Path(tmp) / "RDAR00004-00004680-NATIVA.pdf"
            self.assertEqual(content_type, "application/pdf")
            self.assertEqual(destination.read_bytes(), pdf_bytes)

    def test_fetch_comprobante_pdf_usa_copia_local_sin_consultar_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cached_pdf = b"\xef\xbb\xbf\n%PDF-1.4\ncached"
            destination = Path(tmp) / "RTR00004-00004707-RES.pdf"
            destination.write_bytes(cached_pdf)
            client = FakePdfClient([_pdf_response(b"%PDF-1.4\nfresh")])
            client.company_key = "SEPID"

            with override_settings(BEJERMAN_PDF_SEPID_REMITOS_DIR=tmp):
                pdf_bytes, content_type = fetch_comprobante_pdf(
                    client,
                    BejermanPdfReference(
                        type="RT",
                        number="00004707",
                        letter="R",
                        point_of_sale="00004",
                        issue_date="2026-06-26",
                        customer_code="RES",
                    ),
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(pdf_bytes, b"%PDF-1.4\ncached")
        self.assertEqual(client.consult_calls, 0)
        self.assertEqual(client.generate_calls, 0)

    def test_fetch_comprobante_pdf_usa_copia_local_sin_cliente_en_nombre(self):
        with tempfile.TemporaryDirectory() as tmp:
            cached_pdf = b"%PDF-1.4\nhome air"
            destination = Path(tmp) / "RTR00007-00000665.pdf"
            destination.write_bytes(cached_pdf)
            client = FakePdfClient([_pdf_response(b"%PDF-1.4\nfresh")])
            client.company_key = "MGBIO"
            reference = BejermanPdfReference(
                type="RT",
                number="00000665",
                letter="R",
                point_of_sale="00007",
                issue_date="2026-07-02",
                customer_code="HAIR",
            )

            self.assertIn("RTR00007-00000665.pdf", _comprobante_pdf_filename_candidates(reference))

            with override_settings(BEJERMAN_PDF_MGBIO_REMITOS_DIR=tmp):
                pdf_bytes, content_type = fetch_comprobante_pdf(
                    client,
                    reference,
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(pdf_bytes, cached_pdf)
        self.assertEqual(client.consult_calls, 0)
        self.assertEqual(client.generate_calls, 0)

    def test_fetch_comprobante_pdf_ignora_copia_local_invalida_y_consulta_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "RTR00004-00004707-RES.pdf"
            destination.write_bytes(b"<html>pendiente</html>")
            client = FakePdfClient([_pdf_response(b"%PDF-1.4\nfresh")])
            client.company_key = "SEPID"

            with override_settings(BEJERMAN_PDF_SEPID_REMITOS_DIR=tmp):
                pdf_bytes, content_type = fetch_comprobante_pdf(
                    client,
                    BejermanPdfReference(
                        type="RT",
                        number="00004707",
                        letter="R",
                        point_of_sale="00004",
                        issue_date="2026-06-26",
                        customer_code="RES",
                    ),
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(pdf_bytes, b"%PDF-1.4\nfresh")
        self.assertEqual(client.consult_calls, 1)
        self.assertEqual(client.generate_calls, 0)

    @patch("service.bejerman_sdk.write_remote_pdf")
    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(b"%PDF-1.4\nremote", r"C:\2. SEPID\PDF\REMITOS\RTR00004-00004707-RES.pdf"))
    def test_fetch_comprobante_pdf_usa_copia_remota_samtronic_sin_consultar_sdk(self, read_remote_pdf_candidates, write_remote_pdf):
        client = FakePdfClient([_pdf_response(b"%PDF-1.4\nfresh")])
        client.company_key = "SEPID"

        with override_settings(
            BEJERMAN_PDF_SEPID_REMITOS_DIR=r"C:\2. SEPID\PDF\REMITOS",
            BEJERMAN_PDF_WINDOWS_PATH_MOUNTS=r"C=samtronic:C:\,Z=/mnt/nexora-z",
        ):
            pdf_bytes, content_type = fetch_comprobante_pdf(
                client,
                BejermanPdfReference(
                    type="RT",
                    number="00004707",
                    letter="R",
                    point_of_sale="00004",
                    issue_date="2026-06-26",
                    customer_code="RES",
                ),
                retry_attempts=1,
                retry_delay_ms=0,
            )

        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(pdf_bytes, b"%PDF-1.4\nremote")
        # Se buscan todas las convenciones que conviven en la carpeta. Con sólo la
        # propia, un PDF escrito por el ERP o por el SDK quedaba invisible.
        # Aserción por pertenencia y no por igualdad: agregar un candidato nuevo no
        # tiene por qué romper este test.
        read_remote_pdf_candidates.assert_called_once()
        args, kwargs = read_remote_pdf_candidates.call_args
        self.assertEqual(args[0], r"samtronic:C:\2. SEPID\PDF\REMITOS")
        self.assertIn("RTR00004-00004707-RES.pdf", args[1])           # NEXORA
        self.assertIn("RTR00004-00004707-   RES.pdf", args[1])        # ERP (rjust 6)
        self.assertIn("V-RT -R-00004-00004707-26062026-RES   .pdf", args[1])  # SDK
        self.assertIn("RTR00004-00004707", args[2])
        self.assertIn("V-RT -R-00004-00004707-", args[2])
        self.assertTrue(kwargs["use_cache"])
        write_remote_pdf.assert_not_called()
        self.assertEqual(client.consult_calls, 0)
        self.assertEqual(client.generate_calls, 0)

    def test_fetch_comprobante_pdf_copia_remito_en_carpeta_mgbio_configurada(self):
        with tempfile.TemporaryDirectory() as sepid_tmp, tempfile.TemporaryDirectory() as mgbio_tmp:
            client = FakePdfClient([_pdf_response()])
            client.company_key = "MGBIO"

            with override_settings(
                BEJERMAN_PDF_SEPID_REMITOS_DIR=sepid_tmp,
                BEJERMAN_PDF_MGBIO_REMITOS_DIR=mgbio_tmp,
            ):
                fetch_comprobante_pdf(
                    client,
                    BejermanPdfReference(
                        type="RT",
                        number="00000042",
                        letter="R",
                        point_of_sale="00007",
                        issue_date="2026-06-24",
                        customer_code="OXIHC",
                    ),
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

            self.assertEqual(list(Path(sepid_tmp).iterdir()), [])
            self.assertTrue((Path(mgbio_tmp) / "RTR00007-00000042- OXIHC.pdf").exists())

    def test_windows_pdf_output_path_se_traduce_al_mount_configurado(self):
        with tempfile.TemporaryDirectory() as mount_tmp:
            with override_settings(BEJERMAN_PDF_WINDOWS_PATH_MOUNTS=f"Z={mount_tmp}"):
                resolved = resolve_pdf_output_path(r"Z:\MG BIO\Remitos BEJERMAN")

        self.assertEqual(resolved, str(Path(mount_tmp) / "MG BIO" / "Remitos BEJERMAN"))

    def test_windows_c_pdf_output_path_se_traduce_a_samtronic_remoto(self):
        with override_settings(BEJERMAN_PDF_WINDOWS_PATH_MOUNTS=r"C=samtronic:C:\,Z=/mnt/nexora-z"):
            resolved = resolve_pdf_output_path(r"C:\2. SEPID\PDF\REMITOS")

        self.assertEqual(resolved, r"samtronic:C:\2. SEPID\PDF\REMITOS")

    def test_windows_pdf_output_path_sin_mount_falla(self):
        with (
            override_settings(BEJERMAN_PDF_WINDOWS_PATH_MOUNTS=""),
            patch("service.bejerman_pdf_settings.os", name="posix") as operating_system,
        ):
            operating_system.name = "posix"
            with self.assertRaises(BejermanPdfOutputSettingsError):
                validate_pdf_output_dir(r"Z:\MG BIO\Remitos BEJERMAN")

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    @patch("service.bejerman_sdk.remove_remote_pdf_candidates_if_same")
    @patch("service.bejerman_sdk.write_remote_pdf")
    def test_fetch_comprobante_pdf_copia_remito_a_samtronic_y_limpia_facturas(
        self, write_remote_pdf, remove_remote_candidates, _read_remote_candidates
    ):
        # La limpieza prueba primero el nombre exacto de Crystal (barato); si no
        # encuentra nada ahí (primera llamada -> []), recién entonces cae al barrido
        # completo por prefijos (segunda llamada), que es la que sí encuentra el archivo.
        remove_remote_candidates.side_effect = [
            [],
            [r"C:\2. SEPID\PDF\FACTURAS\V-RT -R-00004-00004707-26062026-RES   .pdf"],
        ]
        write_remote_pdf.return_value = Mock(path=r"C:\2. SEPID\PDF\REMITOS\RTR00004-00004707-RES.pdf")
        client = FakePdfClient([_pdf_response()])
        client.company_key = "SEPID"

        with override_settings(
            BEJERMAN_PDF_SEPID_REMITOS_DIR=r"C:\2. SEPID\PDF\REMITOS",
            BEJERMAN_PDF_WINDOWS_PATH_MOUNTS=r"C=samtronic:C:\,Z=/mnt/nexora-z",
            BEJERMAN_PDF_SAMTRONIC_FACTURAS_DIR=r"C:\2. SEPID\PDF\FACTURAS",
            BEJERMAN_PDF_CLEAN_REMOTE_REMITOS_FROM_FACTURAS=True,
        ):
            pdf_bytes, _ = fetch_comprobante_pdf(
                client,
                BejermanPdfReference(
                    type="RT",
                    number="00004707",
                    letter="R",
                    point_of_sale="00004",
                    issue_date="2026-06-26",
                    customer_code="RES",
                ),
                retry_attempts=1,
                retry_delay_ms=0,
            )

        write_remote_pdf.assert_called_once()
        self.assertEqual(write_remote_pdf.call_args.args[0], r"samtronic:C:\2. SEPID\PDF\REMITOS")
        self.assertEqual(write_remote_pdf.call_args.args[1], "RTR00004-00004707-   RES.pdf")
        self.assertEqual(write_remote_pdf.call_args.args[2], pdf_bytes)
        self.assertEqual(remove_remote_candidates.call_count, 2)
        remove_remote_candidates.assert_any_call(
            r"samtronic:C:\2. SEPID\PDF\FACTURAS",
            ["V-RT -R-00004-00004707-26062026-RES   .pdf"],
            [],
            pdf_bytes,
        )
        fallback = remove_remote_candidates.call_args_list[-1]
        self.assertEqual(fallback.args[0], r"samtronic:C:\2. SEPID\PDF\FACTURAS")
        self.assertEqual(set(fallback.args[1]), {
            "V-RT -R-00004-00004707-26062026-RES   .pdf",
            "RTR00004-00004707-   RES.pdf", "RTR00004-00004707.pdf", "RTR00004-00004707-RES.pdf",
        })
        self.assertEqual(fallback.args[2], ["V-RT -R-00004-00004707-", "RTR00004-00004707"])
        self.assertEqual(fallback.args[3], pdf_bytes)

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_no_devuelve_200_con_bytes_no_pdf(self, _read_remote_candidates):
        client = FakePdfClient([_non_pdf_response(), _non_pdf_response()])

        with self.assertRaises(BejermanPdfPendingError):
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=1,
                retry_delay_ms=0,
            )

        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 2)

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_pendiente_no_es_error_generico(self, _read_remote_candidates):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}, {"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=2,
                retry_delay_ms=0,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 1000)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_consulta_error_persistente_queda_pendiente(self, _read_remote_candidates):
        client = FakePdfClient(
            [
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
                BejermanSdkResponseError("HTTP 406 Bejerman: PDF no disponible"),
            ]
        )

        with self.assertRaises(BejermanPdfPendingError):
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                retry_attempts=2,
                retry_delay_ms=0,
            )

        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 3)

    @override_settings(
        BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT=4,
        BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS=1,
        BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS=0,
        BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS=2500,
    )
    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_interactivo_usa_timeout_y_reintentos_cortos(self, _read_remote_candidates):
        client = FakePdfClient([{"DatosJSON": "{}"}, {"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                interactive=True,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 2500)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 2)
        self.assertEqual(client.timeout, 30)
        self.assertTrue(client.timeout_values)
        self.assertTrue(all(value == 4 for value in client.timeout_values))

    @patch("service.bejerman_sdk.read_remote_pdf_candidates", return_value=(None, ""))
    def test_fetch_comprobante_pdf_interactivo_timeout_generando_queda_pendiente_rapido(self, _read_remote_candidates):
        client = FakePdfGenerateTimeoutClient([{"DatosJSON": "{}"}])

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00000001", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                interactive=True,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 1200)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 1)
        self.assertEqual(client.timeout, 30)
        self.assertTrue(client.timeout_values)
        self.assertTrue(all(value == 4 for value in client.timeout_values))

    @override_settings(
        BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT=4,
        BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS=1,
        BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS=0,
        BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS=2500,
    )
    def test_fetch_comprobante_pdf_interactivo_comprobante_no_encontrado_queda_pendiente(self):
        client = FakePdfGenerateResponseErrorClient(
            [{"DatosJSON": "{}"}],
            "No se encontró el comprobante asociado",
        )

        with self.assertRaises(BejermanPdfPendingError) as ctx:
            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(type="RIS", number="00004571", letter="R", point_of_sale="00004", issue_date="2026-06-12"),
                interactive=True,
            )

        self.assertEqual(ctx.exception.retry_after_ms, 2500)
        self.assertEqual(client.generate_calls, 1)
        self.assertEqual(client.consult_calls, 1)
        self.assertEqual(client.timeout, 30)
        self.assertTrue(client.timeout_values)
        self.assertTrue(all(value == 4 for value in client.timeout_values))


@override_settings(SECURE_SSL_REDIRECT=False)
class BejermanPdfOutputSettingsTests(TestCase):
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
        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_bejerman_pdf_settings_schema", verbosity=0)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_pdf_output_settings")
        User.objects.filter(email__endswith="@pdf-settings.test").delete()
        self.jefe = User.objects.create(
            nombre="Jefe PDF",
            email="jefe@pdf-settings.test",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        self.recepcion = User.objects.create(
            nombre="Recepcion PDF",
            email="recepcion@pdf-settings.test",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )

    def _insert_setting(self, company_key, document_kind, output_dir):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_pdf_output_settings(company_key, document_kind, output_dir)
                VALUES (%s, %s, %s)
                """,
                [company_key, document_kind, output_dir],
            )

    def _client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token(user)}")
        return client

    def test_db_config_de_remitos_tiene_prioridad_sobre_env(self):
        with tempfile.TemporaryDirectory() as db_tmp, tempfile.TemporaryDirectory() as env_tmp:
            self._insert_setting("SEPID", "REMITOS", db_tmp)
            client = FakePdfClient([_pdf_response()])
            client.company_key = "SEPID"

            with override_settings(BEJERMAN_PDF_SEPID_REMITOS_DIR=env_tmp):
                pdf_bytes, _ = fetch_comprobante_pdf(
                    client,
                    BejermanPdfReference(
                        type="RDA",
                        number="00004680",
                        letter="R",
                        point_of_sale="00004",
                        issue_date="2026-06-24",
                        customer_code="NATIVA",
                    ),
                    retry_attempts=1,
                    retry_delay_ms=0,
                )

            self.assertEqual((Path(db_tmp) / "RDAR00004-00004680-NATIVA.pdf").read_bytes(), pdf_bytes)
            self.assertEqual(list(Path(env_tmp).iterdir()), [])

    def test_remito_no_usa_carpeta_de_facturas(self):
        with tempfile.TemporaryDirectory() as facturas_tmp:
            self._insert_setting("SEPID", "FACTURAS", facturas_tmp)
            client = FakePdfClient([_pdf_response()])
            client.company_key = "SEPID"

            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(
                    type="RDN",
                    number="00004681",
                    letter="R",
                    point_of_sale="00004",
                    issue_date="2026-06-24",
                    customer_code="ACU",
                ),
                retry_attempts=1,
                retry_delay_ms=0,
            )

            self.assertEqual(list(Path(facturas_tmp).iterdir()), [])

    def test_remito_respeta_configuracion_por_empresa(self):
        with tempfile.TemporaryDirectory() as sepid_tmp, tempfile.TemporaryDirectory() as mgbio_tmp:
            self._insert_setting("SEPID", "REMITOS", sepid_tmp)
            self._insert_setting("MGBIO", "REMITOS", mgbio_tmp)
            client = FakePdfClient([_pdf_response()])
            client.company_key = "MGBIO"

            fetch_comprobante_pdf(
                client,
                BejermanPdfReference(
                    type="RT",
                    number="00000042",
                    letter="R",
                    point_of_sale="00007",
                    issue_date="2026-06-24",
                    customer_code="OXIHC",
                ),
                retry_attempts=1,
                retry_delay_ms=0,
            )

            self.assertEqual(list(Path(sepid_tmp).iterdir()), [])
            self.assertTrue((Path(mgbio_tmp) / "RTR00007-00000042- OXIHC.pdf").exists())

    def test_endpoint_guarda_y_devuelve_configuracion(self):
        with tempfile.TemporaryDirectory() as remitos_tmp:
            response = self._client_for(self.jefe).put(
                "/api/bejerman/pdf-output-settings/",
                {
                    "items": [
                        {"companyKey": "SEPID", "remitosDir": remitos_tmp, "facturasDir": ""},
                        {"companyKey": "MGBIO", "remitosDir": "", "facturasDir": ""},
                    ]
                },
                format="json",
            )

            self.assertEqual(response.status_code, 200)
            item = next(row for row in response.data["items"] if row["companyKey"] == "SEPID")
            self.assertEqual(item["remitos"]["outputDir"], remitos_tmp)
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT output_dir
                      FROM bejerman_pdf_output_settings
                     WHERE company_key = 'SEPID'
                       AND document_kind = 'REMITOS'
                    """
                )
                self.assertEqual(cur.fetchone()[0], remitos_tmp)

    def test_endpoint_usa_empresas_configuradas_para_selector(self):
        with tempfile.TemporaryDirectory() as remitos_tmp:
            response = self._client_for(self.jefe).put(
                "/api/bejerman/pdf-output-settings/",
                {
                    "items": [
                        {"companyKey": "TEST", "remitosDir": remitos_tmp, "facturasDir": ""},
                    ]
                },
                format="json",
            )

            self.assertEqual(response.status_code, 200, response.data)
            self.assertIn("companies", response.data)
            self.assertTrue(any(company["key"] == "TEST" for company in response.data["companies"]))
            item = next(row for row in response.data["items"] if row["companyKey"] == "TEST")
            self.assertEqual(item["remitos"]["outputDir"], remitos_tmp)

    def test_endpoint_rechaza_usuario_sin_permiso(self):
        response = self._client_for(self.recepcion).get("/api/bejerman/pdf-output-settings/")
        self.assertEqual(response.status_code, 403)

        response = self._client_for(self.recepcion).put(
            "/api/bejerman/pdf-output-settings/",
            {"items": []},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class BejermanDeliveryBillingTests(SimpleTestCase):
    def test_facturacion_uses_sdk_sales_filters(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_facturacion_from_bejerman(
                "ACU",
                {
                    "dateFrom": "2024-01-01",
                    "dateTo": "2024-12-31",
                    "origin": "Reparación",
                    "page": "2",
                    "pageSize": "25",
                    "search": "FAC",
                },
            )

        self.assertEqual(client.calls[0][0], "list_clientes")
        self.assertEqual(client.calls[1][0], "list_comprobantes_ventas")
        self.assertEqual(len([call for call in client.calls if call[0] == "list_comprobantes_ventas"]), 1)
        self.assertEqual(
            client.calls[1][1],
            [
                {"Campo": "Cliente_Codigo", "Accion": "IGUAL", "Valor": "ACU", "Operacion": "Y", "Enlazada": False},
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MAYOR O IGUAL",
                    "Valor": "2024-01-01",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MENOR O IGUAL",
                    "Valor": "2024-12-31",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
            ],
        )
        self.assertEqual(result["pagination"]["pageSize"], 25)

    def test_facturacion_accepts_legacy_filter_aliases(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            list_facturacion_from_bejerman(
                "ACU",
                {
                    "desde": "2024-01-01",
                    "hasta": "2024-12-31",
                    "tipo": "Reparación",
                },
            )

        self.assertEqual(client.calls[1][1][1]["Valor"], "2024-01-01")
        self.assertEqual(client.calls[1][1][2]["Valor"], "2024-12-31")

    def test_facturacion_resuelve_codigo_bejerman_por_nombre_local(self):
        client = FakeSDKClient(
            [
                {
                    "Cliente_Codigo": "   NOV",
                    "Cliente_RazonSocial": "NOVAMED S.A.",
                    "Cliente_NroDocumento": "30710659768",
                }
            ]
        )

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch(
                "service.bejerman_delivery._local_customer_for_code",
                return_value={"name": "NOVAMED S.A.", "cuit": "", "code": "ame"},
            ),
        ):
            result = list_facturacion_from_bejerman("ame", {"dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        self.assertEqual(result["effectiveCustomerCode"], "NOV")
        self.assertEqual(result["bejermanCustomerCode"], "   NOV")
        self.assertEqual(client.calls[1][1][0]["Valor"], "   NOV")
        self.assertEqual(result["pagination"]["total"], 1)

    def test_facturacion_general_omite_filtro_de_cliente_y_pagina_de_a_25(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_facturacion_from_bejerman("", {"dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        self.assertEqual(client.calls[0][0], "list_comprobantes_ventas")
        self.assertEqual(len([call for call in client.calls if call[0] == "list_clientes"]), 0)
        self.assertFalse(any(item["Campo"] == "Cliente_Codigo" for item in client.calls[0][1]))
        self.assertFalse(any(item["Campo"] == "Comprobante_Tipo" for item in client.calls[0][1]))
        self.assertEqual(result["scope"], "company")
        self.assertEqual(result["pagination"]["pageSize"], 25)

    def test_facturacion_tipo_especifico_mantiene_filtro_de_comprobante(self):
        client = FakeSDKClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_facturacion_from_bejerman("", {"origin": "FC", "dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        sales_calls = [call for call in client.calls if call[0] == "list_comprobantes_ventas"]
        self.assertEqual(len(sales_calls), 1)
        self.assertTrue(any(item["Campo"] == "Comprobante_Tipo" and item["Valor"] == "FC" for item in sales_calls[0][1]))
        self.assertEqual(result["pagination"]["total"], 1)

    def test_facturacion_exp_sin_cuotas_devuelve_error_legible(self):
        class FailingBillingClient(FakeSDKClient):
            def list_comprobantes_ventas(self, filters):
                self.calls.append(("list_comprobantes_ventas", filters))
                raise BejermanSdkResponseError(
                    "502 : Error en la ejecución. // There is already an object named 'expSinCuotasV1' in the database."
                )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=FailingBillingClient()):
            with self.assertRaises(BillingError) as ctx:
                list_facturacion_from_bejerman("", {"dateFrom": "2024-01-01", "dateTo": "2024-12-31"})

        self.assertEqual(ctx.exception.code, "BEJERMAN_BILLING_QUERY_BUSY")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("expSinCuotasV1", str(ctx.exception))

    def test_remitos_usa_filtros_de_tipo_operacion_cliente_y_empresa(self):
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RSS",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004567",
                    "Comprobante_FechaEmision": "2026-06-20",
                    "Cliente_Codigo": "ACU",
                    "Cliente_RazonSocial": "ACUMAR",
                    "Comprobante_TipoOperacion": "REP",
                    "Comprobante_ImporteTotal": 0,
                }
            ]
        )
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client) as client_cls:
            result = list_remitos_from_bejerman(
                "ACU",
                {
                    "dateFrom": "2026-01-01",
                    "dateTo": "2026-12-31",
                    "remitoType": "RSS",
                    "operationType": "REP",
                    "pageSize": "10",
                },
                actor_user_id=1152,
                company_key="MGBIO",
            )

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)
        self.assertEqual(client.calls[0][0], "list_clientes")
        self.assertEqual(client.calls[1][0], "list_comprobantes_ventas")
        self.assertEqual(
            client.calls[1][1],
            [
                {"Campo": "Cliente_Codigo", "Accion": "IGUAL", "Valor": "ACU", "Operacion": "Y", "Enlazada": False},
                {"Campo": "Comprobante_Tipo", "Accion": "IGUAL", "Valor": "RSS", "Operacion": "Y", "Enlazada": False},
                {"Campo": "Comprobante_TipoOperacion", "Accion": "IGUAL", "Valor": "REP", "Operacion": "Y", "Enlazada": False},
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MAYOR O IGUAL",
                    "Valor": "2026-01-01",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
                {
                    "Campo": "Comprobante_FechaEmision",
                    "Accion": "MENOR O IGUAL",
                    "Valor": "2026-12-31",
                    "Operacion": "Y",
                    "Enlazada": False,
                },
            ],
        )
        self.assertEqual(result["companyKey"], "MGBIO")
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RSS R 00004-00004567")
        self.assertEqual(result["items"][0]["operationCode"], "REP")

    def test_remitos_todas_las_empresas_encuentra_home_air_en_mgbio(self):
        clients = {
            "SEPID": FakeSDKClient(sales_records=[]),
            "MGBIO": FakeSDKClient(
                sales_records=[
                    {
                        "Comprobante_Tipo": "RT",
                        "Comprobante_Letra": "R",
                        "Comprobante_PtoVenta": "00007",
                        "Comprobante_Numero": "00000665",
                        "Comprobante_FechaEmision": "2026-07-02",
                        "Cliente_Codigo": "HAIR",
                        "Cliente_RazonSocial": "HOME AIR S.R.L.",
                        "Comprobante_TipoOperacion": "MC",
                        "Comprobante_ImporteTotal": 0,
                    }
                ]
            ),
        }

        def client_factory(*, company_key=None, actor_user_id=None, **_kwargs):
            return clients[company_key]

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", side_effect=client_factory) as client_cls,
            patch("service.bejerman_delivery.resolve_remito_group_for_document_id", return_value=None),
        ):
            result = list_remitos_from_bejerman(
                "",
                {"companyKey": "ALL", "search": "HOME AIR", "pageSize": "25"},
                actor_user_id=1152,
            )

        self.assertEqual(result["companyKey"], "ALL")
        self.assertEqual(result["companies"], ["SEPID", "MGBIO"])
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RT R 00007-00000665")
        self.assertEqual(result["items"][0]["customerName"], "HOME AIR S.R.L.")
        self.assertEqual(result["items"][0]["companyKey"], "MGBIO")
        called_companies = [call.kwargs["company_key"] for call in client_cls.call_args_list]
        self.assertIn("SEPID", called_companies)
        self.assertIn("MGBIO", called_companies)

    def test_remitos_acepta_tipo_rd(self):
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RD",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004682",
                    "Comprobante_FechaEmision": "2026-06-24",
                    "Cliente_Codigo": "EHOSP",
                    "Cliente_RazonSocial": "EQUIPO HOSPITALARIO",
                    "Comprobante_TipoOperacion": "REP",
                }
            ]
        )
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman("", {"remitoType": "RD"})

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RD R 00004-00004682")
        sales_calls = [call for call in client.calls if call[0] == "list_comprobantes_ventas"]
        self.assertEqual(sales_calls[0][1][0]["Valor"], "RD")

    def test_remitos_general_consulta_tipos_soportados_y_filtra_busqueda(self):
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RT",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00000001",
                    "Comprobante_FechaEmision": "2026-06-20",
                    "Cliente_Codigo": "GEN",
                    "Cliente_RazonSocial": "CLIENTE UNO",
                    "Comprobante_TipoOperacion": "MC",
                },
                {
                    "Comprobante_Tipo": "RDA",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00000002",
                    "Comprobante_FechaEmision": "2026-06-21",
                    "Cliente_Codigo": "GEN",
                    "Cliente_RazonSocial": "CLIENTE DOS",
                    "Comprobante_TipoOperacion": "ALQ",
                },
            ]
        )
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "",
                {"dateFrom": "2026-01-01", "dateTo": "2026-12-31", "search": "00000002", "pageSize": "25"},
            )

        sales_calls = [call for call in client.calls if call[0] == "list_comprobantes_ventas"]
        self.assertEqual(len(sales_calls), 8)
        self.assertFalse(any(item["Campo"] == "Cliente_Codigo" for item in sales_calls[0][1]))
        self.assertEqual(result["scope"], "company")
        self.assertEqual(result["companyKey"], "SEPID")
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["type"], "RDA")

    def test_remitos_general_tolera_falla_parcial_de_un_tipo(self):
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RT",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00000001",
                    "Comprobante_FechaEmision": "2026-06-20",
                    "Cliente_Codigo": "GEN",
                    "Cliente_RazonSocial": "CLIENTE UNO",
                    "Comprobante_TipoOperacion": "MC",
                },
            ],
            fail_sales_types={"RSS"},
        )
        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch("service.bejerman_delivery._local_generated_remito_items", return_value=[]),
        ):
            result = list_remitos_from_bejerman("", {"dateFrom": "2026-01-01", "dateTo": "2026-12-31"})

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["type"], "RT")
        self.assertEqual(result["partialErrors"][0]["remitoType"], "RSS")
        self.assertIn("RSS", result["warning"])
        sales_calls = [call for call in client.calls if call[0] == "list_comprobantes_ventas"]
        self.assertEqual(len(sales_calls), 8)

    def test_remitos_tipo_especifico_rss_fallido_devuelve_aviso_sin_romper(self):
        client = FakeSDKClient(fail_sales_types={"RSS"})
        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch("service.bejerman_delivery._local_generated_remito_items", return_value=[]),
        ):
            result = list_remitos_from_bejerman("", {"remitoType": "RSS"})

        self.assertEqual(result["pagination"]["total"], 0)
        self.assertEqual(result["partialErrors"][0]["remitoType"], "RSS")
        self.assertIn("RSS", result["warning"])

    def test_remitos_deduplica_items_extra_por_document_id(self):
        raw = {
            "Comprobante_Tipo": "RSS",
            "Comprobante_Letra": "R",
            "Comprobante_PtoVenta": "00004",
            "Comprobante_Numero": "00004500",
            "Comprobante_FechaEmision": "2026-06-02",
            "Cliente_Codigo": "TMD",
            "Cliente_RazonSocial": "TMD S.A.",
            "Comprobante_TipoOperacion": "REP",
        }
        document_id = build_document_id({"t": "RSS", "n": "00004500", "l": "R", "p": "00004", "f": "2026-06-02", "c": "TMD"})

        result = build_remitos_result(
            {"DatosJSON": [raw]},
            "TMD",
            {"remitoType": "RSS"},
            extra_items=[
                {
                    "id": document_id,
                    "documentId": document_id,
                    "type": "RSS",
                    "documentNumber": "RSS R 00004-00004500",
                    "issueDate": "2026-06-02",
                    "operationCode": "REP",
                    "remitoGroupId": "brg-rss-local",
                    "pdfUrl": "/api/ordenes-entrega/remito-bejerman/brg-rss-local/pdf/",
                    "printUrl": "/api/ordenes-entrega/remito-bejerman/brg-rss-local/print/",
                    "source": "nexora",
                }
            ],
        )

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["customerName"], "TMD S.A.")
        self.assertEqual(result["items"][0]["remitoGroupId"], "brg-rss-local")
        self.assertEqual(result["items"][0]["pdfUrl"], "/api/ordenes-entrega/remito-bejerman/brg-rss-local/pdf/")
        self.assertEqual(result["items"][0]["source"], "nexora")

    def test_documentos_ordenan_numeros_naturalmente_antes_de_paginar(self):
        records = [
            {
                "Comprobante_Tipo": "FC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00000010",
                "Comprobante_FechaEmision": "2026-06-10",
                "Cliente_Codigo": "DOS",
                "Cliente_RazonSocial": "Cliente 10",
                "Comprobante_ImporteTotal": "100",
            },
            {
                "Comprobante_Tipo": "FC",
                "Comprobante_Letra": "A",
                "Comprobante_PtoVenta": "00002",
                "Comprobante_Numero": "00000002",
                "Comprobante_FechaEmision": "2026-06-02",
                "Cliente_Codigo": "UNO",
                "Cliente_RazonSocial": "Cliente 2",
                "Comprobante_ImporteTotal": "200",
            },
        ]

        result = build_facturacion_result(
            {"DatosJSON": records},
            "",
            {"sort": "numero", "page": "1", "pageSize": "1"},
        )

        self.assertEqual(result["pagination"]["total"], 2)
        self.assertEqual(result["items"][0]["number"], "00000002")

    def test_documentos_mantienen_vacios_al_final_en_orden_descendente(self):
        records = [
            {
                "Comprobante_Tipo": "RSS",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00000001",
                "Comprobante_FechaEmision": "2026-06-01",
                "Cliente_Codigo": "VAC",
                "Cliente_RazonSocial": "",
            },
            {
                "Comprobante_Tipo": "RSS",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00000002",
                "Comprobante_FechaEmision": "2026-06-02",
                "Cliente_Codigo": "DOS",
                "Cliente_RazonSocial": "Beta",
            },
        ]

        result = build_remitos_result(
            {"DatosJSON": records},
            "",
            {"sort": "-cliente", "pageSize": "25"},
        )

        self.assertEqual([item["customerName"] for item in result["items"]], ["Beta", ""])

    def test_remitos_rechaza_tipo_o_operacion_invalidos(self):
        with self.assertRaises(BillingError) as remito_ctx:
            list_remitos_from_bejerman("", {"remitoType": "FC"})
        self.assertEqual(remito_ctx.exception.code, "INVALID_REMITO_TYPE")

        with self.assertRaises(BillingError) as operation_ctx:
            list_remitos_from_bejerman("", {"operationType": "XXX"})
        self.assertEqual(operation_ctx.exception.code, "INVALID_OPERATION_TYPE")

    def test_build_sales_filters_preserves_padded_customer_code(self):
        filters = build_sales_filters("   NOV", "2024-01-01", "2024-12-31", "FC")

        self.assertEqual(filters[0]["Valor"], "   NOV")
        self.assertEqual(filters[1]["Valor"], "FC")

    def test_build_sales_filters_allows_company_scope(self):
        filters = build_sales_filters("", "2024-01-01", "2024-12-31", "FC")

        self.assertFalse(any(item["Campo"] == "Cliente_Codigo" for item in filters))
        self.assertEqual(filters[0]["Campo"], "Comprobante_Tipo")

    def test_facturacion_pdf_interactivo_propaga_estado_pendiente_del_coordinador(self):
        document_id = build_document_id(
            {"t": "RSS", "n": "00000012", "l": "R", "p": "0004", "f": "2026-06-12", "c": "ACU"}
        )

        pending = BejermanPdfRequestPending(
            "El PDF oficial quedó en cola para solicitarse una sola vez a Bejerman.",
            pdf_state="queued",
            generation_requested=False,
            retry_after_ms=2500,
        )
        with patch(
            "service.bejerman_delivery.fetch_or_enqueue_comprobante_pdf",
            side_effect=pending,
        ) as coordinator:
            with self.assertRaises(BillingError) as ctx:
                get_facturacion_pdf("ACU", document_id, interactive=True)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_PENDING")
        self.assertEqual(ctx.exception.status_code, 202)
        self.assertEqual(ctx.exception.retry_after_ms, 2500)
        self.assertEqual(ctx.exception.payload, {"pdf_state": "queued", "generation_requested": False})
        self.assertEqual(coordinator.call_args.kwargs, {"actor_user_id": None, "remito_group_id": None})

    def test_facturacion_pdf_worker_detenido_devuelve_503_y_no_202(self):
        """Candado de regresión del 202 eterno.

        Si BejermanPdfWorkerStalledError heredara de BejermanSdkUnavailable, el
        `except` interactivo de get_facturacion_pdf lo convertiría en un 202 y el
        navegador volvería a girar para siempre.
        """

        document_id = build_document_id(
            {"t": "RSS", "n": "00000012", "l": "R", "p": "0004", "f": "2026-06-12", "c": "ACU"}
        )

        stalled = BejermanPdfWorkerStalledError(
            "El servicio que prepara los PDF de Bejerman no está respondiendo.",
            pdf_state="queued",
            generation_requested=False,
            details={"worker_alive": False, "stage_label": "En cola para solicitar la generación"},
        )
        with patch(
            "service.bejerman_delivery.fetch_or_enqueue_comprobante_pdf",
            side_effect=stalled,
        ):
            with self.assertRaises(BillingError) as ctx:
                get_facturacion_pdf("ACU", document_id, interactive=True)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_WORKER_STALLED")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotEqual(ctx.exception.status_code, 202)
        self.assertIs(ctx.exception.payload["worker_alive"], False)

    def test_facturacion_pdf_exp_cuotas_devuelve_error_legible(self):
        document_id = build_document_id(
            {"t": "RTN", "n": "00004314", "l": "R", "p": "00004", "f": "2026-05-15", "c": "PRAX"}
        )

        with patch(
            "service.bejerman_delivery.fetch_or_enqueue_comprobante_pdf",
            side_effect=BejermanSdkResponseError("Invalid object name 'expCuotasV'."),
        ):
            with self.assertRaises(BillingError) as ctx:
                get_facturacion_pdf("PRAX", document_id, actor_user_id=1152, company_key="SEPID")

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_GENERATION_ERROR")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("expCuotasV", str(ctx.exception))

    def test_facturacion_pdf_error_sql_interno_devuelve_error_legible(self):
        document_id = build_document_id(
            {"t": "RSS", "n": "00004315", "l": "R", "p": "00004", "f": "2026-05-15", "c": "PRAX"}
        )
        sdk_error = (
            "Invalid column name 'IMPORTE_TOTAL'. Invalid column name 'ConvMonLocal'. "
            'The multi-part identifier "CabVenta.cve_ID" could not be bound. '
            "ORDER BY items must appear in the select list if the statement contains a UNION operator."
        )

        with patch(
            "service.bejerman_delivery.fetch_or_enqueue_comprobante_pdf",
            side_effect=BejermanSdkResponseError(sdk_error),
        ):
            with self.assertRaises(BillingError) as ctx:
                get_facturacion_pdf("PRAX", document_id, actor_user_id=1152, company_key="SEPID")

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_GENERATION_ERROR")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("Bejerman no pudo consultar o generar el PDF", str(ctx.exception))
        self.assertNotIn("CabVenta", str(ctx.exception))

    def test_facturacion_pdf_usa_empresa_bejerman_indicada(self):
        document_id = build_document_id(
            {"t": "RT", "n": "00000012", "l": "R", "p": "00007", "f": "2026-06-12", "c": "ACU"}
        )

        with (
            patch("service.bejerman_delivery.BejermanSDKClient") as client_cls,
            patch(
                "service.bejerman_delivery.fetch_or_enqueue_comprobante_pdf",
                return_value=(b"%PDF-1.4", "application/pdf"),
            ) as coordinator,
        ):
            get_facturacion_pdf(
                "ACU",
                document_id,
                actor_user_id=1152,
                company_key="MGBIO",
                remito_group_id="brg-rt-mgbio",
            )

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)
        self.assertIs(coordinator.call_args.args[0], client_cls.return_value)
        reference = coordinator.call_args.args[1]
        self.assertEqual(reference.type, "RT")
        self.assertEqual(reference.number, "00000012")
        self.assertEqual(reference.customer_code, "ACU")
        self.assertEqual(
            coordinator.call_args.kwargs,
            {"actor_user_id": 1152, "remito_group_id": "brg-rt-mgbio"},
        )

    def test_endpoint_pdf_remito_cobranzas_usa_cliente_y_empresa(self):
        document_id = build_document_id(
            {"t": "RT", "n": "00000683", "l": "R", "p": "00007", "f": "2026-07-14", "c": "ACU"}
        )
        request = type(
            "Request",
            (),
            {
                "query_params": {"customerCode": "ACU", "companyKey": "MGBIO"},
                "user": type("User", (), {"id": 1152})(),
            },
        )()
        with (
            patch("service.views.delivery_orders_views.resolve_remito_group_for_document_id", return_value=None),
            patch("service.views.delivery_orders_views.resolve_registered_ingreso_remito_for_document_id", return_value=None),
            patch(
                "service.views.delivery_orders_views.get_facturacion_pdf",
                return_value=(b"%PDF-1.7\nPDF-OFICIAL-BEJERMAN", "application/pdf"),
            ) as pdf_mock,
        ):
            response = CobranzasRemitoPdfView().get(request, document_id)

        pdf_mock.assert_called_once_with("ACU", document_id, interactive=True, actor_user_id=1152, company_key="MGBIO")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-1.7\nPDF-OFICIAL-BEJERMAN")
        self.assertIn(f"remito-{document_id}.pdf", response["Content-Disposition"])

    def test_endpoint_pdf_remito_cobranzas_prefiere_grupo_local(self):
        request = type(
            "Request",
            (),
            {
                "query_params": {"customerCode": "HAIR", "companyKey": "SEPID"},
                "user": type("User", (), {"id": 1152})(),
            },
        )()
        with (
            patch("service.views.delivery_orders_views.resolve_remito_group_for_document_id", return_value={"id": "brg-rta-local"}) as resolver,
            patch("service.views.delivery_orders_views.get_remito_group_pdf", return_value=(b"%PDF-1.4", "application/pdf", "remito-local.pdf")) as group_pdf,
            patch("service.views.delivery_orders_views.get_facturacion_pdf") as generic_pdf,
        ):
            response = CobranzasRemitoPdfView().get(request, "doc-remito")

        resolver.assert_called_once_with("doc-remito", company_key="SEPID")
        group_pdf.assert_called_once_with("brg-rta-local", actor_user_id=1152)
        generic_pdf.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("remito-local.pdf", response["Content-Disposition"])

    def test_endpoint_pdf_remito_cobranzas_devuelve_pending_interactivo(self):
        request = type(
            "Request",
            (),
            {
                "query_params": {"customerCode": "ACU", "companyKey": "SEPID"},
                "user": type("User", (), {"id": 1152})(),
            },
        )()
        with (
            patch("service.views.delivery_orders_views.resolve_remito_group_for_document_id", return_value=None),
            patch(
                "service.views.delivery_orders_views.get_facturacion_pdf",
                side_effect=BillingError(
                    "BEJERMAN_PDF_PENDING",
                    "Bejerman todavía está preparando el PDF.",
                    status_code=202,
                    retry_after_ms=5000,
                ),
            ),
        ):
            response = CobranzasRemitoPdfView().get(request, "doc-remito")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["code"], "BEJERMAN_PDF_PENDING")
        self.assertEqual(response.data["retry_after_ms"], 5000)
        self.assertEqual(response["Retry-After"], "5")

    def test_endpoint_print_remito_cobranzas_renderiza_espera_con_pdf_url(self):
        document_id = build_document_id(
            {"t": "RT", "n": "00004707", "l": "R", "p": "00004", "f": "2026-06-26", "c": "RES"}
        )
        request = type(
            "Request",
            (),
            {
                "query_params": {"customerCode": "RES", "companyKey": "SEPID"},
                "user": type("User", (), {"id": 1152})(),
            },
        )()

        response = CobranzasRemitoPrintView().get(request, document_id)
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn(f"/api/cobranzas/remitos/{document_id}/pdf/?customerCode=RES", html)
        self.assertIn("companyKey=SEPID", html)
        self.assertIn("Preparando el remito RT", html)
        # El nombre del documento dejó de ser una constante interpolada en el
        # script y ahora viaja en el objeto de configuración de la página.
        self.assertIn('"documentName": "remito RT"', html)
        self.assertNotIn(chr(0x00C3), html)

    def test_orden_con_remito_manual_expone_print_url_por_orden(self):
        order = serialize_order(
            {
                "id": "do-rss-manual",
                "order_number": "OS-29444",
                "customer_name": "RUSSO PABLO MATIAS",
                "delivery_type": "service_release",
                "status": "armado_pendiente_entrega",
                "remito_number": "RSS R 00004-00004715",
                "company_key": "SEPID",
            }
        )

        self.assertEqual(order["remitoPdfUrl"], "/api/ordenes-entrega/do-rss-manual/remito/pdf/")
        self.assertEqual(order["remitoPrintUrl"], "/api/ordenes-entrega/do-rss-manual/remito/print/")

    def test_resuelve_remito_manual_de_orden_por_numero_y_cliente(self):
        document_id = build_document_id(
            {"t": "RSS", "n": "00004715", "l": "R", "p": "00004", "f": "2026-06-29", "c": "RUSSO"}
        )
        order = {
            "id": "do-rss-manual",
            "remitoNumber": "RSS R 00004-00004715",
            "bejermanCustomerCode": "RUSSO",
            "companyKey": "SEPID",
            "deliveryType": "service_release",
        }

        with patch(
            "service.bejerman_delivery.list_remitos_from_bejerman",
            return_value={
                "items": [
                    {
                        "documentId": document_id,
                        "type": "RSS",
                        "letter": "R",
                        "pointOfSale": "00004",
                        "number": "00004715",
                        "documentNumber": "RSS R 00004-00004715",
                        "bejermanCustomerCode": "RUSSO",
                    }
                ],
                "effectiveCustomerCode": "RUSSO",
            },
        ) as remitos_mock:
            document = resolve_delivery_order_remito_document(order, actor_user_id=1152)

        self.assertEqual(document["documentId"], document_id)
        self.assertEqual(document["companyKey"], "SEPID")
        remitos_mock.assert_called_once()
        args, kwargs = remitos_mock.call_args
        self.assertEqual(args[0], "RUSSO")
        self.assertEqual(args[1]["remitoType"], "RSS")
        self.assertEqual(args[1]["search"], "00004715")
        self.assertEqual(kwargs["actor_user_id"], 1152)
        self.assertEqual(kwargs["company_key"], "SEPID")

    def test_resuelve_remito_manual_de_orden_con_busqueda_general_si_cliente_no_lo_devuelve(self):
        document_id = build_document_id(
            {"t": "RSS", "n": "00004715", "l": "R", "p": "00004", "f": "2026-06-29", "c": "RUSSO"}
        )
        order = {
            "id": "do-rss-manual",
            "remitoNumber": "RSS R 00004-00004715",
            "bejermanCustomerCode": "RUSSO",
            "companyKey": "SEPID",
            "deliveryType": "service_release",
        }

        with patch(
            "service.bejerman_delivery.list_remitos_from_bejerman",
            side_effect=[
                {"items": [], "effectiveCustomerCode": "RUSSO"},
                {
                    "items": [
                        {
                            "documentId": document_id,
                            "type": "RSS",
                            "letter": "R",
                            "pointOfSale": "00004",
                            "number": "00004715",
                            "documentNumber": "RSS R 00004-00004715",
                            "bejermanCustomerCode": "RUSSO",
                        }
                    ],
                    "effectiveCustomerCode": "",
                },
            ],
        ) as remitos_mock:
            document = resolve_delivery_order_remito_document(order, actor_user_id=1152)

        self.assertEqual(document["documentId"], document_id)
        self.assertEqual(document["effectiveCustomerCode"], "RUSSO")
        self.assertEqual(remitos_mock.call_count, 2)
        self.assertEqual(remitos_mock.call_args_list[0].args[0], "RUSSO")
        self.assertEqual(remitos_mock.call_args_list[1].args[0], "")
        self.assertEqual(remitos_mock.call_args_list[1].args[1]["search"], "00004715")

    def test_pdf_rt_historico_de_orden_entrega_exactamente_el_archivo_de_bejerman(self):
        document_id = build_document_id(
            {"t": "RT", "n": "00000683", "l": "R", "p": "00007", "f": "2026-07-14", "c": "JORGE"}
        )
        order = {
            "id": "do-rt-historico",
            "remitoNumber": "RT R 00007-00000683",
            "bejermanCustomerCode": "JORGE",
            "companyKey": "MGBIO",
        }
        official_bytes = b"%PDF-1.7\nPDF-OFICIAL-BEJERMAN"

        with (
            patch("service.bejerman_delivery.get_delivery_order", return_value=order),
            patch(
                "service.bejerman_delivery.resolve_delivery_order_remito_document",
                return_value={"documentId": document_id, "bejermanCustomerCode": "JORGE", "companyKey": "MGBIO"},
            ) as resolver,
            patch(
                "service.bejerman_delivery.get_facturacion_pdf",
                return_value=(official_bytes, "application/pdf"),
            ) as pdf_mock,
        ):
            pdf_bytes, content_type, filename = get_delivery_order_remito_pdf("do-rt-historico", actor_user_id=1152)

        self.assertEqual(pdf_bytes, official_bytes)
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(filename, "remito-RT-R-00007-00000683.pdf")
        resolver.assert_called_once_with(order, actor_user_id=1152)
        pdf_mock.assert_called_once_with(
            "JORGE",
            document_id,
            interactive=True,
            actor_user_id=1152,
            company_key="MGBIO",
        )

    def test_resuelve_factura_de_orden_por_punto_y_numero_y_empresa(self):
        document_id = build_document_id(
            {"t": "FC", "n": "00000012", "l": "A", "p": "0001", "f": "2026-06-18", "c": "ACU"}
        )
        order = {
            "id": "do-invoice",
            "invoiceNumber": "000100000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "MGBIO",
            "invoicedAt": "2026-06-18T12:00:00-03:00",
        }

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={
                "items": [
                    {
                        "documentId": document_id,
                        "documentNumber": "FC A 0001-00000012",
                        "type": "FC",
                        "letter": "A",
                        "pointOfSale": "0001",
                        "number": "00000012",
                        "bejermanCustomerCode": "ACU",
                    }
                ],
                "pagination": {"hasNextPage": False, "totalPages": 1},
                "effectiveCustomerCode": "ACU",
            },
        ) as list_mock:
            document = resolve_delivery_order_invoice_document(order, actor_user_id=1152)

        self.assertEqual(document["documentId"], document_id)
        self.assertEqual(document["companyKey"], "MGBIO")
        args, kwargs = list_mock.call_args
        self.assertEqual(args[0], "ACU")
        self.assertEqual(args[1]["origin"], "FC")
        self.assertNotIn("search", args[1])
        self.assertEqual(kwargs["actor_user_id"], 1152)
        self.assertEqual(kwargs["company_key"], "MGBIO")

    def test_resuelve_factura_de_orden_rechaza_datos_incompletos(self):
        with self.assertRaises(BillingError) as invoice_ctx:
            resolve_delivery_order_invoice_document({"bejermanCustomerCode": "ACU"})
        self.assertEqual(invoice_ctx.exception.code, "INVOICE_REQUIRED")

        with self.assertRaises(BillingError) as customer_ctx:
            resolve_delivery_order_invoice_document({"invoiceNumber": "FC A 0001-00000012"})
        self.assertEqual(customer_ctx.exception.code, "CUSTOMER_CODE_REQUIRED")

    def test_resuelve_factura_de_orden_rechaza_no_encontrada_y_ambigua(self):
        base_order = {
            "id": "do-invoice",
            "invoiceNumber": "FC A 0001-00000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "SEPID",
        }

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={"items": [], "pagination": {"hasNextPage": False, "totalPages": 1}},
        ):
            with self.assertRaises(BillingError) as not_found_ctx:
                resolve_delivery_order_invoice_document(base_order)
        self.assertEqual(not_found_ctx.exception.code, "INVOICE_DOCUMENT_NOT_FOUND")
        self.assertEqual(not_found_ctx.exception.status_code, 404)

        with patch(
            "service.bejerman_delivery.list_facturacion_from_bejerman",
            return_value={
                "items": [
                    {"documentId": "doc-1", "documentNumber": "FC A 0001-00000012", "pointOfSale": "0001", "number": "00000012"},
                    {"documentId": "doc-2", "documentNumber": "FC A 0001-00000012", "pointOfSale": "0001", "number": "00000012"},
                ],
                "pagination": {"hasNextPage": False, "totalPages": 1},
            },
        ):
            with self.assertRaises(BillingError) as ambiguous_ctx:
                resolve_delivery_order_invoice_document(base_order)
        self.assertEqual(ambiguous_ctx.exception.code, "INVOICE_DOCUMENT_AMBIGUOUS")
        self.assertEqual(ambiguous_ctx.exception.status_code, 409)

    def test_pdf_factura_de_orden_usa_empresa_de_la_orden(self):
        document_id = build_document_id(
            {"t": "FC", "n": "00000012", "l": "A", "p": "0007", "f": "2026-06-18", "c": "ACU"}
        )
        order = {
            "id": "do-invoice",
            "invoiceNumber": "FC A 0007-00000012",
            "bejermanCustomerCode": "ACU",
            "companyKey": "MGBIO",
        }

        with (
            patch("service.bejerman_delivery.get_delivery_order", return_value=order),
            patch(
                "service.bejerman_delivery.resolve_delivery_order_invoice_document",
                return_value={"documentId": document_id, "bejermanCustomerCode": "ACU", "companyKey": "MGBIO"},
            ),
            patch("service.bejerman_delivery.get_facturacion_pdf", return_value=(b"%PDF-1.4", "application/pdf")) as pdf_mock,
        ):
            pdf_bytes, content_type, filename = get_delivery_order_invoice_pdf("do-invoice", actor_user_id=1152)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(filename, "factura-FC-A-0007-00000012.pdf")
        pdf_mock.assert_called_once_with(
            "ACU",
            document_id,
            interactive=True,
            actor_user_id=1152,
            company_key="MGBIO",
        )


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class BejermanDeliveryRemitosFallbackTests(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.cursor() as cur:
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
        super().setUpClass()

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM bejerman_remito_groups")

    def _insert_remito_group(
        self,
        group_id: str,
        *,
        remito_type: str = "RSS",
        customer_code: str = "TMD",
        customer_name: str = "TMD S.A.",
        number: str = "00004500",
        operation_code: str = "REP",
        issue_date: str = "2026-06-02",
        total: int | float = 0,
        company_key: str = "SEPID",
        point_of_sale: str = "00004",
    ):
        raw = {
            "Comprobante_Tipo": remito_type,
            "Comprobante_Letra": "R",
            "Comprobante_PtoVenta": point_of_sale,
            "Comprobante_Numero": number,
            "Comprobante_FechaEmision": issue_date,
            "Cliente_Codigo": customer_code,
            "Cliente_RazonSocial": customer_name,
            "Comprobante_TipoOperacion": operation_code,
            "Comprobante_ImporteTotal": total,
        }
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_remito_groups(
                    id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                    comprobante_numero, remito_number, customer_code, customer_name, seller_code,
                    payment_term_code, operation_code, deposit_code, order_ids, status, generated_at, response_summary
                )
                VALUES (%s, %s, %s, 'R', %s, %s, %s, %s, %s, 'ADM', '30', %s, 'STC',
                        '[]'::jsonb, 'generated', %s::timestamptz, %s::jsonb)
                """,
                [
                    group_id,
                    company_key,
                    remito_type,
                    point_of_sale,
                    number,
                    f"{remito_type} R {point_of_sale}-{number}",
                    customer_code,
                    customer_name,
                    operation_code,
                    f"{issue_date} 09:00:00-03",
                    json.dumps(
                        {
                            "comprobanteTipo": remito_type,
                            "comprobanteLetra": "R",
                            "comprobantePtoVenta": point_of_sale,
                            "comprobanteNumero": number,
                            "remitoNumber": f"{remito_type} R {point_of_sale}-{number}",
                            "companyKey": company_key,
                            "raw": raw,
                        }
                    ),
                ],
            )

    def test_remitos_rss_fallback_local_si_sdk_falla(self):
        self._insert_remito_group("brg-rss-local")
        client = FakeSDKClient(
            customer_records=[{"Cliente_Codigo": "TMD", "Cliente_RazonSocial": "TMD S.A."}],
            fail_sales_types={"RSS"},
        )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "TMD",
                {"remitoType": "RSS", "dateFrom": "2026-06-01", "dateTo": "2026-06-30", "pageSize": "10"},
                actor_user_id=1152,
                company_key="SEPID",
            )

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RSS R 00004-00004500")
        self.assertEqual(result["items"][0]["remitoGroupId"], "brg-rss-local")
        self.assertEqual(result["items"][0]["pdfUrl"], "/api/ordenes-entrega/remito-bejerman/brg-rss-local/pdf/")
        self.assertEqual(result["partialErrors"][0]["remitoType"], "RSS")
        self.assertIn("RSS generados por NEXORA", result["warning"])

    def test_remitos_rt_fallback_local_si_sdk_falla(self):
        self._insert_remito_group(
            "brg-rt-home-air",
            remito_type="RT",
            customer_code="HAIR",
            customer_name="HOME AIR S.R.L.",
            number="00000665",
            operation_code="MC",
            issue_date="2026-07-02",
            company_key="MGBIO",
            point_of_sale="00007",
        )
        client = FakeSDKClient(
            customer_records=[{"Cliente_Codigo": "HAIR", "Cliente_RazonSocial": "HOME AIR S.R.L."}],
            fail_sales_types={"RT"},
        )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "HAIR",
                {"remitoType": "RT", "dateFrom": "2026-07-01", "dateTo": "2026-07-03", "pageSize": "10"},
                actor_user_id=1152,
                company_key="MGBIO",
            )

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RT R 00007-00000665")
        self.assertEqual(result["items"][0]["remitoGroupId"], "brg-rt-home-air")
        self.assertEqual(result["items"][0]["companyKey"], "MGBIO")
        self.assertEqual(result["partialErrors"][0]["remitoType"], "RT")

    def test_recuperacion_sql_remota_usa_base_por_empresa(self):
        self.assertEqual(_bejerman_sql_database_for_company("SEPID"), "SBDASEP")
        self.assertEqual(_bejerman_sql_database_for_company("MGBIO"), "SBDAMGBI")

    def test_recuperacion_sql_colapsa_renglones_de_remito_multi_orden(self):
        bridge_request = {
            "orders": [
                {
                    "orderNumber": "OS-29342",
                    "items": [{"articleCode": "1118002", "partida": "250100076"}],
                },
                {
                    "orderNumber": "OS-29343",
                    "items": [{"articleCode": "1118002", "partida": "2006120012"}],
                },
            ]
        }
        rows = [
            {
                "Comprobante_ID": "17970",
                "Comprobante_Tipo": "RSS",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00004743",
                "Recovery_Text": "NEXORA brg-old ASPIRADOR A BATERÍAS - OS-29342 1118002 250100076",
                "Item_DescripArticulo": "ASPIRADOR A BATERÍAS | Yuwell | 7E-G - OS-29342",
                "Item_CodigoArticulo": "1118002",
                "Item_Partida": "250100076",
            },
            {
                "Comprobante_ID": "17970",
                "Comprobante_Tipo": "RSS",
                "Comprobante_Letra": "R",
                "Comprobante_PtoVenta": "00004",
                "Comprobante_Numero": "00004743",
                "Recovery_Text": "NEXORA brg-old ASPIRADOR A BATERÍAS - OS-29343 1118002 2006120012",
                "Item_DescripArticulo": "ASPIRADOR A BATERÍAS | Yuwell | 7E-G - OS-29343",
                "Item_CodigoArticulo": "1118002",
                "Item_Partida": "2006120012",
            },
        ]

        collapsed = _collapse_recovery_candidate_rows(rows)

        self.assertEqual(len(collapsed), 1)
        self.assertTrue(_recovery_record_matches_delivery_request(collapsed[0], bridge_request, "brg-new"))

    def test_recuperacion_sql_acepta_group_id_truncado_en_mensaje_bejerman(self):
        group_id = "brg-aa00173b-8520-4625-abda-3d12ca58c89d"
        record = {
            "Comprobante_Tipo": "RSS",
            "Comprobante_Numero": "00004747",
            "Comprobante_Mensaje": "NEXORA brg-aa00173b-8520-4625-abda-3d12c",
            "Recovery_Text": "NEXORA brg-aa00173b-8520-4625-abda-3d12c 1111003 D6#1672",
        }
        bridge_request = {"orders": [{"orderNumber": "OS-29434 + 17 OS", "items": []}]}

        self.assertTrue(_recovery_record_matches_delivery_request(record, bridge_request, group_id))

    def test_sello_del_grupo_separa_los_duplicados_de_reintentos(self):
        """El caso MARK: dos RSS reales por la misma OS, uno por cada reintento.

        Los dos comprobantes comparten cliente, orden, artículo y partida, así que el
        matching por texto los daba como candidatos de los tres grupos a la vez y la
        reconciliación terminaba siempre "ambigua". El sello dice cuál es de quién.
        """
        propio = "brg-b8420d57-0d3a-4f0c-ba62-dcad259fc19e"
        ajeno = "brg-2662a0d5-9906-4f73-a592-998f0c779acd"
        bridge_request = {
            "orders": [
                {
                    "orderNumber": "OS-29399",
                    "items": [{"articleCode": "1110001", "partida": "S219D401065"}],
                }
            ]
        }
        record_ajeno = {
            "Comprobante_Tipo": "RSS",
            "Comprobante_Numero": "00004741",
            "Comprobante_Mensaje": "NEXORA brg-2662a0d5-9906-4f73-a592-998f0",
            "Recovery_Text": (
                "NEXORA brg-2662a0d5-9906-4f73-a592-998f0 POLÍGRAFO - OS-29399 1110001 S219D401065"
            ),
        }

        # Sin mirar el sello, el comprobante del otro grupo pasa el filtro de texto.
        self.assertTrue(_recovery_record_matches_delivery_request(record_ajeno, bridge_request, propio))

        stamps = _recovery_record_group_stamps(record_ajeno)
        self.assertTrue(stamps)
        self.assertFalse(any(_recovery_stamp_belongs_to_group(stamp, propio) for stamp in stamps))
        self.assertTrue(any(_recovery_stamp_belongs_to_group(stamp, ajeno) for stamp in stamps))

    def test_sello_truncado_por_bejerman_sigue_reconociendo_al_grupo_propio(self):
        group_id = "brg-7842fbfe-3507-4e95-a7a7-ddd9352ddb47"
        record = {
            "Comprobante_Numero": "00004742",
            "Comprobante_Mensaje": "NEXORA brg-7842fbfe-3507-4e95-a7a7-ddd93",
        }
        stamps = _recovery_record_group_stamps(record)

        self.assertTrue(any(_recovery_stamp_belongs_to_group(stamp, group_id) for stamp in stamps))

    def test_comprobante_sin_sello_no_queda_excluido(self):
        """Los comprobantes cargados a mano en Bejerman no llevan sello.

        Excluirlos rompería la recuperación de todo lo que no emitió NEXORA.
        """
        record = {"Comprobante_Numero": "00004500", "Comprobante_Mensaje": "Remito manual"}

        self.assertEqual(_recovery_record_group_stamps(record), set())

    def test_remito_group_pdf_registrado_informa_que_no_es_imprimible(self):
        self._insert_remito_group("brg-registered-local", remito_type="RIS", number="00026249")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET response_summary = response_summary || %s::jsonb
                 WHERE id = %s
                """,
                [
                    json.dumps(
                        {
                            "documentMode": "register",
                            "manualRemitoNumber": "RIS R 00004-00026249",
                        }
                    ),
                    "brg-registered-local",
                ],
            )

        with patch("service.bejerman_delivery.get_facturacion_pdf") as pdf_mock:
            with self.assertRaises(BillingError) as ctx:
                get_remito_group_pdf("brg-registered-local", actor_user_id=1152)

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REGISTERED_NO_PDF")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("registrado manualmente", str(ctx.exception))
        pdf_mock.assert_not_called()

    def test_rt_de_sepid_y_mgbio_entrega_exactamente_el_pdf_oficial_de_bejerman(self):
        official_bytes = b"%PDF-1.7\nPDF-OFICIAL-BEJERMAN"
        for company_key, point in (("SEPID", "00004"), ("MGBIO", "00007")):
            with self.subTest(company_key=company_key):
                group_id = f"brg-rt-official-{company_key.lower()}"
                self._insert_remito_group(
                    group_id,
                    remito_type="RT",
                    customer_code="EDU",
                    customer_name="ANDREOLI EDUARDO GABRIEL",
                    number="00000007",
                    operation_code="MC",
                    total=23858.78,
                    company_key=company_key,
                    point_of_sale=point,
                )
                with patch(
                    "service.bejerman_delivery.get_facturacion_pdf",
                    return_value=(official_bytes, "application/pdf"),
                ) as official_pdf:
                    pdf_bytes, content_type, filename = get_remito_group_pdf(group_id, actor_user_id=1152)

                official_pdf.assert_called_once()
                self.assertEqual(official_pdf.call_args.args[0], "EDU")
                self.assertEqual(
                    official_pdf.call_args.kwargs,
                    {
                        "interactive": True,
                        "actor_user_id": 1152,
                        "company_key": company_key,
                        "remito_group_id": group_id,
                    },
                )
                self.assertEqual(content_type, "application/pdf")
                self.assertEqual(pdf_bytes, official_bytes)
                self.assertIn("remito-rt", filename.lower())

    def test_remito_group_pdf_ocasional_usa_codigo_efectivo_de_la_respuesta(self):
        group_id = "brg-rss-occasional-raw"
        self._insert_remito_group(group_id, customer_code="", customer_name="lautaro", number="00004358")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET response_summary = jsonb_set(
                         response_summary,
                         '{raw,Cliente_Codigo}',
                         to_jsonb(%s::text),
                         true
                       )
                 WHERE id = %s
                """,
                ["     1", group_id],
            )

        with (
            patch("service.bejerman_delivery._remito_pdf_request_customer_code", return_value="2") as queue_code,
            patch(
                "service.bejerman_delivery.get_facturacion_pdf",
                return_value=(b"%PDF-1.4", "application/pdf"),
            ) as pdf_mock,
        ):
            get_remito_group_pdf(group_id, actor_user_id=1152)

        queue_code.assert_not_called()
        self.assertEqual(pdf_mock.call_args.args[0], "1")
        self.assertEqual(decode_document_id(pdf_mock.call_args.args[1])["c"], "1")

    def test_remito_group_pdf_ocasional_recupera_codigo_de_la_cola(self):
        group_id = "brg-rss-occasional-queue"
        self._insert_remito_group(group_id, customer_code="", customer_name="lautaro", number="00004359")

        with (
            patch("service.bejerman_delivery._remito_pdf_request_customer_code", return_value="1") as queue_code,
            patch(
                "service.bejerman_delivery.get_facturacion_pdf",
                return_value=(b"%PDF-1.4", "application/pdf"),
            ) as pdf_mock,
        ):
            get_remito_group_pdf(group_id, actor_user_id=1152)

        queue_code.assert_called_once_with(group_id)
        self.assertEqual(pdf_mock.call_args.args[0], "1")
        self.assertEqual(decode_document_id(pdf_mock.call_args.args[1])["c"], "1")

    def test_remito_group_pdf_permanece_disponible_en_revision_con_numero(self):
        self._insert_remito_group("brg-rss-review-with-number", remito_type="RSS", number="00004741")
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET status = 'failed_review',
                       response_summary = response_summary || %s::jsonb
                 WHERE id = %s
                """,
                [
                    json.dumps(
                        {
                            "error": "Bejerman requiere revision manual por posible duplicado.",
                            "duplicateStockMovementDetected": True,
                        }
                    ),
                    "brg-rss-review-with-number",
                ],
            )

        with patch(
            "service.bejerman_delivery.get_facturacion_pdf",
            return_value=(b"%PDF-1.4", "application/pdf"),
        ) as pdf_mock:
            pdf_bytes, content_type, filename = get_remito_group_pdf("brg-rss-review-with-number", actor_user_id=1152)

        pdf_mock.assert_called_once()
        self.assertEqual(pdf_bytes, b"%PDF-1.4")
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("00004741", filename)

    def test_remito_group_pdf_no_inventa_copia_si_bejerman_falla(self):
        self._insert_remito_group("brg-rss-pdf-fallback", remito_type="RSS", number="00004743")
        error = BillingError(
            "BEJERMAN_BILLING_QUERY_BUSY",
            "Bejerman no pudo completar la consulta porque dejó bloqueado expSinCuotasV1.",
            status_code=502,
        )

        with patch(
            "service.bejerman_delivery.get_facturacion_pdf",
            side_effect=error,
        ) as pdf_mock:
            with self.assertRaises(BillingError) as ctx:
                get_remito_group_pdf("brg-rss-pdf-fallback", actor_user_id=1152)

        pdf_mock.assert_called_once()
        self.assertIs(ctx.exception, error)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_remito_group_pdf_antiguo_sigue_pendiente_hasta_recibir_el_pdf_real(self):
        self._insert_remito_group("brg-rss-pdf-pending-old", remito_type="RSS", number="00004743")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_remito_groups SET generated_at = CURRENT_TIMESTAMP - INTERVAL '5 minutes' WHERE id = %s",
                ["brg-rss-pdf-pending-old"],
            )

        with patch(
            "service.bejerman_delivery.get_facturacion_pdf",
            side_effect=BillingError(
                "BEJERMAN_PDF_PENDING",
                "PDF pendiente de generación en Bejerman",
                status_code=202,
                retry_after_ms=1200,
            ),
        ) as pdf_mock:
            with self.assertRaises(BillingError) as ctx:
                get_remito_group_pdf("brg-rss-pdf-pending-old", actor_user_id=1152)

        pdf_mock.assert_called_once()
        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_PENDING")
        self.assertEqual(ctx.exception.status_code, 202)

    def test_remito_group_pdf_pendiente_reciente_no_devuelve_copia_operativa(self):
        self._insert_remito_group("brg-rss-pdf-pending-recent", remito_type="RSS", number="00004744")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE bejerman_remito_groups SET generated_at = CURRENT_TIMESTAMP WHERE id = %s",
                ["brg-rss-pdf-pending-recent"],
            )

        with patch(
            "service.bejerman_delivery.get_facturacion_pdf",
            side_effect=BillingError(
                "BEJERMAN_PDF_PENDING",
                "PDF pendiente de generación en Bejerman",
                status_code=202,
                retry_after_ms=1200,
            ),
        ):
            with self.assertRaises(BillingError) as ctx:
                get_remito_group_pdf("brg-rss-pdf-pending-recent", actor_user_id=1152)

        self.assertEqual(ctx.exception.code, "BEJERMAN_PDF_PENDING")
        self.assertEqual(ctx.exception.status_code, 202)

    def test_remitos_rss_fallback_local_respeta_filtros(self):
        self._insert_remito_group("brg-rss-match", customer_code="TMD", number="00004500", operation_code="REP", issue_date="2026-06-02")
        self._insert_remito_group("brg-rss-other-customer", customer_code="ACU", number="00004501", operation_code="REP", issue_date="2026-06-02")
        self._insert_remito_group("brg-rss-other-operation", customer_code="TMD", number="00004502", operation_code="MC", issue_date="2026-06-02")
        client = FakeSDKClient(
            customer_records=[{"Cliente_Codigo": "TMD", "Cliente_RazonSocial": "TMD S.A."}],
            fail_sales_types={"RSS"},
        )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "TMD",
                {
                    "remitoType": "RSS",
                    "operationType": "REP",
                    "dateFrom": "2026-06-01",
                    "dateTo": "2026-06-03",
                    "search": "4500",
                    "pageSize": "10",
                },
                company_key="SEPID",
            )

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["remitoGroupId"], "brg-rss-match")

    def test_remitos_sdk_se_enriquece_con_pdf_local(self):
        self._insert_remito_group(
            "brg-rta-local",
            remito_type="RTA",
            customer_code="HAIR",
            customer_name="HOME AIR S.R.L.",
            number="00004703",
            operation_code="ALQ",
            issue_date="2026-06-26",
        )
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RTA",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004703",
                    "Comprobante_FechaEmision": "2026-06-26",
                    "Cliente_Codigo": "HAIR",
                    "Cliente_RazonSocial": "HOME AIR S.R.L.",
                    "Comprobante_TipoOperacion": "ALQ",
                    "Comprobante_ImporteTotal": 0,
                }
            ]
        )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "",
                {"remitoType": "RTA", "dateFrom": "2026-06-01", "dateTo": "2026-06-30", "pageSize": "10"},
                company_key="SEPID",
            )

        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["items"][0]["documentNumber"], "RTA R 00004-00004703")
        self.assertEqual(result["items"][0]["remitoGroupId"], "brg-rta-local")
        self.assertEqual(result["items"][0]["pdfUrl"], "/api/ordenes-entrega/remito-bejerman/brg-rta-local/pdf/")
        self.assertEqual(result["items"][0]["printUrl"], "/api/ordenes-entrega/remito-bejerman/brg-rta-local/print/")

    def test_remitos_sdk_incluye_urls_pdf_y_print_de_cobranzas(self):
        client = FakeSDKClient(
            sales_records=[
                {
                    "Comprobante_Tipo": "RT",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004707",
                    "Comprobante_FechaEmision": "2026-06-26",
                    "Cliente_Codigo": "RES",
                    "Cliente_RazonSocial": "RESPIRAR S.A.",
                    "Comprobante_TipoOperacion": "REP",
                    "Comprobante_ImporteTotal": 0,
                }
            ]
        )

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_remitos_from_bejerman(
                "",
                {"remitoType": "RT", "dateFrom": "2026-06-01", "dateTo": "2026-06-30", "pageSize": "10"},
                company_key="SEPID",
            )

        item = result["items"][0]
        self.assertIn("/api/cobranzas/remitos/", item["pdfUrl"])
        self.assertTrue(item["pdfUrl"].endswith("/pdf/?customerCode=RES&companyKey=SEPID"))
        self.assertTrue(item["printUrl"].endswith("/print/?customerCode=RES&companyKey=SEPID"))
        self.assertNotIn("/ordenes-entrega/remito-bejerman/", item["printUrl"])

    def test_mark_group_failed_categoriza_error_y_guarda_diagnostico(self):
        self._insert_remito_group("brg-failed-diagnostico")

        _mark_group_failed(
            "brg-failed-diagnostico", [], None, BejermanSdkResponseError("BEJERMAN_CUSTOMER_NOT_FOUND")
        )

        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, last_error, response_summary FROM bejerman_remito_groups WHERE id = %s",
                ["brg-failed-diagnostico"],
            )
            status, last_error, response_summary = cur.fetchone()

        self.assertEqual(status, "failed")
        self.assertTrue(last_error)
        summary = response_summary if isinstance(response_summary, dict) else json.loads(response_summary)
        self.assertEqual(summary["errorCategory"], "BEJERMAN_CUSTOMER_RESOLUTION_FAILED")

    def test_mark_group_failed_usa_mensaje_generico_si_la_excepcion_no_trae_texto(self):
        self._insert_remito_group("brg-failed-sin-texto")

        class _ExcepcionSinTexto(Exception):
            def __str__(self):
                return ""

        _mark_group_failed("brg-failed-sin-texto", [], None, _ExcepcionSinTexto())

        with connection.cursor() as cur:
            cur.execute("SELECT last_error FROM bejerman_remito_groups WHERE id = %s", ["brg-failed-sin-texto"])
            (last_error,) = cur.fetchone()

        self.assertTrue(last_error)

    def test_mark_group_review_required_categoriza_partida_inexistente(self):
        self._insert_remito_group("brg-review-partida")

        exc = BejermanSdkResponseError(
            "El comprobante no se importó debido a que el ítem 1103001 tiene asignada una "
            "Partida inexistente y/o no informada."
        )
        _mark_group_review_required("brg-review-partida", [], None, exc)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, response_summary FROM bejerman_remito_groups WHERE id = %s",
                ["brg-review-partida"],
            )
            status, response_summary = cur.fetchone()

        self.assertEqual(status, "failed_review")
        summary = response_summary if isinstance(response_summary, dict) else json.loads(response_summary)
        self.assertEqual(summary["errorCategory"], "BEJERMAN_PARTIDA_INEXISTENTE")

    def test_mark_group_sent_unknown_categoriza_bloqueo_transitorio_de_tabla(self):
        self._insert_remito_group("brg-sent-unknown-tabla")

        exc = BejermanSdkResponseError("El comprobante no se importó. No ha sido posible actualizar los datos en la tabla.")
        _mark_group_sent_unknown("brg-sent-unknown-tabla", [], None, exc)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, response_summary FROM bejerman_remito_groups WHERE id = %s",
                ["brg-sent-unknown-tabla"],
            )
            status, response_summary = cur.fetchone()

        self.assertEqual(status, "sent_unknown")
        summary = response_summary if isinstance(response_summary, dict) else json.loads(response_summary)
        self.assertEqual(summary["errorCategory"], "BEJERMAN_TABLE_UPDATE_TRANSIENT")


class BejermanReviewResolutionGuardTests(SimpleTestCase):
    """La resolución manual pisa lo que Bejerman no confirmó: exige decisión, confirmación y motivo."""

    def test_exige_una_decision_valida(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            resolve_bejerman_remito_review("brg-x", 1, {"confirm": True, "reason": "miré"})
        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REVIEW_DECISION_REQUIRED")

    def test_exige_confirmacion_explicita(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            resolve_bejerman_remito_review("brg-x", 1, {"decision": "adopt", "reason": "miré"})
        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REVIEW_CONFIRMATION_REQUIRED")

    def test_exige_dejar_asentado_que_se_verifico(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            resolve_bejerman_remito_review("brg-x", 1, {"decision": "not_emitted", "confirm": True})
        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_REVIEW_REASON_REQUIRED")

    def test_el_mensaje_nombra_las_tres_salidas_posibles(self):
        """El operador tiene que poder leer qué se espera de él, no adivinarlo."""
        with self.assertRaises(DeliveryOrderError) as ctx:
            resolve_bejerman_remito_review("brg-x", 1, {"confirm": True, "reason": "miré"})
        detalle = str(ctx.exception)
        self.assertIn("adopt", detalle)
        self.assertIn("not_emitted", detalle)
        self.assertIn("duplicate", detalle)


class BejermanCustomerMasterDataErrorTests(SimpleTestCase):
    def test_falta_de_situacion_iva_no_se_presenta_como_demora_reintentable(self):
        """El caso OXL: diez intentos en dos días contra un dato que faltaba en la ficha."""
        error = _sdk_error(BejermanSdkResponseError("BEJERMAN_CUSTOMER_VAT_MISSING"))

        self.assertEqual(error.code, "BEJERMAN_CUSTOMER_MASTER_DATA_INCOMPLETE")
        self.assertEqual(error.status_code, 409)
        self.assertIn("situación de IVA", str(error))
        self.assertIn("Reintentar desde NEXORA no lo resuelve", str(error))

    def test_cliente_no_encontrado_sigue_siendo_reintentable(self):
        error = _sdk_error(BejermanSdkResponseError("BEJERMAN_CUSTOMER_NOT_FOUND"))

        self.assertEqual(error.code, "BEJERMAN_CUSTOMER_RESOLUTION_FAILED")
        self.assertIn("reintente", str(error).lower())


class BejermanBlockingStockSyncJobLookupTests(TestCase):
    def setUp(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social, telefono, email) VALUES (%s,%s,%s,%s) RETURNING id",
                ["CLI-SYNC", "Cliente Sync", "123", "sync@bejerman.test"],
            )
            self.customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO locations(nombre) VALUES (%s) RETURNING id", ["Taller Sync"])
            self.taller_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed Sync"])
            self.marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [self.marca_id, "AirSense Sync", "CPAP", ""],
            )
            self.model_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, alquilado, mg_estado)
                VALUES (%s,%s,%s,%s,FALSE,'activo')
                RETURNING id
                """,
                [self.customer_id, self.marca_id, self.model_id, "SN-SYNC-001"],
            )
            self.device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_creacion, ubicacion_id, empresa_bejerman, empresa_facturar)
                VALUES (%s,'liberado','reparación',CURRENT_TIMESTAMP,%s,'SEPID','SEPID')
                RETURNING id
                """,
                [self.device_id, self.taller_id],
            )
            self.ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO delivery_orders(id, order_number, delivery_type) VALUES (%s,%s,'service_release')",
                ["do-sync-lookup-test", "OS-SYNC-TEST"],
            )
            cur.execute(
                "INSERT INTO delivery_order_items(id, order_id, ingreso_id) VALUES (%s,%s,%s)",
                ["doi-sync-lookup-test", "do-sync-lookup-test", self.ingreso_id],
            )

    def _insert_sync_job(
        self,
        *,
        status: str,
        sync_type: str = "stock_str_to_stc",
        target_deposit: str = "STC",
        ingreso_id: int | None = None,
        device_id: int | None = None,
        serial: str = "SN-SYNC-001",
    ):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_sync_jobs(sync_type, ingreso_id, device_id, numero_serie, source_deposit, target_deposit, status, last_error)
                VALUES (%s,%s,%s,%s,'STR',%s,%s,%s)
                RETURNING id
                """,
                [
                    sync_type,
                    ingreso_id or self.ingreso_id,
                    device_id or self.device_id,
                    serial,
                    target_deposit,
                    status,
                    "El comprobante no se importó. No ha sido posible actualizar los datos en la tabla.",
                ],
            )
            return int(cur.fetchone()[0])

    def _insert_linked_ingreso(self, index: int) -> tuple[int, int, str]:
        serial = f"SN-SYNC-EXTRA-{index:02d}"
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, alquilado, mg_estado)
                VALUES (%s,%s,%s,%s,FALSE,'activo')
                RETURNING id
                """,
                [self.customer_id, self.marca_id, self.model_id, serial],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(device_id, estado, motivo, fecha_creacion, ubicacion_id, empresa_bejerman, empresa_facturar)
                VALUES (%s,'liberado','reparación',CURRENT_TIMESTAMP,%s,'SEPID','SEPID')
                RETURNING id
                """,
                [device_id, self.taller_id],
            )
            ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO delivery_order_items(id, order_id, ingreso_id)
                VALUES (%s,%s,%s)
                """,
                [
                    f"doi-sync-lookup-extra-{index}",
                    "do-sync-lookup-test",
                    ingreso_id,
                ],
            )
        return ingreso_id, device_id, serial

    def test_encuentra_job_bloqueado_para_la_orden(self):
        job_id = self._insert_sync_job(status="blocked")

        result = _find_blocking_stock_sync_jobs(["do-sync-lookup-test"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["jobId"], job_id)
        self.assertEqual(result[0]["status"], "blocked")
        self.assertEqual(result[0]["targetDeposit"], "STC")

    def test_no_devuelve_jobs_exitosos(self):
        self._insert_sync_job(status="succeeded")

        result = _find_blocking_stock_sync_jobs(["do-sync-lookup-test"])

        self.assertEqual(result, [])

    def test_orden_sin_ingreso_vinculado_no_rompe(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO delivery_orders(id, order_number, delivery_type) VALUES (%s,%s,'sale')",
                ["do-sync-lookup-sin-ingreso", "OS-SYNC-SIN-INGRESO"],
            )

        result = _find_blocking_stock_sync_jobs(["do-sync-lookup-sin-ingreso"])

        self.assertEqual(result, [])

    def test_lista_vacia_de_ordenes_no_consulta_nada(self):
        self.assertEqual(_find_blocking_stock_sync_jobs([]), [])

    @staticmethod
    def _order_for_preflight() -> dict:
        return {
            "id": "do-sync-lookup-test",
            "deliveryType": "service_release",
            "companyKey": "SEPID",
        }

    def test_generate_reabre_job_bloqueado_y_continua_si_se_resuelve(self):
        job_id = self._insert_sync_job(status="blocked")
        order = self._order_for_preflight()

        def mark_succeeded(**_kwargs):
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_sync_jobs
                       SET status='succeeded',
                           last_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE id=%s
                    """,
                    [job_id],
                )
            return {"processed": 1, "succeeded": 1, "failed": 0, "blocked": 0}

        with (
            patch("service.bejerman_delivery.get_delivery_order", return_value=order),
            patch("service.bejerman_delivery._refresh_service_release_orders_for_remito", return_value=[order]),
            patch("service.bejerman_delivery.refresh_delivery_order_article_partida_flags", return_value=order),
            patch("service.bejerman_delivery._same_uncertain_remito_group_for_orders", return_value=None),
            patch("service.bejerman_delivery.process_bejerman_jobs", side_effect=mark_succeeded) as process_mock,
            patch(
                "service.bejerman_delivery._validate_remito_orders",
                side_effect=RuntimeError("continuó con la generación"),
            ),
            patch("service.bejerman_delivery.BejermanSDKClient") as remito_sdk_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "continuó con la generación"):
                generate_bejerman_remito(
                    ["do-sync-lookup-test"],
                    actor_user_id=1152,
                    _skip_print_split=True,
                )

        process_mock.assert_called_once_with(
            limit=1,
            job_id=job_id,
            actor_user_id=1152,
            reopen_blocked=True,
        )
        remito_sdk_mock.assert_not_called()

    def test_generate_bloquea_antes_del_sdk_si_el_job_sigue_pendiente(self):
        job_id = self._insert_sync_job(status="blocked")
        order = self._order_for_preflight()

        with (
            patch(
                "service.bejerman_delivery.get_delivery_order",
                return_value=order,
            ) as get_order_mock,
            patch("service.bejerman_delivery._refresh_service_release_orders_for_remito", return_value=[order]),
            patch("service.bejerman_delivery.refresh_delivery_order_article_partida_flags", return_value=order),
            patch("service.bejerman_delivery._same_uncertain_remito_group_for_orders", return_value=None),
            patch(
                "service.bejerman_delivery.process_bejerman_jobs",
                return_value={"processed": 1, "succeeded": 0, "failed": 1, "blocked": 0},
            ),
            patch("service.bejerman_delivery._validate_remito_orders") as validate_mock,
            patch("service.bejerman_delivery.BejermanSDKClient") as remito_sdk_mock,
        ):
            with self.assertRaises(BillingError) as ctx:
                generate_bejerman_remito(
                    ["do-sync-lookup-test"],
                    actor_user_id=1152,
                    _skip_print_split=True,
                )

        self.assertEqual(ctx.exception.code, "BEJERMAN_REMITO_BLOCKED_BY_SYNC")
        self.assertEqual(ctx.exception.payload["blockingSyncJobs"][0]["jobId"], job_id)
        self.assertIn("Reintentá en unos minutos", str(ctx.exception))
        get_order_mock.assert_not_called()
        validate_mock.assert_not_called()
        remito_sdk_mock.assert_not_called()

    def test_preflight_sin_jobs_no_hace_nada(self):
        with patch("service.bejerman_delivery.process_bejerman_jobs") as process_mock:
            _ensure_blocking_stock_sync_jobs_resolved(
                ["do-sync-lookup-test"],
                actor_user_id=1152,
            )

        process_mock.assert_not_called()

    def test_preflight_ignora_jobs_legacy_bloqueados_intencionalmente(self):
        self._insert_sync_job(
            status="blocked",
            sync_type="stock_exit_rts",
            target_deposit="SALIDA",
        )

        with patch("service.bejerman_delivery.process_bejerman_jobs") as process_mock:
            _ensure_blocking_stock_sync_jobs_resolved(
                ["do-sync-lookup-test"],
                actor_user_id=1152,
            )

        process_mock.assert_not_called()

    def test_preflight_intenta_todos_los_jobs_sin_limite_de_cinco(self):
        job_ids = [self._insert_sync_job(status="blocked")]
        for index in range(1, 6):
            ingreso_id, device_id, serial = self._insert_linked_ingreso(index)
            job_ids.append(
                self._insert_sync_job(
                    status="blocked",
                    ingreso_id=ingreso_id,
                    device_id=device_id,
                    serial=serial,
                )
            )

        def mark_succeeded(**kwargs):
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bejerman_sync_jobs
                       SET status='succeeded',
                           last_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE id=%s
                    """,
                    [kwargs["job_id"]],
                )
            return {"processed": 1, "succeeded": 1, "failed": 0, "blocked": 0}

        with patch(
            "service.bejerman_delivery.process_bejerman_jobs",
            side_effect=mark_succeeded,
        ) as process_mock:
            _ensure_blocking_stock_sync_jobs_resolved(
                ["do-sync-lookup-test"],
                actor_user_id=1152,
            )

        self.assertEqual(process_mock.call_count, 6)
        self.assertEqual(
            {item.kwargs["job_id"] for item in process_mock.call_args_list},
            set(job_ids),
        )

    def test_chequeo_previo_avisa_sin_reintentar_ni_llamar_a_bejerman(self):
        # Este es el aviso que ve el operador al abrir el modal: tiene que ser
        # una consulta a la cola, no la espera de un reintento contra Bejerman.
        job_id = self._insert_sync_job(status="blocked")

        with patch("service.bejerman_delivery.process_bejerman_jobs") as process_mock:
            result = check_delivery_remito_stock_sync(["do-sync-lookup-test"], actor_user_id=1152)

        process_mock.assert_not_called()
        self.assertFalse(result["canEmit"])
        self.assertEqual(result["blockingSyncJobs"][0]["jobId"], job_id)
        self.assertIn(f"job #{job_id}", result["message"])
        self.assertNotIn("Se intentó resolverlo automáticamente", result["message"])

    def test_chequeo_previo_sin_jobs_habilita_la_emision(self):
        self._insert_sync_job(status="succeeded")

        result = check_delivery_remito_stock_sync(["do-sync-lookup-test"], actor_user_id=1152)

        self.assertTrue(result["canEmit"])
        self.assertEqual(result["blockingSyncJobs"], [])

    def test_chequeo_previo_con_retry_reintenta_y_devuelve_el_resultado(self):
        self._insert_sync_job(status="blocked")

        with patch(
            "service.bejerman_delivery.process_bejerman_jobs",
            return_value={"processed": 1, "succeeded": 0, "failed": 1, "blocked": 0},
        ) as process_mock:
            result = check_delivery_remito_stock_sync(
                ["do-sync-lookup-test"],
                actor_user_id=1152,
                retry=True,
            )

        process_mock.assert_called_once()
        self.assertFalse(result["canEmit"])
        self.assertTrue(result["retried"])
        self.assertIn("Se intentó resolverlo automáticamente", result["message"])

    def test_preflight_falla_cerrado_si_no_puede_consultar_la_cola(self):
        with (
            patch(
                "service.bejerman_delivery._find_blocking_stock_sync_jobs",
                side_effect=RuntimeError("base no disponible"),
            ),
            self.assertRaises(BillingError) as ctx,
        ):
            _ensure_blocking_stock_sync_jobs_resolved(
                ["do-sync-lookup-test"],
                actor_user_id=1152,
            )

        self.assertEqual(
            ctx.exception.code,
            "BEJERMAN_REMITO_SYNC_PREFLIGHT_UNAVAILABLE",
        )


class BejermanDeliveryArticleTests(SimpleTestCase):
    def setUp(self):
        # El cache de catálogos vive a nivel de proceso, así que sin esto el
        # catálogo que dejó un test se le sirve al siguiente.
        bejerman_catalog_cache.invalidate_all()

    def tearDown(self):
        bejerman_catalog_cache.invalidate_all()

    def test_sdk_article_catalog_fetches_full_catalog_without_search_parametros(self):
        client = BejermanSDKClient(company_key="SEPID")
        client.token = "TOKEN"

        with patch.object(client, "_post", return_value={"Resultado": "OK", "ErrorMsg": "", "DatosJSON": []}) as post:
            client.list_articulos(build_article_filters("máscara", field="description"), 20)

        body = post.call_args.args[1]
        self.assertIn("<a:Operacion>ObtenerArticulos</a:Operacion>", body)
        self.assertIn('<a:Parametros i:nil="true" />', body)
        self.assertIn("<a:ParametrosJson>[]</a:ParametrosJson>", body)
        self.assertNotIn("máscara", body)
        self.assertNotIn("<b:anyType", body)

    def test_delivery_article_descriptions_come_from_the_bejerman_catalog(self):
        client = FakeArticleCatalogClient()
        bridge_request = {
            "orders": [
                {
                    "orderNumber": "OE-1",
                    "deliveryType": "sale",
                    "items": [
                        {"articleCode": "1202018", "articleName": "Nombre viejo guardado en NEXORA"},
                        {"articleCode": "", "articleName": "Renglón sin artículo"},
                    ],
                }
            ]
        }

        self.assertEqual(
            _delivery_article_descriptions(client, bridge_request),
            {"1202018": "Máscara nasal adulto"},
        )

    def test_article_descriptions_never_break_the_emission(self):
        # La descripción es cosmética al lado de emitir el comprobante: si el
        # catálogo no responde, cada renglón se queda con el texto que ya tenía.
        class ClienteCaido:
            def list_articulos(self, filters, limit):
                raise BejermanSdkUnavailable("Bejerman no responde")

        self.assertEqual(bejerman_catalog_cache.article_descriptions(ClienteCaido(), ["1202018"]), {})

    def test_service_ingress_line_uses_the_bejerman_article_description(self):
        # El RIS/RDA traía la descripción de `bejerman_article_mappings`, que es una
        # copia vieja del catálogo: en MGBIO el remito nombraba un equipo distinto
        # del que Bejerman tiene para ese código.
        from service.bejerman_ris import _apply_bejerman_article_descriptions

        payload = {
            "companyKey": "SEPID",
            "documentProfile": {"type": "RDA", "updateStock": True},
            "equipment": {
                "articleCode": "1202018",
                "mappedArticleName": "Máscara nasal vieja",
                "equipmentLabel": "Máscara | BMC | N5",
            },
        }

        client = FakeArticleCatalogClient()
        # El RDA exige una única descripción literal por código. El catálogo
        # general de prueba contiene duplicados para probar el buscador.
        with patch.object(client, "list_articulos", return_value={
            "Resultado": "OK",
            "DatosJSON": [{"Articulo_Codigo": "1202018", "Articulo_Descripcion": "Máscara nasal adulto"}],
        }):
            resuelto = _apply_bejerman_article_descriptions(payload, client)

        self.assertEqual(resuelto["equipment"]["mappedArticleName"], "Máscara nasal adulto")
        self.assertEqual(payload["equipment"]["mappedArticleName"], "Máscara nasal vieja")

    def test_article_search_filters_full_catalog_by_description_and_code_without_duplicates(self):
        client = FakeArticleCatalogClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            by_description = list_bejerman_articles("máscara", 20)
            by_code = list_bejerman_articles("1202018", 20)

        self.assertEqual([item["code"] for item in by_description["items"]], ["1202018"])
        self.assertEqual([item["code"] for item in by_code["items"]], ["1202018"])
        self.assertTrue(by_description["items"][0]["requiresPartida"])
        self.assertEqual(by_description["items"][0]["salesVatType"], "1")
        self.assertEqual(by_description["items"][0]["salesVatCode"], "01")
        self.assertEqual(by_description["items"][0]["salesVatRate"], 21)
        self.assertEqual(len({item["code"] for item in by_description["items"]}), len(by_description["items"]))
        # Las dos búsquedas comparten una sola descarga del catálogo: el SDK lo
        # devuelve entero e ignora los filtros, así que se cachea y se filtra
        # localmente (ver bejerman_catalog_cache).
        self.assertEqual(len(client.calls), 1)

    def test_article_search_exposes_non_partida_articles(self):
        client = FakeArticleCatalogClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_bejerman_articles("filtro", 20)

        self.assertEqual([item["code"] for item in result["items"]], ["999"])
        self.assertFalse(result["items"][0]["stockByPartida"])
        self.assertFalse(result["items"][0]["requiresPartida"])

    def test_delivery_article_sales_vat_map_retries_missing_codes_by_code(self):
        client = FakeArticleVatLookupClient()

        result = _delivery_article_sales_vat_map(
            client,
            {
                "orders": [
                    {
                        "deliveryType": "sale",
                        "items": [
                            {"articleCode": "1100001", "unitPrice": 100},
                            {"articleCode": "1206001", "unitPrice": 200},
                        ],
                    }
                ]
            },
            {"type": "RT"},
        )

        self.assertEqual(result["1100001"]["salesVatRate"], 10.5)
        self.assertEqual(result["1206001"]["salesVatRate"], 21)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("1206001", json.dumps(client.calls[1][0], ensure_ascii=False))

    def test_delivery_article_sales_vat_map_blocks_unresolved_priced_articles(self):
        client = FakeArticleVatLookupClient(include_filtered_articles=False)

        with self.assertRaisesRegex(BejermanSdkResponseError, "No se pudo resolver el IVA de venta"):
            _delivery_article_sales_vat_map(
                client,
                {
                    "orders": [
                        {
                            "deliveryType": "sale",
                            "items": [
                                {"articleCode": "1206001", "unitPrice": 200},
                            ],
                        }
                    ]
                },
                {"type": "RT"},
            )

    def test_delivery_article_sales_vat_map_includes_zero_priced_articles(self):
        client = FakeArticleVatLookupClient()

        result = _delivery_article_sales_vat_map(
            client,
            {
                "orders": [
                    {
                        "deliveryType": "sale",
                        "items": [
                            {"articleCode": "1206001", "unitPrice": 0},
                        ],
                    }
                ]
            },
            {"type": "RT"},
        )

        self.assertEqual(result["1206001"]["salesVatCode"], "01")
        self.assertEqual(result["1206001"]["salesVatRate"], 21)

    def test_delivery_article_sales_vat_map_blocks_unresolved_zero_priced_articles(self):
        client = FakeArticleVatLookupClient(include_filtered_articles=False)

        with self.assertRaisesRegex(BejermanSdkResponseError, "No se pudo resolver el IVA de venta"):
            _delivery_article_sales_vat_map(
                client,
                {
                    "orders": [
                        {
                            "deliveryType": "sale",
                            "items": [
                                {"articleCode": "1206001", "unitPrice": 0},
                            ],
                        }
                    ]
                },
                {"type": "RT"},
            )

    def test_article_stock_returns_lots_without_fetching_cold_partidas(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_bejerman_article_stock("1202018", 100)

        self.assertEqual(client.deposit_calls, ["VAL"])
        self.assertEqual(client.partida_calls, 0)
        self.assertFalse(result["unavailable"])
        self.assertEqual(result["depositCode"], "VAL")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["partida"], "L-VAL-1")
        self.assertEqual(item["stockDepositCode"], "VAL")
        self.assertEqual(item["stockAvailableQuantity"], 2)
        self.assertIsNone(item["partidaExpirationDate"])
        self.assertTrue(item["stockCheckedAt"])

    def test_article_stock_uses_cached_partidas_only_to_enrich_expiration(self):
        client = FakeArticleStockClient()
        bejerman_catalog_cache.partida_index(client, "SEPID")

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_bejerman_article_stock("1202018", 100)

        self.assertEqual(client.partida_calls, 1)
        self.assertEqual(result["items"][0]["partidaExpirationDate"], "2030-12-31")

    def test_article_stock_uses_bounded_dedicated_timeout(self):
        class TimedOutStockClient:
            def __init__(self):
                self.timeout = 30
                self.timeout_used = None

            def obtener_stock_deposito(self, deposit_code):
                self.timeout_used = self.timeout
                raise BejermanSdkUnavailable("Bejerman no respondió a tiempo")

        client = TimedOutStockClient()
        with override_settings(BEJERMAN_DELIVERY_ORDER_STOCK_REQUEST_TIMEOUT=7):
            with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
                with self.assertRaises(BillingError):
                    list_bejerman_article_stock("1202018", 100)

        self.assertEqual(client.timeout_used, 7)
        self.assertEqual(client.timeout, 30)

    def test_article_stock_view_returns_structured_unavailable_response(self):
        request = SimpleNamespace(
            query_params={"articleCode": "1202018", "depositCode": "VAL", "companyKey": "MGBIO"},
            user=SimpleNamespace(id=1152),
        )
        with patch(
            "service.views.delivery_orders_views.list_bejerman_article_stock",
            side_effect=BillingError("BEJERMAN_SDK_ERROR", "Bejerman no respondió", status_code=502),
        ):
            response = DeliveryOrderBejermanArticleStockView().get(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items"], [])
        self.assertEqual(response.data["depositCode"], "VAL")
        self.assertEqual(response.data["companyKey"], "MGBIO")
        self.assertTrue(response.data["unavailable"])
        self.assertEqual(response.data["code"], "BEJERMAN_ARTICLE_STOCK_UNAVAILABLE")

    def test_article_stock_uses_requested_company_and_deposit(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client) as client_cls:
            result = list_bejerman_article_stock("1202018", 100, actor_user_id=1152, company_key="MGBIO", deposit_code="STL")

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152, allow_system_credentials=True)
        self.assertEqual(client.deposit_calls, ["STL"])
        self.assertEqual(result["companyKey"], "MGBIO")
        self.assertEqual(result["depositCode"], "STL")
        self.assertEqual([item["partida"] for item in result["items"]], ["L-STL-1"])
        self.assertEqual(result["items"][0]["stockDepositCode"], "STL")

    def test_article_stock_no_corta_en_100_y_avisa_si_igual_recorta(self):
        # OE-00478: SAR tenía más de 100 Auto CPAP (1105001) y el listado cortaba en 100. El
        # recuadro rechazaba las partidas de más allá del corte como "no figura en el depósito".
        class FakeSarClient:
            def obtener_stock_deposito(self, deposit_code):
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1105001",
                            "Art_CodDeposito": "SAR",
                            "Art_Partida": f"E23BE7{index:05d}",
                            "Art_DispUM1": 1,
                            "Art_RealUM1": 1,
                        }
                        for index in range(126)
                    ]
                }

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=FakeSarClient()):
            completo = list_bejerman_article_stock("1105001", 1000, deposit_code="SAR")
            recortado = list_bejerman_article_stock("1105001", 100, deposit_code="SAR")

        self.assertEqual(len(completo["items"]), 126)
        self.assertEqual(completo["totalLots"], 126)
        self.assertFalse(completo["truncated"])
        self.assertIn("E23BE700125", [item["partida"] for item in completo["items"]])
        self.assertEqual(len(recortado["items"]), 100)
        self.assertEqual(recortado["totalLots"], 126)
        self.assertTrue(recortado["truncated"])

    def test_article_stock_marks_rental_lots_that_nexora_cannot_rent(self):
        # E2321905419: stock en STL y alquilado desde 2024. El recuadro lo ofrecía y el guardado
        # lo rechazaba; el stock de alquiler ahora viene con el veredicto de NEXORA.
        client = FakeArticleStockClient()
        verdict = {
            "available": False,
            "code": "RENTAL_EQUIPMENT_NOT_AVAILABLE",
            "reason": "El NS L-STL-1 está en la OS-25863, que ya figura alquilada.",
            "equipmentInternalNumber": None,
        }

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch("service.bejerman_delivery.rental_stock_lot_availability", return_value=[verdict]) as availability,
        ):
            result = list_bejerman_article_stock(
                "1202018", 100, company_key="MGBIO", deposit_code="STL", delivery_type="rental", order_id="oe-1"
            )

        availability.assert_called_once_with(["L-STL-1"], "oe-1")
        item = result["items"][0]
        self.assertFalse(item["rentalAvailable"])
        self.assertEqual(item["rentalBlockCode"], "RENTAL_EQUIPMENT_NOT_AVAILABLE")
        self.assertIn("ya figura alquilada", item["rentalBlockReason"])

    def test_article_stock_keeps_rental_lots_unmarked_when_nexora_lookup_fails(self):
        client = FakeArticleStockClient()

        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=client),
            patch("service.bejerman_delivery.rental_stock_lot_availability", side_effect=RuntimeError("db")),
        ):
            result = list_bejerman_article_stock(
                "1202018", 100, company_key="MGBIO", deposit_code="STL", delivery_type="rental"
            )

        self.assertEqual([item["partida"] for item in result["items"]], ["L-STL-1"])
        self.assertNotIn("rentalAvailable", result["items"][0])

    def test_article_stock_falls_back_to_raw_total_when_article_has_no_partida(self):
        # Como OE-00232: article_requires_partida=false, así que las filas de stock no
        # traen Art_Partida y build_article_stock_lots las descarta. Sin este fallback
        # el frontend no tiene forma de mostrar "hay/no hay stock" para ese artículo.
        class FakeNoPartidaClient:
            def __init__(self):
                self.deposit_calls = []

            def obtener_stock_deposito(self, deposit_code):
                self.deposit_calls.append(deposit_code)
                return {
                    "DatosJSON": [
                        {
                            "Articulo_Codigo": "1203012",
                            "Art_CodDeposito": "VAL",
                            "Art_Partida": "",
                            "Art_DispUM1": 3,
                            "Art_RealUM1": 3,
                        },
                        {
                            "Articulo_Codigo": "1203012",
                            "Art_CodDeposito": "VAL",
                            "Art_Partida": "",
                            "Art_DispUM1": 4,
                            "Art_RealUM1": 4,
                        },
                    ]
                }

            def obtener_partidas(self):
                return {"DatosJSON": []}

        client = FakeNoPartidaClient()
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client):
            result = list_bejerman_article_stock("1203012", 100, company_key="MGBIO")

        self.assertEqual(result["totalAvailableQuantity"], 7)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["stockAvailableQuantity"], 7)
        self.assertEqual(result["items"][0]["partida"], "")
        self.assertIsNone(result["warning"])

    def test_article_stock_warns_when_no_partida_and_no_stock(self):
        class FakeEmptyStockClient:
            def obtener_stock_deposito(self, deposit_code):
                return {"DatosJSON": []}

            def obtener_partidas(self):
                return {"DatosJSON": []}

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=FakeEmptyStockClient()):
            result = list_bejerman_article_stock("1203012", 100, company_key="MGBIO")

        self.assertEqual(result["items"], [])
        self.assertIn("Sin stock positivo", result["warning"])

    def test_bejerman_depositos_returns_sdk_deposits_for_company(self):
        client = FakeArticleStockClient()

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=client) as client_cls:
            result = list_bejerman_depositos("MGBIO", actor_user_id=1152)

        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152, allow_system_credentials=True)
        self.assertFalse(result["unavailable"])
        self.assertEqual([item["code"] for item in result["items"]], ["VAL", "STL"])

    def test_bejerman_depositos_reusa_el_ultimo_listado_bueno_si_el_servicio_se_cae(self):
        """Un corte momentáneo de Bejerman no puede recortar el selector a VAL/STL:
        los depósitos que contestó hace un rato son el dato real."""

        class FakeDepositClient:
            def __init__(self, records):
                self.records = records

            def obtener_depositos(self):
                return {
                    "DatosJSON": [
                        {"Deposito_Codigo": code, "Deposito_Descripcion": name}
                        for code, name in self.records
                    ]
                }

        good = FakeDepositClient([("VAL", "Ventas"), ("STR", "Reparación"), ("DES", "Descarte")])
        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=good):
            list_bejerman_depositos("SEPID")

        class FakeDownClient:
            def obtener_depositos(self):
                raise BejermanSdkTransportUnavailable(
                    "Error HTTP Bejerman 500: System.ServiceModel.ServiceActivationException"
                )

        caido = FakeDownClient()
        with (
            patch("service.bejerman_delivery.BejermanSDKClient", return_value=caido),
            override_settings(BEJERMAN_DEPOSITS_CACHE_SECONDS=0),
        ):
            result = list_bejerman_depositos("SEPID")

        self.assertTrue(result["unavailable"])
        self.assertEqual([item["code"] for item in result["items"]], ["VAL", "STR", "DES"])
        self.assertIn("últimos depósitos conocidos", result["warning"])
        # El detalle técnico queda en el log, no en la pantalla del operador.
        self.assertNotIn("ServiceActivationException", result["warning"])

    def test_bejerman_depositos_cae_al_listado_fijo_sin_nada_cacheado(self):
        class FakeDownClient:
            company_key = "SEPID"

            def obtener_depositos(self):
                raise BejermanSdkTransportUnavailable("servicio caído")

        with patch("service.bejerman_delivery.BejermanSDKClient", return_value=FakeDownClient()):
            result = list_bejerman_depositos("SEPID")

        self.assertTrue(result["unavailable"])
        # STC entra en el listado fijo porque es el depósito del que sale el remito de una
        # reparación: sin él, una caída del catálogo deja esas órdenes sin depósito elegible.
        self.assertEqual([item["code"] for item in result["items"]], ["VAL", "STL", "STC"])
        self.assertIn("depósitos estándar", result["warning"])

    def test_article_stock_helper_filters_deposit_and_zero_stock(self):
        stock_rows = build_stock_rows(FakeArticleStockClient().obtener_stock_deposito("VAL"))
        partida_rows = build_partida_rows(FakeArticleStockClient().obtener_partidas())

        lots = build_article_stock_lots(stock_rows, partida_rows, "1202018", "VAL")

        self.assertEqual([lot["partida"] for lot in lots], ["L-VAL-1"])
        self.assertTrue(all(lot["depositCode"] == "VAL" for lot in lots))
        self.assertTrue(all((lot["availableQuantity"] if lot["availableQuantity"] is not None else lot["realQuantity"]) > 0 for lot in lots))

    def test_insert_item_partidas_persists_stock_metadata(self):
        cur = Mock()

        insert_item_partidas(
            cur,
            "doi-1",
            [
                {
                    "partida": "L-VAL-1",
                    "assignedQuantity": "1",
                    "partidaExpirationDate": "2030-12-31",
                    "stockDepositCode": "VAL",
                    "stockAvailableQuantity": "2",
                    "stockCheckedAt": "2026-06-17T10:00:00-03:00",
                    "ingresoId": 29470,
                    "deviceId": 315,
                }
            ],
        )

        values = cur.execute.call_args.args[1]
        self.assertEqual(values[1], "doi-1")
        self.assertEqual(values[2], 29470)
        self.assertEqual(values[3], 315)
        self.assertEqual(values[4], "L-VAL-1")
        self.assertEqual(values[6], "2030-12-31")
        self.assertEqual(values[7], "VAL")
        self.assertEqual(str(values[8]), "2")
        self.assertEqual(values[9], "2026-06-17T10:00:00-03:00")


class BejermanIngressCompanyTests(SimpleTestCase):
    def test_ingress_companies_are_controlled(self):
        companies = list_ingress_companies()

        self.assertEqual([company.key for company in companies], ["SEPID", "MGBIO", "TEST"])
        self.assertEqual(require_company("MGBI").key, "MGBIO")
        self.assertEqual(require_company("MODE").key, "TEST")

    @override_settings(BEJERMAN_COMPANY_TEST="SEP")
    def test_sdk_resolves_company_key(self):
        self.assertEqual(BejermanSDKClient(company_key="SEPID").company, "SEP")
        self.assertEqual(BejermanSDKClient(company_key="MGBIO").company, "MGBI")
        self.assertEqual(BejermanSDKClient(company_key="TEST").company, "SEP")

    def test_sdk_sends_parametros_json_as_single_json_string(self):
        client = BejermanSDKClient(company_key="SEPID")
        client.token = "TOKEN"

        with patch.object(client, "_post", return_value={"Resultado": "OK", "ErrorMsg": ""}) as post:
            client.list_comprobantes_ventas([{"Campo": "Cliente_Codigo", "Valor": "ALCLA"}])

        body = post.call_args.args[1]
        self.assertIn("<a:ParametrosJson>", body)
        self.assertIn('\\"ALCLA\\"', body)
        self.assertIn('"N"', body)
        self.assertNotIn("<b:string>", body)

    def test_sdk_acepta_aviso_de_importacion_correcta_como_ok(self):
        self.assertTrue(
            is_ok_response(
                {
                    "Resultado": "OK",
                    "ErrorMsg": (
                        "El comprobante se importó correctamente. "
                        "La Fecha de Contabilización se modificó ya que debe ser posterior "
                        "a la del último cierre del Libro IVA Ventas."
                    ),
                }
            )
        )
        self.assertFalse(is_ok_response({"Resultado": "ERROR", "ErrorMsg": "El comprobante se importó correctamente."}))
        self.assertFalse(is_ok_response({"Resultado": "OK", "ErrorMsg": "Artículo inexistente"}))

    @override_settings(
        BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES="",
        BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES="",
        BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES="",
        BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="",
    )
    def test_sdk_emission_article_quantity_sign_by_document_type(self):
        expected = {
            "RD": -1,
            "RDN": -1,
            "RDA": -1,
            "RIS": 1,
            "RT": 1,
            "RTN": 1,
            "RTA": 1,
            "RSS": 1,
        }
        for document_type, quantity in expected.items():
            with self.subTest(document_type=document_type):
                self.assertEqual(sdk_emission_article_quantity(document_type, 1), quantity)
                self.assertEqual(sdk_signed_article_quantity(document_type, 1), quantity)
                self.assertEqual(sdk_emission_article_quantity(document_type, -1), quantity)
                self.assertEqual(sdk_uses_negative_article_quantity(document_type), quantity < 0)

    @override_settings(
        BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES="RDA",
        BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES="",
        BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES="",
        BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RT,RTN,RTA,RSS",
    )
    def test_sdk_signed_article_quantity_respects_general_override(self):
        self.assertEqual(sdk_emission_article_quantity("RDA", 1), -1)
        self.assertEqual(sdk_emission_article_quantity("RT", 1), 1)

    @override_settings(
        BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES="",
        BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES="",
        BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES="",
        BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RIS",
    )
    def test_legacy_sdk_quantity_setting_remains_supported(self):
        self.assertEqual(sdk_emission_article_quantity("RIS", 1), -1)
        self.assertEqual(sdk_emission_article_quantity("RT", 1), 1)

    def test_ris_payload_includes_company_key(self):
        payload = _build_payload(
            {
                "id": 29012,
                "empresa_bejerman": "MGBIO",
                "customer_code": "ARGAS",
                "customer_name": "ARGENTINA DE GASES S.A.",
                "fecha_ingreso": "2026-06-09",
                "numero_serie": "A3125412159",
                "numero_interno": "",
                "tipo_equipo": "BPAP",
                "marca": "BMC",
                "modelo": "GI",
                "equipo_variante": "25",
                "device_variante": "",
                "modelo_variante": "",
                "motivo": "No enciende",
                "accesorios": "",
                "comentarios": "",
                "model_id": None,
            }
        )

        self.assertEqual(payload["companyKey"], "MGBIO")
        self.assertEqual(payload["companyLabel"], "MG BIO")
        self.assertEqual(payload["bejermanCompany"], "MGBI")
        self.assertEqual(payload["documentProfile"]["deposit"], "STR")
        self.assertEqual(payload["equipment"]["articleCode"], "")
        self.assertTrue(payload["equipment"]["mappingRequired"])
        self.assertEqual(payload["equipment"]["articleName"], "BPAP BMC GI 25")
        # mappedArticleName ahora es exclusivamente la descripción que Bejerman tiene
        # registrada para el código (vacía si todavía no hay mapeo confirmado); el texto
        # armado localmente (equipmentLabel) ya no se mezcla acá, solo en articleName.
        self.assertEqual(payload["equipment"]["mappedArticleName"], "")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_uses_bejerman_customer_fiscal_fields_and_no_stock(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-1",
                "ingresoId": 1,
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipment": {"articleCode": "SERVICIO", "serial": "SN-1"},
            },
            {
                "Cliente_RazonSocial": "ALCLA SRL",
                "Cliente_NroDocumento": "30700000000",
                "Cliente_Provincia": "02",
                "Cliente_SitIVA": "RI",
                "Cliente_NumeroIIBB": "123",
            },
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Cliente_SitIVA"], "RI")
        self.assertEqual(comprobante["Cliente_RazonSocial"], "ALCLA SRL")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 1)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], 0)
        self.assertEqual(article_lines[0]["Item_Partida"], "SN-1")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_article_line_uses_equipment_details(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-29382",
                "ingresoId": 29382,
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipment": {
                    "articleCode": "1115003",
                    "articleName": "Equipo recibido para servicio técnico",
                    "serial": "G0588065",
                    "internalNumber": "MG 6697",
                    "equipmentType": "OXÍMETRO DE PULSO",
                    "brand": "Nellcor",
                    "model": "N-595",
                    "repairReason": "Reparación",
                },
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        comprobante = built["comprobante"]
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], "OXÍMETRO DE PULSO | Nellcor | N-595")
        self.assertEqual(legend_lines[0], "Ingreso a servicio técnico - OS 29382 -")
        self.assertIn("Motivo: Reparación", legend_lines)
        self.assertIn("Interno: MG 6697", legend_lines)
        self.assertEqual(article_lines[0]["Item_Partida"], "G0588065")
        self.assertFalse(any("----------------" in line for line in legend_lines))
        self.assertFalse(any(line.startswith("N/S:") for line in legend_lines))
        self.assertFalse(any("OXÍMETRO" in line for line in legend_lines))

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_accepts_multiple_equipments(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-lote-1-2",
                "ingresoId": 1,
                "ingresoIds": [1, 2],
                "issueDate": "2026-06-12",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipments": [
                    {
                        "articleCode": "SERVICIO",
                        "articleName": "Equipo recibido",
                        "serial": "SN-1",
                        "equipmentType": "CPAP",
                        "brand": "BMC",
                        "model": "G3",
                        "repairReason": "No enciende",
                        "accessories": "Bolso (ref: B-1), batería (ref: 2046-01)",
                        "osLabel": "00001",
                    },
                    {
                        "articleCode": "SERVICIO",
                        "articleName": "Equipo recibido",
                        "serial": "SN-2",
                        "brand": "ResMed",
                        "model": "AirSense 10",
                        "repairReason": "Ruido",
                        "osLabel": "00002",
                    },
                ],
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        comprobante = built["comprobante"]
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in comprobante["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(legend_lines[0], "Se recibe para servicio técnico:")
        self.assertIn("- OS 00001 -", legend_lines)
        self.assertIn("- OS 00002 -", legend_lines)
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-1", "SN-2"])
        self.assertFalse(any("SN-1" in line for line in legend_lines))
        self.assertFalse(any("SN-2" in line for line in legend_lines))
        self.assertIn("Motivo: No enciende", legend_lines)
        self.assertIn("Accesorios: Bolso (ref: B-1)", legend_lines)
        self.assertIn("Accesorios: batería (ref: 2046-01)", legend_lines)
        self.assertFalse(any("----------------" in line for line in legend_lines))
        self.assertFalse(any(line.startswith(("N/S:", "Partida:")) for line in legend_lines))
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], "CPAP | BMC | G3")
        self.assertEqual(article_lines[1]["Item_DescripArticulo"], "ResMed | AirSense 10")
        self.assertEqual(len(article_lines), 2)
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [1, 1])
        self.assertEqual([item["Item_CantidadUM2"] for item in article_lines], [0, 0])
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_lote_destaca_os_sin_repetir_la_serie_impresa_en_columna(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-lote-13",
                "ingresoId": 13,
                "issueDate": "2026-08-10",
                "customerCode": "ALCLA",
                "customerName": "ALCLA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "equipments": [
                    {
                        "articleCode": "1115003",
                        "articleName": "Concentrador de oxígeno",
                        "serial": "MZJ5S121916",
                        "internalNumber": "MG 9901",
                        "repairReason": "reparación alquiler",
                        "osLabel": "00013",
                    }
                ],
            },
            {"Cliente_RazonSocial": "ALCLA SRL"},
        )

        items = built["comprobante"]["Comprobante_Items"]
        legend_lines = [item["Item_DescripArticulo"] for item in items if item["Item_Tipo"] == "L"]
        article = next(item for item in items if item["Item_Tipo"] == "A")

        self.assertEqual(
            legend_lines,
            [
                "Se recibe para servicio técnico:",
                "- OS 00013 -",
                "Interno: MG 9901",
                "Motivo: reparación alquiler",
            ],
        )
        self.assertEqual(article["Item_Partida"], "MZJ5S121916")
        self.assertFalse(any("MZJ5S121916" in line for line in legend_lines))
        self.assertFalse(any("----------------" in line for line in legend_lines))

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_ris_comprobante_compacta_leyendas_para_registro_manual_largo(self):
        equipments = [
            {
                "articleCode": "SERVICIO",
                "articleName": "Equipo recibido",
                "serial": f"SN-{index:02d}",
                "brand": "Marca",
                "model": "Modelo",
                "repairReason": "Reparacion",
                "accessories": "Bolso, cable",
                "comments": "Comentario largo",
                "osLabel": f"29{index:03d}",
            }
            for index in range(18)
        ]
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-lote-compacto",
                "ingresoId": 29000,
                "ingresoIds": list(range(29000, 29018)),
                "issueDate": "2026-06-23",
                "customerCode": "TMD",
                "customerName": "Cliente",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "compactServiceIngressLegends": True,
                "equipments": equipments,
            },
            {"Cliente_RazonSocial": "Cliente"},
        )

        items = built["comprobante"]["Comprobante_Items"]
        legend_lines = [item for item in items if item["Item_Tipo"] == "L"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        self.assertEqual(len(legend_lines), 1)
        self.assertEqual(len(article_lines), 18)
        self.assertLess(len(json.dumps(built["comprobante"], ensure_ascii=False)), 30000)

    def test_registered_rda_uses_sdk_sign_compensation(self):
        payload = {
            "requestId": "reparaciones-ingreso-rda-1",
            "ingresoId": 1,
            "issueDate": "2026-06-24",
            "customerCode": "SIMPLE",
            "customerName": "SIMPLE SALUD SA",
            "sellerCode": "ADM",
            "paymentTermCode": "30",
            "documentProfile": {
                "key": "rda",
                "type": "RDA",
                "label": "RDA",
                "letter": "R",
                "reason": "Ingreso de equipo MG activo desde alquiler.",
                "deposit": "STR",
                "operation": "ALQ",
                "pointOfSale": "00004",
                "updateStock": True,
            },
            "equipment": {
                "articleCode": "1115007",
                "articleName": "Concentrador de oxigeno",
                "serial": "G3GB4000030",
                "internalNumber": "MG 1234",
            },
        }
        built = build_service_ingress_comprobante(payload, {"Cliente_RazonSocial": "SIMPLE SALUD SA"})
        emitted_article_lines = [
            item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"
        ]
        self.assertEqual(emitted_article_lines[0]["Item_CantidadUM1"], -1)
        self.assertEqual(emitted_article_lines[0]["Item_CantidadUM2"], -1)
        self.assertEqual(emitted_article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(emitted_article_lines[0]["Item_Partida"], "G3GB4000030")
        legend_lines = [
            item["Item_DescripArticulo"]
            for item in built["comprobante"]["Comprobante_Items"]
            if item["Item_Tipo"] == "L"
        ]
        self.assertIn("Ingreso a servicio técnico - OS 00001 -", legend_lines)
        self.assertIn("Motivo: Ingreso de equipo MG activo desde alquiler. (MG 1234)", legend_lines)

        comprobante = _apply_registered_document(
            built["comprobante"],
            payload,
            {
                "type": "RDA",
                "letter": "R",
                "point": "00004",
                "number": "00004684",
                "remitoNumber": "RDA R 00004-00004684",
            },
        )

        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDA")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], -1)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], -1)
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "G3GB4000030")

    def test_rda_comprobante_uses_internal_number_as_stock_partida_when_serial_missing(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-rda-internal",
                "ingresoId": 2,
                "issueDate": "2026-06-24",
                "customerCode": "SIMPLE",
                "customerName": "SIMPLE SALUD SA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "documentProfile": {
                    "type": "RDA",
                    "letter": "R",
                    "deposit": "STR",
                    "operation": "ALQ",
                    "pointOfSale": "00004",
                    "updateStock": True,
                },
                "equipment": {
                    "articleCode": "1115007",
                    "articleName": "Concentrador de oxigeno",
                    "serial": "",
                    "internalNumber": "MG 1234",
                },
            },
            {"Cliente_RazonSocial": "SIMPLE SALUD SA"},
        )

        items = built["comprobante"]["Comprobante_Items"]
        legend_lines = [item for item in items if item["Item_Tipo"] == "L"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        self.assertTrue(all(item["Item_Partida"] == " " for item in legend_lines))
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], -1)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], -1)
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "MG 1234")
        self.assertFalse(any(item["Item_Partida"].strip() == "" for item in article_lines))

    def test_service_ingress_stock_article_requires_partida_before_sdk_emit(self):
        for document_type in ("RDA", "RDN", "RIS"):
            with self.subTest(document_type=document_type):
                with self.assertRaisesRegex(BejermanSdkResponseError, f"{document_type}_PARTIDA_REQUIRED"):
                    build_service_ingress_comprobante(
                        {
                            "requestId": f"reparaciones-ingreso-{document_type.lower()}-sin-partida",
                            "ingresoId": 3,
                            "issueDate": "2026-06-24",
                            "customerCode": "SIMPLE",
                            "customerName": "SIMPLE SALUD SA",
                            "sellerCode": "ADM",
                            "paymentTermCode": "30",
                            "documentProfile": {
                                "type": document_type,
                                "letter": "R",
                                "deposit": "STR",
                                "operation": "ALQ",
                                "pointOfSale": "00004",
                                "updateStock": True,
                            },
                            "equipment": {
                                "articleCode": "1115007",
                                "articleName": "Concentrador de oxigeno",
                                "serial": "",
                                "internalNumber": "",
                            },
                        },
                        {"Cliente_RazonSocial": "SIMPLE SALUD SA"},
                    )

    def test_service_ingress_stock_documents_send_signed_quantity_without_stock_lookup(self):
        for document_type in ("RDA", "RDN", "RIS"):
            with self.subTest(document_type=document_type):
                built = build_service_ingress_comprobante(
                    {
                        "requestId": f"reparaciones-ingreso-{document_type.lower()}",
                        "ingresoId": 4,
                        "issueDate": "2026-06-24",
                        "customerCode": "SIMPLE",
                        "customerName": "SIMPLE SALUD SA",
                        "sellerCode": "ADM",
                        "paymentTermCode": "30",
                        "documentProfile": {
                            "type": document_type,
                            "letter": "R",
                            "deposit": "STR",
                            "operation": "REP",
                            "pointOfSale": "00004",
                            "updateStock": True,
                        },
                        "equipment": {
                            "articleCode": "1115007",
                            "articleName": "Concentrador de oxigeno",
                            "serial": f"SN-{document_type}",
                            "internalNumber": "MG 1234",
                        },
                    },
                    {"Cliente_RazonSocial": "SIMPLE SALUD SA"},
                )

                comprobante = built["comprobante"]
                article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
                expected_quantity = 1 if document_type == "RIS" else -1
                self.assertEqual(comprobante["Comprobante_Tipo"], document_type)
                self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
                self.assertEqual(article_lines[0]["Item_CantidadUM1"], expected_quantity)
                self.assertEqual(article_lines[0]["Item_CantidadUM2"], expected_quantity)
                self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
                self.assertEqual(article_lines[0]["Item_Partida"], f"SN-{document_type}")
                self.assertEqual(comprobante["Comprobante_ImporteTotal"], 0)

    def test_ris_stock_ingress_sends_positive_quantity_for_new_equipment(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "reparaciones-ingreso-ris-stock-29470",
                "ingresoId": 29470,
                "issueDate": "2026-07-02",
                "customerCode": "MEDIBE",
                "customerName": "MEDIBEL SA",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "documentProfile": {
                    "key": "ris",
                    "type": "RIS",
                    "label": "RIS",
                    "letter": "R",
                    "reason": "Ingreso normal de servicio tecnico.",
                    "deposit": "STR",
                    "operation": "REP",
                    "pointOfSale": "00004",
                    "updateStock": True,
                },
                "equipment": {
                    "articleCode": "1120002",
                    "articleName": "Videolaringoscopio",
                    "serial": "2511062",
                    "equipmentType": "VIDEOLARINGOSCOPIO",
                    "brand": "Coreray",
                    "model": "CR-31",
                    "repairReason": "urgente control",
                },
            },
            {"Cliente_RazonSocial": "MEDIBEL SA"},
        )

        comprobante = built["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RIS")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 1)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], 1)
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "2511062")

    def test_delivery_signed_sdk_quantity_does_not_invert_amounts(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-rd",
                "issueDate": "2026-06-25",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-RD",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Equipo RD",
                                "quantity": 2,
                                "unitPrice": 100,
                                "partida": "SN-RD",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RD", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        comprobante = built["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], -2)
        self.assertEqual(article_lines[0]["Item_Importe"], 200)
        self.assertEqual(article_lines[0]["Item_ImporteTotal"], 200)
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 200)


class BejermanDeliveryRemitoPayloadTests(SimpleTestCase):
    def test_service_release_equipment_detail_matches_rss_visible_data(self):
        detail = _service_release_equipment_detail(
            {
                "tipo_equipo": "OXÍMETRO DE PULSO",
                "marca": "Nellcor",
                "modelo": "N-595",
                "ingreso_id": 29381,
                "equipment_serial": "G0588065",
                "equipment_internal_number": "MG 6697",
            }
        )

        self.assertEqual(
            detail,
            "OXÍMETRO DE PULSO | Nellcor | N-595 - N° interno (MG) MG 6697",
        )
        self.assertNotIn("OS-29381", detail)
        self.assertNotIn("G0588065", detail)

    def test_nexora_order_payload_uses_equipment_serial_as_item_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OES-1",
                "deliveryType": "service_release",
                "ingresoId": 29381,
                "sourceReference": "OS 29381",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                    }
                ],
            }
        )

        self.assertEqual(payload["items"][0]["partida"], "SN-123")
        self.assertEqual(payload["sourceReference"], "OS-29381")

    def test_nexora_order_payload_keeps_explicit_item_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                        "partida": "MANUAL-1",
                    }
                ],
            }
        )

        self.assertEqual(payload["items"][0]["partida"], "MANUAL-1")

    def test_nexora_order_payload_reenvia_moneda_cotizacion_y_descuento(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "priceCurrency": "USD",
                "commercialExchangeRate": "1200",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 2,
                        "unitPrice": 100,
                        "priceCurrency": "USD",
                        "discountPercent": 10,
                        "partida": "P1",
                    }
                ],
            }
        )

        self.assertEqual(payload["priceCurrency"], "USD")
        self.assertEqual(payload["commercialExchangeRate"], "1200")
        self.assertEqual(payload["items"][0]["priceCurrency"], "USD")
        self.assertEqual(payload["items"][0]["discountPercent"], 10)

    def test_sale_order_payload_does_not_use_equipment_serial_as_partida(self):
        payload = _bridge_order(
            {
                "id": "do-1",
                "orderNumber": "OE-1",
                "deliveryType": "sale",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "110706",
                        "quantity": 1,
                    }
                ],
            }
        )

        self.assertIsNone(payload["items"][0]["partida"])

    def test_sale_order_with_unknown_partida_policy_blocks_before_remito(self):
        with self.assertRaises(DeliveryOrderError) as ctx:
            _assert_partidas_ready(
                {
                    "deliveryType": "sale",
                    "equipmentSerial": "SN-123",
                    "items": [
                        {
                            "id": "doi-1",
                            "articleCode": "110706",
                            "quantity": 1,
                        }
                    ],
                }
            )

        self.assertEqual(ctx.exception.code, "DELIVERY_ORDER_ARTICLE_PARTIDA_POLICY_UNKNOWN")

    def test_sale_order_with_partida_article_requires_partida_before_remito(self):
        with self.assertRaisesRegex(Exception, "Complete las partidas"):
            _assert_partidas_ready(
                {
                    "deliveryType": "sale",
                    "equipmentSerial": "SN-123",
                    "items": [
                        {
                            "id": "doi-1",
                            "articleCode": "110706",
                            "articleRequiresPartida": True,
                            "quantity": 1,
                        }
                    ],
                }
            )

    def test_sale_order_with_non_partida_article_allows_missing_partida_before_remito(self):
        _assert_partidas_ready(
            {
                "deliveryType": "sale",
                "equipmentSerial": "SN-123",
                "items": [
                    {
                        "id": "doi-1",
                        "articleCode": "1208010",
                        "articleRequiresPartida": False,
                        "quantity": 10,
                    }
                ],
            }
        )

    def _preflight_order(self, quantity, delivery_type="sale"):
        return {
            "id": "do-1",
            "deliveryType": delivery_type,
            "companyKey": "SEPID",
            "items": [
                {
                    "id": "doi-1",
                    "articleCode": "1208009",
                    "articleName": "Filtro p/ aspirador SILFAB FI13",
                    "quantity": quantity,
                }
            ],
        }

    def test_preflight_avisa_el_faltante_de_stock_antes_de_emitir(self):
        with patch.object(bejerman_delivery, "get_delivery_order", return_value=self._preflight_order(19)):
            with patch.object(delivery_orders, "_delivery_article_available_quantity", return_value=Decimal("18")):
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertFalse(result["canEmit"])
        self.assertEqual(result["code"], "DELIVERY_REMITO_ARTICLE_STOCK_INSUFFICIENT")
        self.assertEqual(result["blockingSyncJobs"], [])
        self.assertIn("Filtro p/ aspirador SILFAB FI13", result["message"])
        self.assertIn("hay 18 en VAL", result["message"])
        self.assertIn("pide 19", result["message"])

    def test_preflight_no_bloquea_cuando_el_stock_alcanza(self):
        with patch.object(bejerman_delivery, "get_delivery_order", return_value=self._preflight_order(19)):
            with patch.object(delivery_orders, "_delivery_article_available_quantity", return_value=Decimal("19")):
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertTrue(result["canEmit"])
        self.assertNotIn("articleStockShortages", result)

    def test_preflight_no_bloquea_si_no_puede_consultar_el_stock(self):
        with patch.object(bejerman_delivery, "get_delivery_order", return_value=self._preflight_order(19)):
            with patch.object(
                delivery_orders,
                "_delivery_article_available_quantity",
                side_effect=RuntimeError("Bejerman caído"),
            ):
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertTrue(result["canEmit"])

    def test_preflight_no_mira_stock_de_articulo_en_alquileres(self):
        with patch.object(
            bejerman_delivery, "get_delivery_order", return_value=self._preflight_order(19, delivery_type="rental")
        ):
            with patch.object(delivery_orders, "_delivery_article_available_quantity") as available:
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertTrue(result["canEmit"])
        available.assert_not_called()

    def test_stock_fresco_baja_el_deposito_una_sola_vez_por_tanda(self):
        rows = [
            {"articleCode": "1208009", "availableQuantity": 18},
            {"articleCode": "1208031", "availableQuantity": 25},
        ]
        cache = {}
        with patch("service.bejerman_sdk.BejermanSDKClient", return_value=Mock()):
            with patch.object(bejerman_catalog_cache, "stock_rows", return_value=rows) as stock_rows:
                primero = delivery_orders._delivery_article_available_quantity(
                    "1208009", "VAL", "SEPID", None, cache, force_refresh=True
                )
                segundo = delivery_orders._delivery_article_available_quantity(
                    "1208031", "VAL", "SEPID", None, cache, force_refresh=True
                )

        self.assertEqual(primero, Decimal("18"))
        self.assertEqual(segundo, Decimal("25"))
        self.assertTrue(stock_rows.call_args_list[0].kwargs["force_refresh"])
        self.assertFalse(stock_rows.call_args_list[1].kwargs["force_refresh"])

    def test_preflight_lee_stock_fresco_y_no_el_cacheado(self):
        # Corrigen el stock a mano en Bejerman y vuelven enseguida: con el cache de
        # 30 s el preflight bloquearía con un número que ya no existe.
        with patch.object(bejerman_delivery, "get_delivery_order", return_value=self._preflight_order(19)):
            with patch.object(
                delivery_orders, "_delivery_article_available_quantity", return_value=Decimal("19")
            ) as available:
                bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertTrue(available.call_args.kwargs["force_refresh"])

    def test_preflight_suma_el_mismo_articulo_de_varias_ordenes(self):
        orders = {"do-1": self._preflight_order(10), "do-2": self._preflight_order(10)}
        orders["do-2"]["id"] = "do-2"

        with patch.object(bejerman_delivery, "get_delivery_order", side_effect=lambda order_id, **_: orders[order_id]):
            with patch.object(delivery_orders, "_delivery_article_available_quantity", return_value=Decimal("15")):
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1", "do-2"], None, retried=False)

        self.assertFalse(result["canEmit"])
        self.assertIn("pide 20", result["message"])

    def test_preflight_cuenta_en_el_deposito_de_las_partidas(self):
        # OE-00130 (10/09/2026): 20 CPAP armados desde SAR. Con partidas el formulario deja
        # vacío el depósito del renglón y el preflight caía en VAL ("hay 9 en VAL").
        order = self._preflight_order(20)
        order["items"][0]["stockDepositCode"] = ""
        order["items"][0]["partidas"] = [
            {"partida": f"E238D5010{n:02d}", "assignedQuantity": 1.0, "stockDepositCode": "SAR"}
            for n in range(20)
        ]
        stock = {"SAR": Decimal("20"), "VAL": Decimal("9")}

        with patch.object(bejerman_delivery, "get_delivery_order", return_value=order):
            with patch.object(
                delivery_orders,
                "_delivery_article_available_quantity",
                side_effect=lambda article, deposit, *args, **kwargs: stock[deposit],
            ) as available:
                result = bejerman_delivery._delivery_remito_preflight_result(["do-1"], None, retried=False)

        self.assertTrue(result["canEmit"])
        self.assertEqual([call.args[1] for call in available.call_args_list], ["SAR"])

    def test_sale_remito_profile_is_not_rss(self):
        sale_profile = remito_profile_for_type("sale")
        service_profile = remito_profile_for_type("service_release")

        self.assertEqual(sale_profile.comprobante_tipo, "RT")
        self.assertEqual(sale_profile.point_of_sale, "00004")
        self.assertEqual(sale_profile.deposit_code, "VAL")
        self.assertEqual(service_profile.comprobante_tipo, "RSS")
        self.assertNotEqual(sale_profile.comprobante_tipo, service_profile.comprobante_tipo)

    def test_delivery_remito_profile_usa_punto_por_empresa(self):
        with override_settings(
            BEJERMAN_REMITO_SALE_POINT_OF_SALE="00002",
            BEJERMAN_REMITO_SALE_SEPID_POINT_OF_SALE="00003",
            BEJERMAN_REMITO_SALE_MGBIO_POINT_OF_SALE="00008",
        ):
            sepid_profile = _profile_for_order("sale", "SEPID")
            mgbio_profile = _profile_for_order("sale", "MGBIO")

        self.assertEqual(sepid_profile["type"], "RT")
        self.assertEqual(sepid_profile["pointOfSale"], "00004")
        self.assertEqual(mgbio_profile["type"], "RT")
        self.assertEqual(mgbio_profile["pointOfSale"], "00007")
        self.assertEqual(delivery_remito_config(sepid_profile)["salePointOfSale"], "00004")
        self.assertEqual(delivery_remito_config(mgbio_profile)["salePointOfSale"], "00007")

    def test_delivery_remito_lote_no_permite_mezclar_empresas(self):
        def order(order_id, company_label):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "operationCompanyLabel": company_label,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        compatibility = _validate_remito_orders([order("do-1", "SEPID")])
        self.assertEqual(compatibility["companyKey"], "SEPID")
        self.assertEqual(_validate_remito_orders([order("do-2", "MG BIO")])["companyKey"], "MGBIO")
        with self.assertRaisesRegex(Exception, "empresas Bejerman distintas"):
            _validate_remito_orders([order("do-1", "SEPID"), order("do-2", "MG BIO")])

    def test_delivery_remito_cliente_particular_usa_datos_del_propietario(self):
        def order(order_id, propietario_doc):
            return {
                "id": order_id,
                "bejermanCustomerCode": "",
                "customerName": "Particular",
                "propietarioNombre": "Juan Perez",
                "propietarioDoc": propietario_doc,
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "operationCompanyLabel": "SEPID",
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        compatibility = _validate_remito_orders([order("do-part-1", "30710698"), order("do-part-2", "30710698")])

        self.assertEqual(compatibility["customerCode"], "")
        self.assertEqual(compatibility["propietarioNombre"], "Juan Perez")
        self.assertEqual(compatibility["propietarioDoc"], "30710698")
        self.assertEqual(compatibility["customerName"], "Juan Perez")

    def test_delivery_remito_lote_no_permite_particulares_con_documentos_distintos(self):
        def order(order_id, propietario_doc):
            return {
                "id": order_id,
                "bejermanCustomerCode": "",
                "customerName": "Particular",
                "propietarioNombre": "Juan Perez",
                "propietarioDoc": propietario_doc,
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "operationCompanyLabel": "SEPID",
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        with self.assertRaisesRegex(Exception, "mismo propietario"):
            _validate_remito_orders([order("do-part-1", "30710698"), order("do-part-2", "27263312258")])

    def test_delivery_remito_particular_sin_propietario_sigue_bloqueado(self):
        order = {
            "id": "do-part-sin-doc",
            "bejermanCustomerCode": "",
            "customerName": "Particular",
            "deliveryType": "sale",
            "status": "armado_pendiente_entrega",
            "operationCompanyLabel": "SEPID",
            "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
        }

        with self.assertRaisesRegex(Exception, "cargue nombre y documento del propietario"):
            _validate_remito_orders([order])

    def test_delivery_remito_cliente_sin_codigo_nombra_al_cliente(self):
        # Una sola orden de un cliente con razón social propia y sin código: el error tiene que
        # hablar de ese cliente, no de que "todas las órdenes" compartan código.
        order = {
            "id": "do-sin-codigo",
            "bejermanCustomerCode": "",
            "customerName": "NOVAMED SA",
            "deliveryType": "sale",
            "status": "armado_pendiente_entrega",
            "operationCompanyLabel": "SEPID",
            "items": [{"articleCode": "1201001", "quantity": 1, "partida": "P1"}],
        }

        with self.assertRaisesRegex(Exception, "NOVAMED SA: el cliente no tiene código Bejerman"):
            _validate_remito_orders([order])

    def test_delivery_remito_lote_no_permite_mezclar_monedas(self):
        def order(order_id, price_currency):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": "SEPID",
                "priceCurrency": price_currency,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order("do-ars", "ARS"), order("do-usd", "USD")])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_delivery_remito_lote_no_permite_mezclar_monedas_por_item(self):
        order = {
            "id": "do-mixed-items",
            "bejermanCustomerCode": "ACU",
            "customerName": "ACUMAR",
            "deliveryType": "sale",
            "status": "armado_pendiente_entrega",
            "companyKey": "SEPID",
            "priceCurrency": "ARS",
            "items": [
                {"articleCode": "110706", "quantity": 1, "partida": "P1", "priceCurrency": "ARS"},
                {"articleCode": "110707", "quantity": 1, "partida": "P2", "priceCurrency": "USD"},
            ],
        }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_delivery_remito_lote_usd_no_permite_mezclar_cotizaciones(self):
        def order(order_id, exchange_rate):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": "SEPID",
                "priceCurrency": "USD",
                "commercialExchangeRate": exchange_rate,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order("do-usd-1", "1200"), order("do-usd-2", "1210")])

        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_DELIVERY_REMITO_EXCHANGE_RATE")

    def test_delivery_remito_lote_usd_requiere_cotizacion(self):
        order = {
            "id": "do-usd-no-tc",
            "bejermanCustomerCode": "ACU",
            "customerName": "ACUMAR",
            "deliveryType": "sale",
            "status": "armado_pendiente_entrega",
            "companyKey": "SEPID",
            "priceCurrency": "USD",
            "commercialExchangeRate": "",
            "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
        }

        with self.assertRaises(DeliveryOrderError) as ctx:
            _validate_remito_orders([order])

        self.assertEqual(ctx.exception.code, "DELIVERY_REMITO_EXCHANGE_RATE_REQUIRED")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_delivery_remito_lote_usa_company_key_explicito(self):
        def order(order_id, company_key, legacy_label="SEPID"):
            return {
                "id": order_id,
                "bejermanCustomerCode": "ACU",
                "customerName": "ACUMAR",
                "deliveryType": "sale",
                "status": "armado_pendiente_entrega",
                "companyKey": company_key,
                "operationCompanyLabel": legacy_label,
                "items": [{"articleCode": "110706", "quantity": 1, "partida": "P1"}],
            }

        compatibility = _validate_remito_orders([order("do-explicit", "MGBIO", "SEPID")])
        self.assertEqual(compatibility["companyKey"], "MGBIO")
        with self.assertRaisesRegex(Exception, "empresas Bejerman distintas"):
            _validate_remito_orders([order("do-1", "SEPID"), order("do-2", "MGBIO")])

    def test_demo_remito_profile_uses_rtn_demo_val(self):
        demo_profile = remito_profile_for_type("demo")

        self.assertEqual(demo_profile.comprobante_tipo, "RTN")
        self.assertEqual(demo_profile.point_of_sale, "00004")
        self.assertEqual(demo_profile.operation_code, "DEMO")
        self.assertEqual(demo_profile.deposit_code, "VAL")

    def test_only_rt_remito_profile_requires_billing(self):
        self.assertTrue(_remito_requires_billing({"type": "RT"}))
        for remito_type in ("RSS", "RTN", "RTA", "RDA", "RDN", ""):
            with self.subTest(remito_type=remito_type):
                self.assertFalse(_remito_requires_billing({"type": remito_type}))

    def test_delivery_comprobante_outbound_types_use_positive_sdk_quantity(self):
        cases = [
            ("RT", "sale", "MC", "VAL"),
            ("RTA", "rental", "ALQ", "STL"),
            ("RTN", "demo", "DEMO", "VAL"),
            ("RSS", "service_release", "REP", "STC"),
        ]
        for remito_type, delivery_type, operation, deposit in cases:
            with self.subTest(remito_type=remito_type):
                built = build_delivery_remito_comprobante(
                    {
                        "groupId": f"brg-{remito_type.lower()}",
                        "issueDate": "2026-06-25",
                        "customerCode": "ACU",
                        "customerName": "ACUMAR",
                        "sellerCode": "ADM",
                        "paymentTermCode": "30",
                        "orders": [
                            {
                                "orderNumber": f"OE-{remito_type}",
                                "deliveryType": delivery_type,
                                "equipmentSerial": f"SN-{remito_type}",
                                "items": [
                                    {
                                        "articleCode": "110706",
                                        "articleName": f"Equipo {remito_type}",
                                        "quantity": -2,
                                        "partida": f"SN-{remito_type}",
                                    }
                                ],
                            }
                        ],
                    },
                    delivery_remito_config(
                        {"type": remito_type, "operation": operation, "deposit": deposit, "pointOfSale": "00004"}
                    ),
                )

                comprobante = built["comprobante"]
                article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
                legend_lines = [item["Item_DescripArticulo"] for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "L"]
                self.assertEqual(comprobante["Comprobante_Tipo"], remito_type)
                self.assertEqual(article_lines[0]["Item_CantidadUM1"], 2)
                self.assertEqual(article_lines[0]["Item_CantidadUM2"], 0)
                self.assertEqual(article_lines[0]["Item_Deposito"], deposit)
                self.assertFalse(any("----------------" in line for line in legend_lines))
                if remito_type == "RSS":
                    self.assertEqual(legend_lines, ["Se entrega reparado"])
                elif remito_type == "RTA":
                    self.assertEqual(legend_lines, ["- Orden OE-RTA -", "Se entrega en Alquiler"])
                elif remito_type == "RT":
                    self.assertEqual(legend_lines, [])
                else:
                    self.assertEqual(legend_lines, [f"- Orden OE-{remito_type} -"])

    def test_delivery_comprobante_prints_bejerman_article_description(self):
        # El nexo entre los dos sistemas es el número de artículo: lo que se imprime
        # sale del maestro de Bejerman, no del texto que NEXORA guardó al armar el
        # pedido. En MGBIO ese texto guardado venía del catálogo de la otra empresa y
        # el remito salía nombrando un artículo distinto al que entregaba.
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-desc",
                "issueDate": "2026-08-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-DESC",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1206087",
                                "articleName": "Cánula nasal adulto BESMED",
                                "quantity": 1,
                            },
                            {
                                "articleCode": "9999999",
                                "articleName": "Artículo sin maestro",
                                "quantity": 1,
                            },
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00007"}),
            None,
            None,
            {"1206087": "Canula traq. c/Endocanula, c/Balon N° 7 OPTIMA BC"},
        )

        descriptions = [
            item["Item_DescripArticulo"] for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"
        ]
        self.assertEqual(
            descriptions,
            ["Canula traq. c/Endocanula, c/Balon N° 7 OPTIMA BC", "Artículo sin maestro"],
        )

    def test_build_article_description_map_keeps_only_real_descriptions(self):
        catalog = {
            "DatosJSON": [
                {"Articulo_Codigo": "1111001", "Articulo_Descripcion": "Calentador humidificador BMC H60-GII"},
                # Sin descripción: `map_article` cae al código, y eso no es un texto
                # para imprimir — el renglón se queda con el que ya traía.
                {"Articulo_Codigo": "1222009", "Articulo_Descripcion": ""},
            ]
        }

        self.assertEqual(
            build_article_description_map(catalog, ["1111001", "1222009", "1300000"]),
            {"1111001": "Calentador humidificador BMC H60-GII"},
        )

    def test_demo_delivery_comprobante_uses_rtn_profile(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-demo",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OED-1",
                        "deliveryType": "demo",
                        "equipmentSerial": "SN-DEMO",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Equipo demo",
                                "quantity": 1,
                                "partida": "SN-DEMO",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RTN", "operation": "DEMO", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RTN")
        self.assertEqual(comprobante["Comprobante_PtoVenta"], "00004")
        self.assertEqual(comprobante["Comprobante_TipoOperacion"], "DEMO")
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 1)
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")

    def test_delivery_comprobante_uses_bejerman_customer_fiscal_fields(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale",
                "issueDate": "2026-06-19",
                "customerCode": "NOV",
                "customerName": "NOVAMED",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1110001",
                                "articleName": "Poligrafo portatil",
                                "quantity": 1,
                                "partida": "S225DC14018",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {
                "Cliente_RazonSocial": "NOVAMED S.A.",
                "Cliente_NroDocumento": "30710659768",
                "Cliente_Provincia": "02",
                "Cliente_SitIVA": "1",
            },
        )

        comprobante = built["comprobante"]
        self.assertEqual(comprobante["Cliente_RazonSocial"], "NOVAMED S.A.")
        self.assertEqual(comprobante["Cliente_NroDocumento"], "30710659768")
        self.assertEqual(comprobante["Cliente_Provincia"], "02")
        self.assertEqual(comprobante["Cliente_SitIVA"], "1")
        legend_lines = [item["Item_DescripArticulo"] for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "L"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 1)
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")
        self.assertEqual(article_lines[0]["Item_Partida"], "S225DC14018")
        self.assertEqual(legend_lines, [])

    def test_delivery_comprobante_usa_iva_de_articulo_en_total_facturable(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale-vat",
                "issueDate": "2026-06-30",
                "customerCode": "LUI",
                "customerName": "MAJIRENA LUIS ALBERTO",
                "sellerCode": "EZE",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-00062",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1206001",
                                "articleName": "Canula nasal Airflow adulto p/poligrafo BMC WKT12",
                                "articleRequiresPartida": False,
                                "quantity": 10,
                                "unitPrice": 5950.41,
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {},
            {"1206001": {"salesVatType": "1", "salesVatCode": "01"}},
        )

        comprobante = built["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_ImporteTotal"], 59504.1)
        self.assertEqual(article_lines[0]["Item_Importe"], 59504.1)
        self.assertEqual(article_lines[0]["Item_TipoIVA"], "1")
        self.assertEqual(article_lines[0]["Item_TasaIVAInscrip"], 21)
        self.assertEqual(article_lines[0]["Item_ImporteIVAInscrip"], 12495.86)
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 71999.96)

    def test_delivery_rt_sitiva_6_discrimina_iva_igual_que_responsable_inscripto(self):
        # Regresión OE-00105 / ANDREOLI (monotributista, Cliente_SitIVA=6): el remito RT
        # 00023968 de MGBIO salió con el precio inflado por 1,21 y la alícuota en 0, y la
        # Factura A 00002-00018489 del 21/07/2026 se emitió con IVA 0,00. La situación de
        # IVA del cliente no cambia lo que va en el renglón del remito.
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-oe-00105",
                "issueDate": "2026-07-13",
                "customerCode": "EDU",
                "customerName": "ANDREOLI EDUARDO GABRIEL",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-00105",
                        "deliveryType": "sale",
                        "items": [
                            {"articleCode": "ART-1", "articleName": "Equipo 1", "quantity": 1, "unitPrice": 9438},
                            {"articleCode": "ART-2", "articleName": "Equipo 2", "quantity": 1, "unitPrice": 10280},
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00007"}),
            {"Cliente_SitIVA": "6"},
            {
                "ART-1": {"salesVatType": "1", "salesVatRate": 21},
                "ART-2": {"salesVatType": "1", "salesVatRate": 21},
            },
        )

        comprobante = built["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(built["amountMode"], "tax_discriminated")
        self.assertEqual([item["Item_PrecioUnitario"] for item in article_lines], [9438, 10280])
        self.assertEqual([item["Item_ImporteTotal"] for item in article_lines], [9438, 10280])
        self.assertEqual([item["Item_TasaIVAInscrip"] for item in article_lines], [21, 21])
        self.assertEqual([item["Item_ImporteIVAInscrip"] for item in article_lines], [1981.98, 2158.8])
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 23858.78)

    def test_delivery_rt_sitiva_1_conserva_neto_mas_iva_discriminado(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sitiva-1",
                "issueDate": "2026-07-13",
                "customerCode": "RI",
                "customerName": "Responsable inscripto",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [{"orderNumber": "OE-1", "deliveryType": "sale", "items": [{"articleCode": "ART", "quantity": 1, "unitPrice": 100}]}],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {"Cliente_SitIVA": "1"},
            {"ART": {"salesVatType": "1", "salesVatRate": 21}},
        )

        article = next(item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A")
        self.assertEqual(built["amountMode"], "tax_discriminated")
        self.assertEqual(article["Item_ImporteTotal"], 100)
        self.assertEqual(article["Item_ImporteIVAInscrip"], 21)
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 121)

    def test_prevalidacion_bloquea_encabezado_que_no_cierra(self):
        comprobante = {
            "Comprobante_ImporteTotal": 121.01,
            "Comprobante_Items": [
                {
                    "Item_Tipo": "A",
                    "Item_ImporteTotal": 100,
                    "Item_ImporteIVAInscrip": 21,
                    "Item_ImporteIVANoInscrip": 0,
                    "Item_ImporteIVANoGravado": 0,
                }
            ],
        }

        with self.assertRaisesRegex(BejermanSdkResponseError, "DELIVERY_REMITO_AMOUNT_MISMATCH"):
            _validate_delivery_comprobante_amounts(comprobante)

    def test_delivery_comprobante_prioriza_iva_de_bejerman_sobre_item_local(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale-vat-catalog",
                "issueDate": "2026-07-08",
                "customerCode": "RES",
                "customerName": "RESPIRAR S.A.",
                "sellerCode": "EZE",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-IVA-CATALOG",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1206001",
                                "articleName": "Canula nasal",
                                "articleRequiresPartida": False,
                                "quantity": 1,
                                "unitPrice": 100,
                                "salesVatRate": 0,
                                "salesVatCode": "04",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {},
            {"1206001": {"salesVatType": "1", "salesVatCode": "01", "salesVatRate": 21}},
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_TasaIVAInscrip"], 21)
        self.assertEqual(article_lines[0]["Item_ImporteIVAInscrip"], 21)
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 121)

    def test_delivery_comprobante_envia_iva_de_bejerman_con_precio_cero(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale-vat-zero-price",
                "issueDate": "2026-07-13",
                "customerCode": "RES",
                "customerName": "RESPIRAR S.A.",
                "sellerCode": "EZE",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-IVA-SIN-PRECIO",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1206001",
                                "articleName": "Cánula nasal",
                                "articleRequiresPartida": False,
                                "quantity": 10,
                                "unitPrice": 0,
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {"Cliente_SitIVA": "1"},
            {"1206001": {"salesVatType": "1", "salesVatCode": "01", "salesVatRate": 21}},
        )

        article_line = next(item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A")
        self.assertEqual(article_line["Item_TipoIVA"], "1")
        self.assertEqual(article_line["Item_TasaIVAInscrip"], 21)
        self.assertEqual(article_line["Item_ImporteTotal"], 0)
        self.assertEqual(article_line["Item_ImporteIVAInscrip"], 0)
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 0)

    def test_delivery_comprobante_soporta_iva_diez_y_medio(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-sale-vat-105",
                "issueDate": "2026-06-30",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-IVA-105",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1300001",
                                "articleName": "Articulo gravado al 10.5",
                                "articleRequiresPartida": False,
                                "quantity": 1,
                                "unitPrice": 100,
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
            {},
            {"1300001": {"salesVatType": "1", "salesVatCode": "03"}},
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_TasaIVAInscrip"], 10.5)
        self.assertEqual(article_lines[0]["Item_ImporteIVAInscrip"], 10.5)
        self.assertEqual(article_lines[0]["Item_ImporteTotal"], 100)
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 110.5)

    def test_delivery_comprobante_expands_multiple_partidas(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 3,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "STL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual([item["Item_CantidadUM1"] for item in article_lines], [1, 1])
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["P1", "P2"])
        self.assertEqual([item["Item_Deposito"] for item in article_lines], ["STL", "VAL"])
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 6)

    def test_delivery_comprobante_omits_partida_for_non_partida_article(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-29",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-FILTRO",
                        "deliveryType": "sale",
                        "equipmentSerial": "SN-NO-DEBE-USARSE",
                        "items": [
                            {
                                "articleCode": "1208010",
                                "articleName": "Filtro de aire",
                                "articleRequiresPartida": False,
                                "quantity": 10,
                                "unitPrice": 1,
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_Partida"], " ")
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 10)

    def test_delivery_comprobante_no_duplica_cantidad_en_um2(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-23",
                "customerCode": "ETI",
                "customerName": "ETICA S.A.",
                "sellerCode": "TOM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-00027",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "1203006",
                                "articleName": "Tubuladura descartable c/punta de goma 285/5064",
                                "quantity": 10,
                                "unitPrice": 16325,
                                "partidas": [
                                    {"partida": "24G0424FAX", "assignedQuantity": 10, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00004"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 10)
        self.assertEqual(article_lines[0]["Item_CantidadUM2"], 0)
        self.assertEqual(article_lines[0]["Item_Deposito"], "VAL")
        self.assertEqual(article_lines[0]["Item_Partida"], "24G0424FAX")

    def test_delivery_comprobante_envia_descuento_por_item(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OE-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 100,
                                "discountPercent": 10,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "STL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        comprobante = built["comprobante"]
        items = comprobante["Comprobante_Items"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        article_indexes = [index for index, item in enumerate(items) if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Moneda"], "")
        self.assertEqual(comprobante["Comprobante_TipoCambio"], "")
        self.assertEqual(comprobante["Comprobante_CotizacionCambio"], 0)
        self.assertEqual([item["Item_PrecioUnitario"] for item in article_lines], [100, 100])
        self.assertEqual([item["Item_TasaDescPorItem"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteDescPorLinea"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_Importe"] for item in article_lines], [90, 90])
        self.assertEqual([item["Item_ImporteTotal"] for item in article_lines], [90, 90])
        self.assertEqual(items[article_indexes[0] + 1]["Item_Tipo"], "L")
        self.assertEqual(items[article_indexes[1] + 1]["Item_Tipo"], "L")
        self.assertEqual(
            items[article_indexes[0] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: $ 90,00",
        )
        self.assertEqual(
            items[article_indexes[1] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: $ 90,00",
        )
        self.assertEqual(built["comprobante"]["Comprobante_ImporteTotal"], 180)

    def test_delivery_comprobante_usd_no_fija_moneda_para_facturar_en_pesos(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-usd",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "exchangeRate": "1200",
                "orders": [
                    {
                        "orderNumber": "OE-USD-1",
                        "deliveryType": "sale",
                        "items": [
                            {
                                "articleCode": "110706",
                                "articleName": "Filtro",
                                "quantity": 2,
                                "unitPrice": 100,
                                "priceCurrency": "USD",
                                "discountPercent": 10,
                                "partidas": [
                                    {"partida": "P1", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                    {"partida": "P2", "assignedQuantity": 1, "stockDepositCode": "VAL"},
                                ],
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
        )

        comprobante = built["comprobante"]
        items = comprobante["Comprobante_Items"]
        article_lines = [item for item in items if item["Item_Tipo"] == "A"]
        article_indexes = [index for index, item in enumerate(items) if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Moneda"], "")
        self.assertEqual(comprobante["Comprobante_TipoCambio"], "")
        self.assertEqual(comprobante["Comprobante_CotizacionCambio"], 0)
        self.assertEqual([item["Item_PrecioUnitario"] for item in article_lines], [100, 100])
        self.assertEqual([item["Item_TasaDescPorItem"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteDescPorLinea"] for item in article_lines], [10, 10])
        self.assertEqual([item["Item_ImporteTotal"] for item in article_lines], [90, 90])
        self.assertEqual(
            items[article_indexes[0] + 1]["Item_DescripArticulo"],
            "Descuento aplicado: 10% - Precio final con descuento: U$S 90,00",
        )
        self.assertEqual(comprobante["Comprobante_ImporteTotal"], 180)

    def test_delivery_comprobante_usd_requiere_cotizacion_nexora(self):
        with self.assertRaises(BejermanSdkResponseError) as ctx:
            build_delivery_remito_comprobante(
                {
                    "groupId": "brg-usd-config",
                    "issueDate": "2026-06-12",
                    "customerCode": "ACU",
                    "customerName": "ACUMAR",
                    "sellerCode": "ADM",
                    "paymentTermCode": "30",
                    "exchangeRate": "",
                    "orders": [
                        {
                            "orderNumber": "OE-USD-CONFIG",
                            "deliveryType": "sale",
                            "items": [
                                {
                                    "articleCode": "110706",
                                    "articleName": "Filtro",
                                    "quantity": 1,
                                    "unitPrice": 100,
                                    "priceCurrency": "USD",
                                    "partida": "P1",
                                }
                            ],
                        }
                    ],
                },
                delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
            )

        self.assertEqual(str(ctx.exception), "DELIVERY_REMITO_EXCHANGE_RATE_REQUIRED")

    def test_delivery_comprobante_no_permite_monedas_mixtas_por_item(self):
        with self.assertRaises(BejermanSdkResponseError) as ctx:
            build_delivery_remito_comprobante(
                {
                    "groupId": "brg-mixed",
                    "issueDate": "2026-06-12",
                    "customerCode": "ACU",
                    "customerName": "ACUMAR",
                    "sellerCode": "ADM",
                    "paymentTermCode": "30",
                    "orders": [
                        {
                            "orderNumber": "OE-MIXED-1",
                            "deliveryType": "sale",
                            "items": [
                                {
                                    "articleCode": "110706",
                                    "articleName": "Filtro",
                                    "quantity": 1,
                                    "unitPrice": 100,
                                    "priceCurrency": "ARS",
                                    "partida": "P1",
                                },
                                {
                                    "articleCode": "110707",
                                    "articleName": "Filtro USD",
                                    "quantity": 1,
                                    "unitPrice": 10,
                                    "priceCurrency": "USD",
                                    "partida": "P2",
                                },
                            ],
                        }
                    ],
                },
                delivery_remito_config({"type": "RT", "operation": "MC", "deposit": "VAL", "pointOfSale": "00002"}),
            )

        self.assertEqual(str(ctx.exception), "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")

    def test_service_release_uses_resolution_delivery_legend(self):
        cases = [
            ("reparado", "Se entrega reparado"),
            ("no_reparado", "Se entrega sin reparar"),
            ("controlado_sin_defecto", "Se entrega controlado sin defecto"),
            ("no_se_encontro_falla", "Se entrega controlado sin defecto"),
        ]
        for resolution, expected in cases:
            with self.subTest(resolution=resolution):
                built = build_delivery_remito_comprobante(
                    {
                        "groupId": "brg-test",
                        "issueDate": "2026-06-12",
                        "customerCode": "ACU",
                        "customerName": "ACUMAR",
                        "sellerCode": "ADM",
                        "paymentTermCode": "30",
                        "orders": [
                            {
                                "orderNumber": "OES-1",
                                "deliveryType": "service_release",
                                "sourceReference": "OS 29381",
                                "serviceResolution": resolution,
                                "equipmentModel": "CPAP BMC",
                                "equipmentSerial": "SN-123",
                                "items": [
                                    {
                                        "articleCode": "110706",
                                        "articleName": "Servicio tecnico",
                                        "quantity": 1,
                                        "partida": "SN-123",
                                    }
                                ],
                            }
                        ],
                    },
                    delivery_remito_config({"type": "RSS", "operation": "REP", "deposit": "STC", "pointOfSale": "00004"}),
                )

                legend_lines = [
                    item["Item_DescripArticulo"]
                    for item in built["comprobante"]["Comprobante_Items"]
                    if item["Item_Tipo"] == "L"
                ]
                self.assertEqual(legend_lines, [f"{expected} - OS 29381 -"])

    def test_service_release_rss_article_line_uses_equipment_detail(self):
        equipment_detail = "OXÍMETRO DE PULSO | Nellcor | N-595 - OS-29381 - N° interno (MG) MG 6697"
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-06-12",
                "customerCode": "ACU",
                "customerName": "ACUMAR",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OES-1",
                        "deliveryType": "service_release",
                        "sourceReference": "OS 29381",
                        "serviceResolution": "reparado",
                        "equipmentModel": "OXÍMETRO DE PULSO | Nellcor | N-595",
                        "equipmentSerial": "G0588065",
                        "equipmentInternalNumber": "MG 6697",
                        "items": [
                            {
                                "articleCode": "11",
                                "articleName": equipment_detail,
                                "description": "Liberación servicio técnico",
                                "quantity": 1,
                                "partida": "G0588065",
                            }
                        ],
                    }
                ],
            },
            delivery_remito_config({"type": "RSS", "operation": "REP", "deposit": "STC", "pointOfSale": "00004"}),
        )

        article_lines = [item for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        legend_lines = [item["Item_DescripArticulo"] for item in built["comprobante"]["Comprobante_Items"] if item["Item_Tipo"] == "L"]
        self.assertEqual(article_lines[0]["Item_CantidadUM1"], 1)
        self.assertEqual(article_lines[0]["Item_DescripArticulo"], "OXÍMETRO DE PULSO | Nellcor | N-595 - N° interno (MG) MG 6697")
        self.assertEqual(legend_lines, ["Se entrega reparado - OS 29381 -"])
        self.assertNotIn("Liberación", article_lines[0]["Item_DescripArticulo"])
        self.assertNotIn("OS-29381", article_lines[0]["Item_DescripArticulo"])
        self.assertNotIn("G0588065", article_lines[0]["Item_DescripArticulo"])

    def test_service_release_rss_usa_leyendas_sin_separador(self):
        built = build_delivery_remito_comprobante(
            {
                "groupId": "brg-test",
                "issueDate": "2026-07-02",
                "customerCode": "TMD",
                "customerName": "TERAPIAS MEDICAS DOMICILIARIAS S.A.",
                "sellerCode": "ADM",
                "paymentTermCode": "30",
                "orders": [
                    {
                        "orderNumber": "OS-29318",
                        "deliveryType": "service_release",
                        "sourceReference": "OS 29318",
                        "serviceResolution": "reparado",
                        "items": [
                            {
                                "ingresoId": 29318,
                                "articleCode": "1111003",
                                "articleName": "CALENTADOR HUMIDIFICADOR | Marbel | C-500A - OS-29318",
                                "quantity": 1,
                                "partida": "D5#1615",
                            }
                        ],
                    },
                    {
                        "orderNumber": "OS-29319",
                        "deliveryType": "service_release",
                        "sourceReference": "OS 29319",
                        "serviceResolution": "no_se_encontro_falla",
                        "items": [
                            {
                                "ingresoId": 29319,
                                "articleCode": "1111003",
                                "articleName": "CALENTADOR HUMIDIFICADOR | Marbel | C-500A - OS-29319",
                                "quantity": 1,
                                "partida": "D5#1642",
                            }
                        ],
                    },
                ],
            },
            delivery_remito_config({"type": "RSS", "operation": "REP", "deposit": "STC", "pointOfSale": "00004"}),
        )

        items = built["comprobante"]["Comprobante_Items"]

        self.assertEqual([item["Item_Tipo"] for item in items], ["L", "A", "L", "A"])
        self.assertEqual(items[0]["Item_DescripArticulo"], "Se entrega reparado - OS 29318 -")
        self.assertEqual(items[1]["Item_DescripArticulo"], "CALENTADOR HUMIDIFICADOR | Marbel | C-500A")
        self.assertEqual(items[2]["Item_DescripArticulo"], "Se entrega controlado sin defecto - OS 29319 -")
        self.assertEqual(items[3]["Item_DescripArticulo"], "CALENTADOR HUMIDIFICADOR | Marbel | C-500A")
        self.assertEqual(items[1]["Item_Deposito"], "STC")
        self.assertEqual(items[3]["Item_Deposito"], "STC")
        self.assertEqual(items[1]["Item_CantidadUM2"], 0)
        self.assertEqual(items[3]["Item_CantidadUM2"], 0)
        self.assertNotIn("OS-", items[1]["Item_DescripArticulo"])
        self.assertNotIn("OS-", items[3]["Item_DescripArticulo"])

    def test_print_wait_page_does_not_force_pdf_accept_header(self):
        html = _remito_print_wait_page("brg-test", "RT")

        self.assertIn("/api/ordenes-entrega/remito-bejerman/brg-test/pdf/", html)
        self.assertIn("Preparando el remito RT", html)
        self.assertIn('"documentName": "remito RT"', html)
        self.assertNotIn("Preparando RSS", html)
        self.assertNotIn("Accept: 'application/pdf'", html)
        self.assertIn("Retry-After", html)
        self.assertIn("retry_after_ms", html)
        self.assertIn("REMITO_DOCUMENT_NOT_FOUND", html)
        self.assertIn("NEXORA volverá a comprobarlo automáticamente", html)
        self.assertIn("Cerrar pestaña", html)
        self.assertNotIn("maxAttempts", html)
        self.assertNotIn("maxWaitMs", html)
        self.assertNotIn("lookupMissRetryLimit", html)
        self.assertNotIn("shouldStopWaiting", html)
        self.assertIn("El comprobante ya está emitido en Bejerman", html)
        self.assertIn("Buscando el PDF en Bejerman", html)
        self.assertNotIn("Reintente en unos segundos", html)
        self.assertNotIn("no es necesario volver a emitir el remito", html)
        self.assertNotIn("Intento ", html)
        self.assertNotIn("Esperando PDF (", html)
        self.assertNotIn(chr(0x00C3), html)


class DeliveryOrderPropietarioOcasionalTests(TestCase):
    @classmethod
    def setUpClass(cls):
        # get_delivery_order lee el codigo Bejerman vivo del cliente (multi-empresa SEPID/MGBIO).
        call_command("apply_customer_bejerman_schema", verbosity=0)
        super().setUpClass()

    def setUp(self):
        with connection.cursor() as cur:
            # 'PTL' a proposito: en produccion la fila Particular quedo con un codigo cargado que no
            # existe en Bejerman. Con NULL aca el test pasaba en verde mientras el circuito real
            # fallaba, porque el camino de cliente ocasional esta detras de guardas
            # `if not customer_code`.
            cur.execute(
                "INSERT INTO customers(cod_empresa, razon_social) VALUES (%s,%s) RETURNING id",
                ["PTL", "Particular"],
            )
            self.particular_customer_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO locations(nombre) VALUES (%s) RETURNING id", ["Taller Propietario"])
            self.taller_id = int(cur.fetchone()[0])
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s) RETURNING id", ["ResMed Propietario"])
            self.marca_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s) RETURNING id",
                [self.marca_id, "AirSense Propietario", "CPAP", ""],
            )
            self.model_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, alquilado, mg_estado)
                VALUES (%s,%s,%s,%s,FALSE,'activo')
                RETURNING id
                """,
                [self.particular_customer_id, self.marca_id, self.model_id, "SN-PROPIETARIO-001"],
            )
            self.device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_creacion, ubicacion_id,
                    empresa_bejerman, empresa_facturar, propietario_nombre, propietario_doc
                )
                VALUES (%s,'liberado','reparación',CURRENT_TIMESTAMP,%s,'SEPID','SEPID',%s,%s)
                RETURNING id
                """,
                [self.device_id, self.taller_id, "Juan Perez", "30710698"],
            )
            self.ingreso_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO delivery_orders(id, order_number, delivery_type, ingreso_id, customer_id, bejerman_customer_code, customer_name)
                VALUES (%s,%s,'service_release',%s,%s,NULL,%s)
                """,
                ["do-propietario-test", "OS-PROPIETARIO-TEST", self.ingreso_id, self.particular_customer_id, "Particular"],
            )

    def _insert_ingreso_particular(self, *, estado, propietario_doc="30710698", serial="SN-EXTRA"):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, alquilado, mg_estado)
                VALUES (%s,%s,%s,%s,FALSE,'activo') RETURNING id
                """,
                [self.particular_customer_id, self.marca_id, self.model_id, serial],
            )
            device_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ingresos(
                    device_id, estado, motivo, fecha_creacion, ubicacion_id,
                    empresa_bejerman, empresa_facturar, propietario_nombre, propietario_doc
                )
                VALUES (%s,%s,'reparación',CURRENT_TIMESTAMP,%s,'SEPID','SEPID',%s,%s)
                RETURNING id
                """,
                [device_id, estado, self.taller_id, "Juan Perez", propietario_doc],
            )
            return int(cur.fetchone()[0])

    def test_un_segundo_equipo_del_mismo_particular_mantiene_la_ficha_ocupada(self):
        # El caso que motivo la salvaguarda: trae dos equipos y se entrega uno solo.
        self._insert_ingreso_particular(estado="reparado", serial="SN-PROPIETARIO-002")

        self.assertTrue(has_other_open_ingresos("30710698", exclude_ingreso_id=self.ingreso_id))
        # El documento local puede venir con puntos; el maestro lo guarda sin nada.
        self.assertTrue(has_other_open_ingresos("30.710.698", exclude_ingreso_id=self.ingreso_id))

    def test_el_ingreso_que_se_esta_entregando_no_cuenta_como_abierto(self):
        self.assertFalse(has_other_open_ingresos("30710698", exclude_ingreso_id=self.ingreso_id))

    def test_los_estados_terminales_no_cuentan_como_abiertos(self):
        for estado in ("entregado", "vendido_entregado", "baja"):
            with self.subTest(estado=estado):
                self._insert_ingreso_particular(estado=estado, serial=f"SN-TERM-{estado}")
        self.assertFalse(has_other_open_ingresos("30710698", exclude_ingreso_id=self.ingreso_id))

    def test_liberado_y_pendiente_de_entrega_siguen_contando_como_abiertos(self):
        # El equipo esta listo pero todavia en la casa: si se deshabilita la ficha ahi, la salida
        # de ese equipo se queda sin cliente habilitado.
        for estado in ("liberado", "vendido_pendiente_entrega"):
            with self.subTest(estado=estado):
                otro = self._insert_ingreso_particular(estado=estado, serial=f"SN-ABIERTO-{estado}")
                self.assertTrue(has_other_open_ingresos("30710698", exclude_ingreso_id=self.ingreso_id))
                with connection.cursor() as cur:
                    cur.execute("UPDATE ingresos SET estado='entregado' WHERE id=%s", [otro])

    def test_otro_documento_no_bloquea(self):
        self._insert_ingreso_particular(
            estado="reparado", propietario_doc="27263312258", serial="SN-OTRO-DOC"
        )

        self.assertFalse(has_other_open_ingresos("30710698", exclude_ingreso_id=self.ingreso_id))

    def test_get_delivery_order_expone_datos_del_propietario_ocasional(self):
        order = get_delivery_order("do-propietario-test")

        self.assertEqual(order["propietarioNombre"], "Juan Perez")
        self.assertEqual(order["propietarioDoc"], "30710698")
        self.assertEqual(order["bejermanCustomerCode"], "")

    def test_list_delivery_orders_expone_datos_del_propietario_ocasional(self):
        # La respuesta de emision reusa el listado: si ahi faltan los datos del propietario,
        # el panel no puede distinguir un particular de un cliente sin mapear.
        items = list_delivery_orders({"limit": 200})["items"]
        order = next(item for item in items if item["id"] == "do-propietario-test")

        self.assertEqual(order["propietarioNombre"], "Juan Perez")
        self.assertEqual(order["propietarioDoc"], "30710698")
