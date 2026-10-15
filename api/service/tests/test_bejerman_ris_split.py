from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_sdk import BejermanSdkResponseError

from service.bejerman_ris import (
    BejermanRisError,
    BejermanRisReviewRequiredError,
    _combined_payload,
    _partition_service_ingress_payload,
    _preflight_from_contexts,
    _recover_emit_partida_rejection,
    _send_service_ingress_comprobante,
    emit_or_get_ris_batch,
    register_ris_batch,
)


def _equipment_payload(ingreso_id, *, accessories=""):
    return {
        "requestId": f"reparaciones-ingreso-{ingreso_id}",
        "ingresoId": ingreso_id,
        "companyKey": "SEPID",
        "companyLabel": "SEPID SA",
        "bejermanCompany": "EMP",
        "documentProfile": {
            "type": "RIS",
            "letter": "R",
            "pointOfSale": "00004",
            "operation": "REP",
            "deposit": "STR",
            "updateStock": False,
        },
        "issueDate": "2026-08-11",
        "customerCode": "CLI-BEJ",
        "customerName": "Cliente Bejerman",
        "sellerCode": "ADM",
        "paymentTermCode": "30",
        "notes": f"OS {ingreso_id}",
        "equipment": {
            "articleCode": "ART-RIS",
            "articleName": "Concentrador de oxígeno",
            "serial": f"SN-{ingreso_id}",
            "internalNumber": "",
            "repairReason": "otros",
            "accessories": accessories,
            "comments": "",
            "equipmentLabel": "Concentrador de oxígeno",
        },
    }


def _combined(ingreso_ids, *, accessories_by_index=None):
    accessories_by_index = accessories_by_index or {}
    issues = []
    payload = _combined_payload(
        [
            _equipment_payload(ingreso_id, accessories=accessories_by_index.get(index, ""))
            for index, ingreso_id in enumerate(ingreso_ids)
        ],
        issues,
    )
    if issues:
        raise AssertionError(issues)
    return payload


def _customer_fields():
    return {
        "Cliente_RazonSocial": "Cliente Bejerman",
        "Cliente_NroDocumento": "30700000000",
        "Cliente_Provincia": "02",
        "Cliente_SitIVA": "RI",
    }


