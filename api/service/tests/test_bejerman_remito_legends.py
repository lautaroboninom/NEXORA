from unittest import skipUnless
from unittest.mock import patch

import fitz
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from service.bejerman_delivery import (
    _assert_remito_payload_print_limits,
    _order_print_line_count,
    _partition_remito_orders_for_printing,
    _remito_print_item_chunks,
    _split_remito_orders_for_printing,
)
from service.bejerman_remito_legends import (
    delivery_article_legend_lines,
    delivery_order_general_legend_lines,
    format_os_badge,
    format_rental_article_accessories_legend,
    service_release_item_legend,
    wrap_remito_legend_lines,
)
from service.bejerman_remito_pdf import render_remito_pdf
from service.bejerman_sdk import build_service_ingress_comprobante, build_delivery_remito_comprobante, delivery_remito_config
from service.delivery_orders import create_delivery_order, get_delivery_order, update_delivery_order
from service.tests.test_bejerman_remito_contracts import _payload, CUSTOMER_FIELDS


class BejermanRemitoLegendTests(SimpleTestCase):
    def test_rda_une_motivo_e_interno_en_individual_lote_y_compacto(self):
        for mode in ("individual", "lote", "compacto"):
            with self.subTest(mode=mode):
                payload = _payload()
                payload["documentProfile"].update(type="RDA", key="rda", operation="ALQ")
                payload["equipment"].update(internalNumber="MG 7652", repairReason="Baja alquiler")
                if mode != "individual":
                    payload["equipments"] = [payload["equipment"], {
                        **payload["equipment"], "internalNumber": "MG 1234", "serial": "OTRO-NS",
                    }]
                payload["compactServiceIngressLegends"] = mode == "compacto"
                built = build_service_ingress_comprobante(payload, CUSTOMER_FIELDS)
                items = built["comprobante"]["Comprobante_Items"]
                legends = [i["Item_DescripArticulo"] for i in items if i["Item_Tipo"] == "L"]
                self.assertEqual(legends.count("Motivo: Baja alquiler (MG 7652)"), 1)
                self.assertFalse(any(line.startswith("Interno:") for line in legends))
                if mode != "individual":
                    self.assertEqual(legends.count("Motivo: Baja alquiler (MG 1234)"), 1)
                with fitz.open(stream=render_remito_pdf(built["comprobante"], company_key="SEPID"), filetype="pdf") as pdf:
                    self.assertIn("Motivo: Baja alquiler (MG 7652)", pdf[0].get_text())

    def test_rda_sin_interno_no_imprime_parentesis_vacios(self):
        payload = _payload()
        payload["documentProfile"].update(type="RDA", key="rda", operation="ALQ")
        payload["equipment"].update(internalNumber="", repairReason="Baja alquiler")
        items = build_service_ingress_comprobante(payload, CUSTOMER_FIELDS)["comprobante"]["Comprobante_Items"]
        self.assertIn("Motivo: Baja alquiler", [i["Item_DescripArticulo"] for i in items if i["Item_Tipo"] == "L"])

    def test_rta_imprime_leyenda_general_y_accesorios_por_articulo(self):
        request = {
            "groupId": "rta-leyendas-test", "issueDate": "2026-09-08",
            "customerCode": "TEST", "customerName": "Cliente de prueba",
            "sellerCode": "ADM", "paymentTermCode": "30",
            "orders": [{
                "orderNumber": "OE-TEST", "deliveryType": "rental",
                "remitoGeneralLegend": "Se entrega en alquiler",
                "items": [
                    {
                        "articleCode": "1115003", "articleName": "Concentrador de oxígeno",
                        "remitoArticleLegend": "Vaso humidificador y cánula nasal",
                        "quantity": 2, "unitPrice": 0,
                        "partidas": [
                            {"partida": "SERIE-A", "quantity": 1, "internalNumber": "MG 7652"},
                            {"partida": "SERIE-B", "quantity": 1, "internalNumber": "MG 1234"},
                        ],
                    },
                    {
                        "articleCode": "2225001", "articleName": "Aspirador de secreciones",
                        "remitoArticleLegend": "Frasco recolector y tubo de aspiración",
                        "quantity": 1, "unitPrice": 0,
                        "partidas": [
                            {"partida": "SERIE-C", "quantity": 1, "internalNumber": "MG 9999"},
                        ],
                    },
                ],
            }],
        }
        config = delivery_remito_config({"type": "RTA", "operation": "ALQ", "deposit": "STL"})
        built = build_delivery_remito_comprobante(request, config)
        items = built["comprobante"]["Comprobante_Items"]
        legends = [i["Item_DescripArticulo"] for i in items if i["Item_Tipo"] == "L"]

        # Leyenda general arriba de todo (1 sola vez para todo el remito)
        self.assertEqual(legends[0], "- Orden OE-TEST -")
        self.assertEqual(legends[1], "Se entrega en alquiler")
        self.assertEqual(legends.count("Se entrega en alquiler"), 1)

        # Leyenda por artículo antes de cada grupo de artículos (dividida a máx 50 caracteres)
        self.assertIn("Los siguientes equipos se entregan con: Vaso", legends)
        self.assertIn("humidificador y cánula nasal", legends)
        self.assertIn("Los siguientes equipos se entregan con: Frasco", legends)
        self.assertIn("recolector y tubo de aspiración", legends)
        for leg in legends:
            self.assertLessEqual(len(leg), 50)

        # No hay leyendas repetidas por cada partida
        self.assertFalse(any("MG 7652" in leg for leg in legends))
        self.assertFalse(any("MG 1234" in leg for leg in legends))

        # En PDF se visualizan correctamente
        with fitz.open(stream=render_remito_pdf(built["comprobante"], company_key="SEPID"), filetype="pdf") as pdf:
            import re
            pdf_text = re.sub(r"\s+", " ", pdf[0].get_text())
            self.assertIn("Se entrega en alquiler", pdf_text)
            self.assertIn("Los siguientes equipos se entregan con: Vaso humidificador y cánula nasal", pdf_text)
            self.assertIn("Los siguientes equipos se entregan con: Frasco recolector y tubo de aspiración", pdf_text)

    def test_formatea_la_os_como_badge_legible(self):
        self.assertEqual(format_os_badge(13), "- OS 00013 -")
        self.assertEqual(format_os_badge("OS-29381"), "- OS 29381 -")

    def test_rss_conserva_la_resolucion_y_destaca_la_os(self):
        line = service_release_item_legend(
            {"serviceResolution": "no_se_encontro_falla", "sourceReference": "OS 29381"},
            {"articleCode": "1115003"},
        )

        self.assertEqual(line, "Se entrega controlado sin defecto - OS 29381 -")

    @override_settings(BEJERMAN_RIS_UPDATE_STOCK="0")
    def test_pdf_del_ris_lote_no_duplica_serie_y_mantiene_la_columna(self):
        built = build_service_ingress_comprobante(
            {
                "requestId": "leyenda-ris-13",
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

        pdf_bytes = render_remito_pdf(built["comprobante"], company_key="SEPID")
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            rendered_text = document[0].get_text()

        self.assertIn("- OS 00013 -", rendered_text)
        self.assertIn("Interno: MG 9901", rendered_text)
        self.assertIn("Motivo: reparación alquiler", rendered_text)
        self.assertIn("MZJ5S121916", rendered_text)
        self.assertNotIn("N/S:", rendered_text)
        self.assertNotIn("------------------------------", rendered_text)

    def test_rta_print_split_repite_leyenda_general_y_accesorios_en_continuacion(self):
        # 25 equipos de un artículo con accesorios en RTA (límite 21 líneas).
        # Cabecera: 2 líneas ("- Orden OE-SPLIT-RTA -" + "Se entrega en Alquiler").
        # Leyenda accesorios: 2 líneas ("Los siguientes equipos se entregan con: Vaso" + "humidificador y cánula nasal").
        # Quedan 17 líneas: 21 - 2 - 2 = 17 equipos en remito 1 (total 21 líneas).
        # Remito 2: repite cabecera (2) + accesorios (2) + 8 equipos restantes = 12 líneas.
        order = {
            "id": "do-split-rta",
            "orderNumber": "OE-SPLIT-RTA",
            "deliveryType": "rental",
            "remitoGeneralLegend": "Se entrega en Alquiler",
            "items": [
                {
                    "id": "doi-1",
                    "articleCode": "1115003",
                    "articleName": "Concentrador de oxígeno",
                    "remitoArticleLegend": "Vaso humidificador y cánula nasal",
                    "quantity": 25,
                    "unitPrice": 0,
                    "stockDepositCode": "STL",
                    "partidas": [
                        {
                            "id": f"partida-{index}",
                            "partida": f"E238D50{index:03d}",
                            "assignedQuantity": 1,
                            "stockDepositCode": "STL",
                        }
                        for index in range(1, 26)
                    ],
                }
            ],
        }
        config = delivery_remito_config({"type": "RTA", "operation": "ALQ", "deposit": "STL"})

        chunks = _remito_print_item_chunks(order)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0][0]["partidas"]), 17)
        self.assertEqual(len(chunks[1][0]["partidas"]), 8)

        split_orders = _split_remito_orders_for_printing([order], None)
        self.assertEqual(len(split_orders), 2)

        batches = _partition_remito_orders_for_printing(split_orders)
        self.assertEqual(len(batches), 2)

        for batch_index, batch in enumerate(batches, start=1):
            built = build_delivery_remito_comprobante(
                {
                    "groupId": f"brg-split-{batch_index}",
                    "issueDate": "2026-09-10",
                    "customerCode": "ACU",
                    "customerName": "ACUMAR",
                    "sellerCode": "ADM",
                    "paymentTermCode": "30",
                    "orders": batch,
                },
                config,
            )
            comprobante = built["comprobante"]
            items = comprobante["Comprobante_Items"]
            _assert_remito_payload_print_limits("rental", comprobante)
            legends = [i["Item_DescripArticulo"] for i in items if i["Item_Tipo"] == "L"]

            # Cada remito tiene cabecera completa
            self.assertEqual(legends[0], "- Orden OE-SPLIT-RTA -")
            self.assertEqual(legends[1], "Se entrega en Alquiler")

            # Cada remito repite la leyenda de accesorios para el grupo
            self.assertIn("Los siguientes equipos se entregan con: Vaso", legends)
            self.assertIn("humidificador y cánula nasal", legends)

            if batch_index == 1:
                self.assertEqual(len(items), 21)
                article_lines = [i for i in items if i["Item_Tipo"] == "A"]
                self.assertEqual(len(article_lines), 17)
            else:
                self.assertEqual(len(items), 12)
                article_lines = [i for i in items if i["Item_Tipo"] == "A"]
                self.assertEqual(len(article_lines), 8)

    def test_rta_print_split_no_deja_leyenda_de_accesorios_huerfana_al_final_de_hoja(self):
        # Renglón 1: 18 equipos de art 1115003 con accesorios de 1 sola línea (llena remito 1 con 2 cabeceras + 1 leyenda + 18 items = 21).
        # Renglón 2: 2 equipos de art 2225001 con accesorios. No entra ni la leyenda ni el ítem en remito 1.
        order = {
            "id": "do-orphan-check",
            "orderNumber": "OE-ORPHAN",
            "deliveryType": "rental",
            "remitoGeneralLegend": "Se entrega en Alquiler",
            "items": [
                {
                    "id": "doi-1",
                    "articleCode": "1115003",
                    "articleName": "Concentrador de oxígeno",
                    "remitoArticleLegend": "Vaso",
                    "quantity": 18,
                    "unitPrice": 0,
                    "stockDepositCode": "STL",
                    "partidas": [
                        {"id": f"p1-{n}", "partida": f"P1-{n}", "assignedQuantity": 1, "stockDepositCode": "STL"}
                        for n in range(1, 19)
                    ],
                },
                {
                    "id": "doi-2",
                    "articleCode": "2225001",
                    "articleName": "Aspirador de secreciones",
                    "remitoArticleLegend": "Tubo",
                    "quantity": 2,
                    "unitPrice": 0,
                    "stockDepositCode": "STL",
                    "partidas": [
                        {"id": f"p2-{n}", "partida": f"P2-{n}", "assignedQuantity": 1, "stockDepositCode": "STL"}
                        for n in range(1, 3)
                    ],
                },
            ],
        }
        config = delivery_remito_config({"type": "RTA", "operation": "ALQ", "deposit": "STL"})

        chunks = _remito_print_item_chunks(order)
        self.assertEqual(len(chunks), 2)
        # Remito 1 tiene solo artículos del primer renglón
        self.assertEqual(len(chunks[0]), 1)
        self.assertEqual(chunks[0][0]["articleCode"], "1115003")
        self.assertEqual(len(chunks[0][0]["partidas"]), 18)

        # Remito 2 tiene el segundo renglón
        self.assertEqual(len(chunks[1]), 1)
        self.assertEqual(chunks[1][0]["articleCode"], "2225001")
        self.assertEqual(len(chunks[1][0]["partidas"]), 2)

        split_orders = _split_remito_orders_for_printing([order], None)
        batches = _partition_remito_orders_for_printing(split_orders)

        built_1 = build_delivery_remito_comprobante(
            {"groupId": "b1", "issueDate": "2026-09-10", "customerCode": "ACU", "sellerCode": "ADM", "orders": batches[0]},
            config,
        )
        built_2 = build_delivery_remito_comprobante(
            {"groupId": "b2", "issueDate": "2026-09-10", "customerCode": "ACU", "sellerCode": "ADM", "orders": batches[1]},
            config,
        )

        legends_1 = [i["Item_DescripArticulo"] for i in built_1["comprobante"]["Comprobante_Items"] if i["Item_Tipo"] == "L"]
        legends_2 = [i["Item_DescripArticulo"] for i in built_2["comprobante"]["Comprobante_Items"] if i["Item_Tipo"] == "L"]

        self.assertIn("Los siguientes equipos se entregan con: Vaso", legends_1)
        self.assertNotIn("Tubo", legends_1)

        self.assertIn("Los siguientes equipos se entregan con: Tubo", legends_2)
        self.assertNotIn("Vaso", legends_2)

    def test_formato_leyendas_accesorios_y_general_variantes(self):
        # Accesorios sin prefijo
        self.assertEqual(
            format_rental_article_accessories_legend("Máscara y arnés"),
            "Los siguientes equipos se entregan con: Máscara y arnés",
        )
        # Accesorios con prefijo existente en singular o plural no se duplica
        self.assertEqual(
            format_rental_article_accessories_legend("Los siguientes equipos se entregan con: Fuente"),
            "Los siguientes equipos se entregan con: Fuente",
        )
        self.assertEqual(
            format_rental_article_accessories_legend("Los siguientes equipos se entrega con: Fuente"),
            "Los siguientes equipos se entrega con: Fuente",
        )
        # Accesorios multilinea
        self.assertEqual(
            format_rental_article_accessories_legend("Bolso\nCable\nFiltro"),
            "Los siguientes equipos se entregan con: Bolso, Cable, Filtro",
        )
        # Accesorios vacíos o sólo con espacios no emiten leyenda
        self.assertEqual(format_rental_article_accessories_legend(""), "")
        self.assertEqual(format_rental_article_accessories_legend("   "), "")
        self.assertEqual(format_rental_article_accessories_legend(None), "")
        # Prefijo sin accesorios no emite leyenda
        self.assertEqual(format_rental_article_accessories_legend("Los siguientes equipos se entregan con:"), "")
        self.assertEqual(format_rental_article_accessories_legend("Los siguientes equipos se entrega con:   "), "")
        self.assertEqual(delivery_article_legend_lines({"remitoArticleLegend": ""}), [])
        self.assertEqual(delivery_article_legend_lines({"remitoArticleLegend": "   "}), [])
        self.assertEqual(delivery_article_legend_lines({"remitoArticleLegend": "Los siguientes equipos se entregan con: "}), [])
        # Leyenda general para alquiler por defecto
        self.assertEqual(delivery_order_general_legend_lines({"deliveryType": "rental"}), ["Se entrega en Alquiler"])
        # Leyenda general para alquiler con texto sin "alquiler"
        self.assertEqual(
            delivery_order_general_legend_lines({"deliveryType": "rental", "remitoGeneralLegend": "Atención Dr. Pérez"}),
            ["Se entrega en Alquiler", "Atención Dr. Pérez"],
        )
        # Leyenda general para venta
        self.assertEqual(delivery_order_general_legend_lines({"deliveryType": "sale"}), [])
        self.assertEqual(
            delivery_order_general_legend_lines({"deliveryType": "sale", "remitoGeneralLegend": "Entrega parcial"}),
            ["Entrega parcial"],
        )

    def test_rta_sin_accesorios_no_imprime_leyenda_de_accesorios(self):
        request = {
            "groupId": "rta-no-acc-test", "issueDate": "2026-09-08",
            "customerCode": "TEST", "customerName": "Cliente de prueba",
            "sellerCode": "ADM", "paymentTermCode": "30",
            "orders": [{
                "orderNumber": "OE-NOACC", "deliveryType": "rental",
                "remitoGeneralLegend": "Se entrega en alquiler",
                "items": [
                    {
                        "articleCode": "1115003", "articleName": "Concentrador de oxígeno",
                        "remitoArticleLegend": "",  # Sin accesorios cargados
                        "quantity": 1, "unitPrice": 0,
                        "partidas": [{"partida": "SERIE-X", "quantity": 1}],
                    },
                ],
            }],
        }
        config = delivery_remito_config({"type": "RTA", "operation": "ALQ", "deposit": "STL"})
        built = build_delivery_remito_comprobante(request, config)
        items = built["comprobante"]["Comprobante_Items"]
        legends = [i["Item_DescripArticulo"] for i in items if i["Item_Tipo"] == "L"]

        # Debe tener la orden y la leyenda general de alquiler
        self.assertIn("- Orden OE-NOACC -", legends)
        self.assertIn("Se entrega en alquiler", legends)

        # NO debe existir la leyenda de accesorios bajo ninguna circunstancia
        self.assertFalse(any("Los siguientes equipos se entregan con" in leg for leg in legends))
        self.assertFalse(any("accesorios" in leg.lower() for leg in legends))

    def test_wrap_remito_legend_lines_aprovecha_hasta_50_chars_sin_cortar_palabras(self):
        # Caso 1: Con prefacio (39 chars) + accesorio corto. Aprovecha los 50 caracteres sin cortar palabras.
        # "Los siguientes equipos se entregan con: " (39) + "Bolso," (6) = 46 caracteres.
        # "fuente de alimentación, cable de red" no entra en 50, así que pasa limpia al renglón 2.
        lines = delivery_article_legend_lines({"remitoArticleLegend": "Bolso, fuente de alimentación, cable de red"})
        self.assertEqual(
            lines,
            [
                "Los siguientes equipos se entregan con: Bolso,",
                "fuente de alimentación, cable de red",
            ],
        )
        self.assertTrue(all(len(l) <= 50 for l in lines))

        # Caso 2: Comas sin espacio se normalizan para no tratar palabras compuestas como una sola
        lines_no_spaces = delivery_article_legend_lines({"remitoArticleLegend": "Bolso,fuente,cable,mascara"})
        self.assertEqual(
            lines_no_spaces,
            [
                "Los siguientes equipos se entregan con: Bolso,",
                "fuente, cable, mascara",
            ],
        )
        self.assertTrue(all(len(l) <= 50 for l in lines_no_spaces))

        # Caso 3: Lista extensa de accesorios que requiere 3 renglones sin superar 50 caracteres en ninguno
        long_acc = "Bolso, fuente de alimentación, cable 220V, manguera de oxígeno, filtro bacteriológico, soporte rodante"
        lines_long = delivery_article_legend_lines({"remitoArticleLegend": long_acc})
        self.assertEqual(len(lines_long), 3)
        self.assertTrue(all(len(l) <= 50 for l in lines_long))
        self.assertEqual(lines_long[0], "Los siguientes equipos se entregan con: Bolso,")

        # Caso 4: Accesorio cuya primera palabra excede los 11 caracteres libres del prefacio
        # Prefacio (39) + "Humidificador" (13) = 52 (>50). Debe cortar antes de la palabra para no partirla.
        lines_long_word = delivery_article_legend_lines({"remitoArticleLegend": "Humidificador, cable de red"})
        self.assertEqual(
            lines_long_word,
            [
                "Los siguientes equipos se entregan con:",
                "Humidificador, cable de red",
            ],
        )
        self.assertTrue(all(len(l) <= 50 for l in lines_long_word))

    def test_split_remito_con_leyendas_de_distintas_longitudes(self):
        # Remito con límite de 21 líneas.
        # Cabecera siempre ocupa 2 líneas ("- Orden OE-XXX -" + "Se entrega en Alquiler").
        # Espacio restante para items + leyendas = 19 líneas.

        # 1. Sin accesorios (0 líneas de leyenda) -> 19 items en remito 1, 1 en remito 2
        order_0 = {
            "orderNumber": "OE-S0",
            "deliveryType": "rental",
            "items": [{
                "id": "item-0",
                "articleCode": "1105001",
                "description": "CPAP",
                "remitoArticleLegend": "",
                "partidas": [{"partida": f"P-{i}", "quantity": 1} for i in range(1, 21)],
            }],
        }
        chunks_0 = _remito_print_item_chunks(order_0)
        self.assertEqual(len(chunks_0), 2)
        self.assertEqual(len(chunks_0[0][0]["partidas"]), 19)
        self.assertEqual(len(chunks_0[1][0]["partidas"]), 1)

        # 2. Con accesorios de 1 línea (ej. "Vaso") -> 18 items en remito 1, 2 en remito 2
        order_1 = {
            "orderNumber": "OE-S1",
            "deliveryType": "rental",
            "items": [{
                "id": "item-1",
                "articleCode": "1105001",
                "description": "CPAP",
                "remitoArticleLegend": "Vaso",
                "partidas": [{"partida": f"P-{i}", "quantity": 1} for i in range(1, 21)],
            }],
        }
        chunks_1 = _remito_print_item_chunks(order_1)
        self.assertEqual(len(chunks_1), 2)
        self.assertEqual(len(chunks_1[0][0]["partidas"]), 18)
        self.assertEqual(len(chunks_1[1][0]["partidas"]), 2)

        # 3. Con accesorios de 2 líneas (ej. "Bolso, fuente de alimentación, cable de red") -> 17 items en remito 1, 3 en remito 2
        order_2 = {
            "orderNumber": "OE-S2",
            "deliveryType": "rental",
            "items": [{
                "id": "item-2",
                "articleCode": "1105001",
                "description": "CPAP",
                "remitoArticleLegend": "Bolso, fuente de alimentación, cable de red",
                "partidas": [{"partida": f"P-{i}", "quantity": 1} for i in range(1, 21)],
            }],
        }
        chunks_2 = _remito_print_item_chunks(order_2)
        self.assertEqual(len(chunks_2), 2)
        self.assertEqual(len(chunks_2[0][0]["partidas"]), 17)
        self.assertEqual(len(chunks_2[1][0]["partidas"]), 3)

        # 4. Con accesorios de 3 líneas -> 16 items en remito 1, 4 en remito 2
        order_3 = {
            "orderNumber": "OE-S3",
            "deliveryType": "rental",
            "items": [{
                "id": "item-3",
                "articleCode": "1105001",
                "description": "CPAP",
                "remitoArticleLegend": "Bolso, fuente de alimentación, cable 220V, manguera de oxígeno, filtro bacteriológico, soporte rodante",
                "partidas": [{"partida": f"P-{i}", "quantity": 1} for i in range(1, 21)],
            }],
        }
        chunks_3 = _remito_print_item_chunks(order_3)
        self.assertEqual(len(chunks_3), 2)
        self.assertEqual(len(chunks_3[0][0]["partidas"]), 16)
        self.assertEqual(len(chunks_3[1][0]["partidas"]), 4)


@skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL")
class DeliveryOrderLegendsPersistenceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        call_command("apply_delivery_orders_schema", verbosity=0)
        super().setUpClass()

    def test_crear_y_actualizar_orden_con_leyendas_general_y_articulo(self):
        payload = {
            "orderNumber": "OE-LEGEND-TEST",
            "customerName": "Cliente Leyendas",
            "deliveryType": "rental",
            "companyKey": "SEPID",
            "remitoGeneralLegend": "Se entrega en alquiler",
            "items": [
                {
                    "articleCode": "1115003",
                    "description": "Concentrador de oxígeno",
                    "remitoArticleLegend": "Vaso humidificador y cánula nasal",
                    "quantity": 2,
                    "unitPrice": "0",
                    "priceCurrency": "ARS",
                    "stockDepositCode": "STL",
                }
            ],
        }
        with (
            patch("service.delivery_orders.notify_delivery_order_created"),
            patch("service.delivery_orders.notify_delivery_order_remito_ready"),
        ):
            created = create_delivery_order(payload, actor_user_id=None)

        self.assertEqual(created["remitoGeneralLegend"], "Se entrega en alquiler")
        self.assertEqual(len(created["items"]), 1)
        self.assertEqual(created["items"][0]["remitoArticleLegend"], "Vaso humidificador y cánula nasal")

        fetched = get_delivery_order(created["id"])
        self.assertEqual(fetched["remitoGeneralLegend"], "Se entrega en alquiler")
        self.assertEqual(fetched["items"][0]["remitoArticleLegend"], "Vaso humidificador y cánula nasal")

        item_id = fetched["items"][0]["id"]
        update_payload = {
            "remitoGeneralLegend": "Se entrega en comodato",
            "items": [
                {
                    "id": item_id,
                    "articleCode": "1115003",
                    "description": "Concentrador de oxígeno",
                    "remitoArticleLegend": "Accesorios actualizados",
                    "quantity": 2,
                    "unitPrice": "0",
                    "priceCurrency": "ARS",
                    "stockDepositCode": "STL",
                }
            ],
        }
        updated = update_delivery_order(created["id"], update_payload, actor_user_id=None)
        self.assertEqual(updated["remitoGeneralLegend"], "Se entrega en comodato")
        self.assertEqual(updated["items"][0]["remitoArticleLegend"], "Accesorios actualizados")

        fetched_again = get_delivery_order(created["id"])
        self.assertEqual(fetched_again["remitoGeneralLegend"], "Se entrega en comodato")
        self.assertEqual(fetched_again["items"][0]["remitoArticleLegend"], "Accesorios actualizados")