@override_settings(BEJERMAN_RIS_MAX_PRINT_LINES=22)
class BejermanRisSplitTests(SimpleTestCase):
    @override_settings(BEJERMAN_RECOVERY_KEEPS_STOCK_MOVEMENT=True)
    def test_reintento_ris_que_entra_con_error_no_repone_el_ent(self):
        client = Mock()
        client.ingresar_comprobante_ventas_json.side_effect = [RuntimeError("timeout"), {"Resultado": "OK"}]
        entries = [{"Comprobante_ArtPartida": "N568930", "Comprobante_ArtDeposito": "STR"}]
        payload = {"documentProfile": {"deposit": "STR"}, "equipment": {"serial": "N568930"}}
        built = {"comprobante": {"Comprobante_Items": []}}

        with (
            patch("service.bejerman_ris._ensure_register_stock_entries", return_value=entries),
            patch("service.bejerman_ris._neutralize_register_stock_entries"),
            patch("service.bejerman_ris._register_entries_moved_by_comprobante", return_value=True),
            patch("service.bejerman_ris._restore_register_stock_entries") as restore,
            patch("service.bejerman_ris._service_ingress_response", return_value={"remitoNumber": "RDA R 00001-00005287"}),
        ):
            with self.assertRaises(BejermanRisReviewRequiredError):
                _recover_emit_partida_rejection(
                    client, payload, built, operation="IngresarComprobanteJSON", numera_flex="S", emite_reg="E"
                )

        self.assertEqual(client.ingresar_comprobante_ventas_json.call_count, 1)
        restore.assert_not_called()

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=3, BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0)
    def test_la_ris_entro_si_se_movio_una_sola_de_sus_partidas(self):
        # RIS 00004-00005325 (21/09/2026, cinco equipos): el reintento entró, pero se
        # exigía que se movieran todas las partidas y se miraba una sola vez. Una que
        # todavía no se reflejaba bastó para reponer el ENT de las cinco: STR quedó en 2.
        from service.bejerman_ris import _register_entries_moved_by_comprobante

        entries = [
            {"Comprobante_ArtPartida": partida, "Comprobante_Art_CodGen": "ART-RIS"}
            for partida in ("P1", "P2", "P3")
        ]
        client = Mock()
        client.obtener_stock_partida.side_effect = lambda partida: partida
        # Primera vuelta: todas en cero. Segunda: P2 ya muestra la unidad de la RIS.
        lecturas = iter([0, 0, 0, 0, 1])

        with (
            patch(
                "service.bejerman_ris._records_for_partida_any_quantity",
                side_effect=lambda _response, _partida, _deposit: [{"q": next(lecturas), "Art_CodGen": "ART-RIS"}],
            ),
            patch("service.bejerman_ris._quantity_from_record", side_effect=lambda record: record["q"]),
        ):
            self.assertTrue(_register_entries_moved_by_comprobante(client, entries, "STR"))

    def test_falta_de_stock_no_dispara_la_recuperacion_de_partida(self):
        from service.bejerman_ris import _is_partida_rejection_message

        self.assertFalse(
            _is_partida_rejection_message(
                "El comprobante no se importó. No hay stock suficiente en la partida X para el artículo Y ."
            )
        )
        self.assertTrue(_is_partida_rejection_message("Partida inexistente y/o no informada"))
        self.assertFalse(_is_partida_rejection_message("El comprobante no se importó: stock insuficiente en la partida X"))

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=2, BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0)
    def test_la_ris_no_entro_si_ninguna_partida_se_movio(self):
        from service.bejerman_ris import _register_entries_moved_by_comprobante

        client = Mock()
        with (
            patch("service.bejerman_ris._records_for_partida_any_quantity", return_value=[{"q": 0, "Art_CodGen": "ART-RIS"}]),
            patch("service.bejerman_ris._quantity_from_record", side_effect=lambda record: record["q"]),
        ):
            self.assertFalse(
                _register_entries_moved_by_comprobante(
                    client, [{"Comprobante_ArtPartida": "P1", "Comprobante_Art_CodGen": "ART-RIS"}], "STR"
                )
            )
        self.assertEqual(client.obtener_stock_partida.call_count, 2)

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=2, BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0)
    def test_lectura_incierta_no_habilita_reponer_el_ent(self):
        from service.bejerman_ris import _register_entries_moved_by_comprobante

        client = Mock()
        client.obtener_stock_partida.side_effect = RuntimeError("sin respuesta")
        entries = [{"Comprobante_ArtPartida": "P1", "Comprobante_Art_CodGen": "ART-RIS"}]
        self.assertIsNone(_register_entries_moved_by_comprobante(client, entries, "STR"))

        with (
            patch("service.bejerman_ris._ensure_register_stock_entries", return_value=entries),
            patch("service.bejerman_ris._neutralize_register_stock_entries"),
            patch("service.bejerman_ris._restore_register_stock_entries") as restore,
        ):
            client.ingresar_comprobante_ventas_json.side_effect = BejermanSdkResponseError(
                "El comprobante no se importó. Partida inexistente."
            )
            with self.assertRaises(BejermanRisReviewRequiredError):
                _recover_emit_partida_rejection(
                    client,
                    {"documentProfile": {"deposit": "STR"}},
                    {"comprobante": {"Comprobante_Items": []}},
                    operation="IngresarComprobanteJSON",
                    numera_flex="S",
                    emite_reg="E",
                )
        restore.assert_not_called()

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=2, BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0)
    def test_cero_confirmado_permite_reponer_y_usar_el_fallback_sin_stock(self):
        entries = [{"Comprobante_ArtPartida": "P1", "Comprobante_Art_CodGen": "ART-RIS"}]
        client = Mock()
        client.ingresar_comprobante_ventas_json.side_effect = [
            BejermanSdkResponseError("Partida inexistente y/o no informada"),
            {"Resultado": "OK"},
        ]
        with (
            patch("service.bejerman_ris._ensure_register_stock_entries", return_value=entries),
            patch("service.bejerman_ris._neutralize_register_stock_entries"),
            patch("service.bejerman_ris._records_for_partida_any_quantity", return_value=[{"Art_CodGen": "ART-RIS", "Art_RealUM1": 0}]),
            patch("service.bejerman_ris._restore_register_stock_entries") as restore,
            patch("service.bejerman_ris._service_ingress_response", return_value={"success": True}),
        ):
            request_payload, response = _recover_emit_partida_rejection(
                client,
                {"documentProfile": {"deposit": "STR"}, "equipment": {"serial": "P1"}},
                {"comprobante": {"Comprobante_Tipo": "RIS", "Comprobante_Items": []}},
                operation="IngresarComprobanteJSON",
                numera_flex="S",
                emite_reg="E",
            )
        restore.assert_called_once_with(client, entries)
        self.assertEqual(request_payload["comprobante"]["Comprobante_ActualizaStock"], "N")
        self.assertTrue(response["success"])

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=1)
    def test_partida_compartida_no_confunde_el_stock_de_otro_articulo(self):
        from service.bejerman_ris import _register_entries_moved_by_comprobante

        entries = [{"Comprobante_ArtPartida": "SERIE-COMPARTIDA", "Comprobante_Art_CodGen": "CPAP"}]
        client = Mock()
        with patch(
            "service.bejerman_ris._records_for_partida_any_quantity",
            return_value=[{"Art_CodGen": "HUMIDIFICADOR", "Art_RealUM1": 1}, {"Art_CodGen": "CPAP", "Art_RealUM1": 0}],
        ):
            self.assertFalse(_register_entries_moved_by_comprobante(client, entries, "STR"))

    @override_settings(BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=1)
    def test_stock_negativo_requiere_revision(self):
        from service.bejerman_ris import _register_entries_moved_by_comprobante

        client = Mock()
        entries = [{"Comprobante_ArtPartida": "P1", "Comprobante_Art_CodGen": "ART-RIS"}]
        with patch(
            "service.bejerman_ris._records_for_partida_any_quantity",
            return_value=[{"Art_CodGen": "ART-RIS", "Art_RealUM1": -1}],
        ):
            self.assertIsNone(_register_entries_moved_by_comprobante(client, entries, "STR"))

    def test_preflight_de_12_equipos_cuenta_las_os_provisionales(self):
        payloads = [_equipment_payload(f"preflight-{index + 1}") for index in range(12)]
        contexts = [{"id": f"preflight-{index + 1}"} for index in range(12)]

        with (
            patch("service.bejerman_ris._payloads_from_contexts", return_value=payloads),
            patch("service.bejerman_ris._validate_customer", return_value=_customer_fields()),
            patch("service.bejerman_ris._validate_document_partida"),
            patch("service.bejerman_ris._validate_article"),
            patch("service.bejerman_ris._validate_equipment_remito_not_duplicated"),
        ):
            result = _preflight_from_contexts(contexts)

        self.assertTrue(result["can_emit"], result["issues"])
        self.assertEqual(result["preview"]["lineCount"], 37)
        self.assertTrue(result["preview"]["splitRequired"])
        self.assertEqual(result["preview"]["documentLineCounts"], [22, 16])

    def test_preflight_de_registro_manual_conserva_un_unico_remito_compacto(self):
        payloads = [_equipment_payload(f"preflight-{index + 1}") for index in range(12)]
        contexts = [{"id": f"preflight-{index + 1}"} for index in range(12)]

        with (
            patch("service.bejerman_ris._payloads_from_contexts", return_value=payloads),
            patch("service.bejerman_ris._validate_customer", return_value=_customer_fields()),
            patch("service.bejerman_ris._validate_document_partida"),
            patch("service.bejerman_ris._validate_article"),
        ):
            result = _preflight_from_contexts(
                contexts,
                skip_equipment_duplicate_check=True,
                compact_service_ingress_legends=True,
            )

        self.assertTrue(result["can_emit"], result["issues"])
        self.assertEqual(result["preview"]["lineCount"], 13)
        self.assertFalse(result["preview"]["splitRequired"])
        self.assertEqual(result["preview"]["documentCount"], 1)
        self.assertEqual(result["preview"]["documentLineCounts"], [13])

    def test_particiona_12_equipos_completos_en_7_y_5_y_repite_titulo(self):
        blocks = _partition_service_ingress_payload(_combined(range(1, 13)), _customer_fields())

        self.assertEqual([block["equipmentCount"] for block in blocks], [7, 5])
        self.assertEqual([block["lineCount"] for block in blocks], [22, 16])
        self.assertEqual([block["ingresoIds"] for block in blocks], [list(range(1, 8)), list(range(8, 13))])
        for block in blocks:
            items = block["built"]["comprobante"]["Comprobante_Items"]
            titles = [
                item
                for item in items
                if item.get("Item_Tipo") == "L"
                and item.get("Item_DescripArticulo") == "Se recibe para servicio técnico:"
            ]
            self.assertEqual(len(titles), 1)

    def test_preflight_bloquea_un_equipo_que_por_si_solo_supera_22_renglones(self):
        accessories = "; ".join(f"Accesorio {index}" for index in range(20))
        payloads = [
            _equipment_payload("preflight-1", accessories=accessories),
            _equipment_payload("preflight-2"),
        ]
        contexts = [{"id": "preflight-1"}, {"id": "preflight-2"}]

        with (
            patch("service.bejerman_ris._payloads_from_contexts", return_value=payloads),
            patch("service.bejerman_ris._validate_customer", return_value=_customer_fields()),
            patch("service.bejerman_ris._validate_document_partida"),
            patch("service.bejerman_ris._validate_article"),
            patch("service.bejerman_ris._validate_equipment_remito_not_duplicated"),
        ):
            result = _preflight_from_contexts(contexts)

        self.assertFalse(result["can_emit"])
        issue = next(item for item in result["issues"] if item["code"] == "BEJERMAN_RIS_ITEM_EXCEEDS_MAX_LINES")
        self.assertEqual(issue["item_index"], 0)
        self.assertIn("no entra completo", issue["message"])

    def test_emit_batch_emite_y_persiste_cada_bloque_y_devuelve_contrato_agregado(self):
        ingreso_ids = list(range(1, 13))
        batch_payload = _combined(ingreso_ids)
        persisted = []
        emitted = []

        def emit(block_payload, *, user_id=None):
            emitted.append(list(block_payload["ingresoIds"]))
            index = len(emitted)
            number = f"RIS R 00004-{4500 + index:08d}"
            line_count = 1 + (3 * len(block_payload["equipments"]))
            request_payload = {**block_payload, "comprobante": {"Comprobante_Items": []}}
            response = {
                "remitoNumber": number,
                "response": {
                    "comprobanteTipo": "RIS",
                    "comprobanteLetra": "R",
                    "comprobantePtoVenta": "00004",
                    "comprobanteNumero": f"{4500 + index:08d}",
                    "remitoNumber": number,
                },
                "profile": block_payload["documentProfile"],
                "lineCount": line_count,
            }
            return request_payload, response

        def persist(block_ids, request_payload, response):
            persisted.append(list(block_ids))
            return {
                "id": 100 + len(persisted),
                "ingreso_id": block_ids[0],
                "status": "generated",
                "pdf_status": "pending",
                "remito_number": response["remitoNumber"],
                "comprobante_tipo": "RIS",
            }

        with (
            patch("service.bejerman_ris._ensure_ris_row"),
            patch(
                "service.bejerman_ris._row_for_ingreso",
                side_effect=lambda ingreso_id: {"ingreso_id": ingreso_id, "status": "pending", "remito_number": ""},
            ),
            patch("service.bejerman_ris.preflight_ris_for_ingresos", return_value={"can_emit": True, "issues": []}),
            patch("service.bejerman_ris._lock_for_emit_many"),
            patch("service.bejerman_ris._build_batch_payload", return_value=batch_payload),
            patch("service.bejerman_ris._emit_payload_to_bejerman", side_effect=emit),
            patch("service.bejerman_ris._update_ris_generated_many", side_effect=persist),
        ):
            result = emit_or_get_ris_batch(ingreso_ids, user_id=9)

        self.assertEqual(emitted, [list(range(1, 8)), list(range(8, 13))])
        self.assertEqual(persisted, emitted)
        self.assertTrue(result["multiple"])
        self.assertEqual(result["remitoNumbers"], ["RIS R 00004-00004501", "RIS R 00004-00004502"])
        self.assertEqual(result["remito_number"], "RIS R 00004-00004501")
        self.assertEqual(result["id"], 101)
        self.assertEqual([item["ingresoIds"] for item in result["remitos"]], emitted)
        self.assertEqual([item["lineCount"] for item in result["remitos"]], [22, 16])

    def test_lote_incierto_deja_en_revision_solo_el_bloque_enviado(self):
        ingreso_ids = list(range(1, 13))
        with (
            patch("service.bejerman_ris._ensure_ris_row"),
            patch("service.bejerman_ris._row_for_ingreso", return_value={"status": "pending"}),
            patch("service.bejerman_ris.preflight_ris_for_ingresos", return_value={"can_emit": True, "issues": []}),
            patch("service.bejerman_ris._lock_for_emit_many"),
            patch("service.bejerman_ris._build_batch_payload", return_value=_combined(ingreso_ids)),
            patch("service.bejerman_ris._emit_payload_to_bejerman", side_effect=BejermanRisReviewRequiredError("Emisión incierta")) as emit,
            patch("service.bejerman_ris._mark_ris_emission_uncertain") as review,
            patch("service.bejerman_ris._update_ris_failure_many") as failed,
        ):
            with self.assertRaises(BejermanRisReviewRequiredError):
                emit_or_get_ris_batch(ingreso_ids, user_id=9)
        emit.assert_called_once()
        self.assertEqual([call.args[0] for call in review.call_args_list], list(range(1, 8)))
        self.assertEqual(failed.call_args.args[0], list(range(8, 13)))
        failed.assert_called_once()

    def test_guard_final_bloquea_un_bloque_mayor_a_22_antes_del_sdk(self):
        client = Mock()

        with self.assertRaisesRegex(BejermanRisError, "supera el máximo físico"):
            _send_service_ingress_comprobante(
                client,
                _combined(range(1, 9)),
                _customer_fields(),
                operation="IngresarComprobanteJSON",
                numera_flex="S",
                emite_reg="E",
            )

        client.ingresar_comprobante_ventas_json.assert_not_called()

    def test_register_batch_no_usa_la_particion_automatica(self):
        ingreso_ids = list(range(1, 13))
        batch_payload = _combined(ingreso_ids)
        document = {
            "type": "RIS",
            "letter": "R",
            "point": "00001",
            "number": "00001234",
            "remitoNumber": "RIS R 00001-00001234",
        }
        response = {
            "remitoNumber": document["remitoNumber"],
            "documentMode": "register",
            "response": {},
        }

        with (
            patch("service.bejerman_ris._existing_ingreso_ids", return_value=ingreso_ids),
            patch("service.bejerman_ris._build_batch_payload", return_value=batch_payload),
            patch("service.bejerman_ris.normalize_manual_ris_remito_number", return_value=document),
            patch("service.bejerman_ris._ensure_ris_row"),
            patch(
                "service.bejerman_ris._row_for_ingreso",
                side_effect=lambda ingreso_id: {"ingreso_id": ingreso_id, "status": "pending", "remito_number": ""},
            ),
            patch("service.bejerman_ris.preflight_ris_for_ingresos", return_value={"can_emit": True, "issues": []}),
            patch("service.bejerman_ris._lock_for_register_many"),
            patch(
                "service.bejerman_ris._register_payload_to_bejerman",
                return_value=({**batch_payload, "documentMode": "register"}, response),
            ) as register,
            patch(
                "service.bejerman_ris._update_ris_generated_many",
                return_value={
                    "ingreso_id": ingreso_ids[0],
                    "status": "generated",
                    "remito_number": document["remitoNumber"],
                },
            ),
            patch("service.bejerman_ris._partition_service_ingress_payload") as partition,
        ):
            result = register_ris_batch(ingreso_ids, document["remitoNumber"], user_id=9)

        self.assertEqual(result["remito_number"], document["remitoNumber"])
        register.assert_called_once()
        self.assertEqual(register.call_args.args[2], ingreso_ids)
        partition.assert_not_called()
