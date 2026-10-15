"""El equipo que vuelve a entrar con su orden anterior todavía abierta.

Lo que se protege acá es que el mostrador no se trabe: el remito tiene que salir igual, con
el equipo adentro, pero sin afirmar un número de OS que todavía puede cambiar.
"""

from contextlib import nullcontext
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_remito_legends import format_os_badge
from service.bejerman_ris import _build_batch_payload, _validate_no_open_ingreso
from service.bejerman_sdk import build_service_ingress_comprobante
from service.views.ingresos_views import NuevoIngresoLoteView, _register_ingreso_conflicts


def _context(ingreso_id, *, serial="SN-1", tipo="CPAP"):
    return {
        "id": ingreso_id,
        "motivo": "reparacion",
        "fecha_ingreso": "2026-08-19",
        "accesorios": "",
        "comentarios": "",
        "equipo_variante": "",
        "remito_ingreso": "",
        "empresa_bejerman": "SEPID",
        "empresa_facturar": "SEPID",
        "customer_id": 1,
        "customer_code": "CLI-BEJ",
        "customer_name": "Cliente Bejerman",
        "cuit": "30700000000",
        "propietario_nombre": "",
        "propietario_contacto": "",
        "propietario_doc": "",
        "device_id": 1,
        "numero_serie": serial,
        "numero_interno": "",
        "marca_id": 1,
        "marca": "Yamind",
        "model_id": 1,
        "modelo": "DM26",
        "tipo_equipo": tipo,
        "device_variante": "",
        "modelo_variante": "",
        "recibido_por_nombre": "",
    }


def _customer_fields():
    return {
        "Cliente_RazonSocial": "Cliente Bejerman",
        "Cliente_NroDocumento": "30700000000",
        "Cliente_Provincia": "02",
        "Cliente_SitIVA": "RI",
    }


def _legend_texts(comprobante):
    return [
        str(item.get("Item_DescripArticulo") or "")
        for item in comprobante["comprobante"]["Comprobante_Items"]
        if str(item.get("Item_Tipo") or "") != "A"
    ]


class OsSuppressionTests(SimpleTestCase):
    def test_el_equipo_en_conflicto_sale_en_el_remito_sin_numero_de_os(self):
        contexts = [_context(101), _context(102, serial="SN-2")]

        with (
            patch("service.bejerman_ris._ingreso_context", side_effect=contexts),
            patch("service.bejerman_ris._article_mapping_for_context", return_value={"article_code": "ART-RIS"}),
            patch("service.bejerman_ris._pending_conflict_ingreso_ids", return_value={102}),
        ):
            payload = _build_batch_payload([101, 102])

        equipments = {item["ingresoId"]: item for item in payload["equipments"]}
        self.assertEqual(equipments[101]["osLabel"], "00101")
        self.assertFalse(equipments[101].get("suppressOsLabel"))
        self.assertEqual(equipments[102]["osLabel"], "")
        self.assertTrue(equipments[102]["suppressOsLabel"])

        # El equipo trabado igual viaja en el remito: es lo que el cliente vino a dejar.
        self.assertEqual(len(payload["equipments"]), 2)
        # Y la cabecera no lo contradice nombrando la OS que el renglón no imprime.
        self.assertEqual(payload["notes"], "OS 00101")

    @override_settings(BEJERMAN_RIS_MAX_PRINT_LINES=22)
    def test_el_comprobante_no_imprime_la_leyenda_de_os_del_equipo_trabado(self):
        contexts = [_context(101), _context(102, serial="SN-2")]

        with (
            patch("service.bejerman_ris._ingreso_context", side_effect=contexts),
            patch("service.bejerman_ris._article_mapping_for_context", return_value={"article_code": "ART-RIS"}),
            patch("service.bejerman_ris._pending_conflict_ingreso_ids", return_value={102}),
        ):
            payload = _build_batch_payload([101, 102])

        comprobante = build_service_ingress_comprobante(payload, _customer_fields())
        legends = _legend_texts(comprobante)

        self.assertIn(format_os_badge(101), legends)
        # El fallback a `ingresoId` es justamente lo que hacía reaparecer la OS suprimida.
        self.assertNotIn(format_os_badge(102), legends)
        self.assertNotIn("00102", " ".join(legends))

        # En el papel no queda un hueco ni un guion suelto donde iba la OS: el renglón
        # directamente no se imprime. El formulario tiene 22 líneas contadas, así que un
        # renglón vacío correría todo lo de abajo.
        self.assertNotIn("", legends)
        with (
            patch("service.bejerman_ris._ingreso_context", side_effect=[_context(101), _context(102, serial="SN-2")]),
            patch("service.bejerman_ris._article_mapping_for_context", return_value={"article_code": "ART-RIS"}),
            patch("service.bejerman_ris._pending_conflict_ingreso_ids", return_value=set()),
        ):
            sin_conflicto = build_service_ingress_comprobante(
                _build_batch_payload([101, 102]), _customer_fields()
            )
        self.assertEqual(
            len(sin_conflicto["comprobante"]["Comprobante_Items"]),
            len(comprobante["comprobante"]["Comprobante_Items"]) + 1,
        )

    @override_settings(BEJERMAN_RIS_MAX_PRINT_LINES=22)
    def test_sin_conflictos_el_remito_queda_exactamente_igual_que_antes(self):
        contexts = [_context(101), _context(102, serial="SN-2")]

        with (
            patch("service.bejerman_ris._ingreso_context", side_effect=contexts),
            patch("service.bejerman_ris._article_mapping_for_context", return_value={"article_code": "ART-RIS"}),
            patch("service.bejerman_ris._pending_conflict_ingreso_ids", return_value=set()),
        ):
            payload = _build_batch_payload([101, 102])

        legends = _legend_texts(build_service_ingress_comprobante(payload, _customer_fields()))

        self.assertIn(format_os_badge(101), legends)
        self.assertIn(format_os_badge(102), legends)
        self.assertEqual(payload["notes"], "OS 00101, OS 00102")

    def test_una_tabla_de_conflictos_ausente_no_rompe_la_emision(self):
        contexts = [_context(101)]

        with (
            patch("service.bejerman_ris._ingreso_context", side_effect=contexts),
            patch("service.bejerman_ris._article_mapping_for_context", return_value={"article_code": "ART-RIS"}),
            patch(
                "service.bejerman_ingreso_conflicts.pending_conflict_ingreso_ids",
                side_effect=RuntimeError("relation does not exist"),
            ),
        ):
            payload = _build_batch_payload([101])

        self.assertEqual(payload["equipments"][0]["osLabel"], "00101")


class OpenIngresoIssueTests(SimpleTestCase):
    def test_avisa_sin_bloquear_y_nombra_el_equipo_con_serie_y_tipo(self):
        issues = []
        abierto = {
            "id": 29488,
            "fecha_ingreso": "2026-05-04",
            "estado": "ingresado",
            "cliente": "GRIFFO DAMIAN EZEQUIEL",
        }

        with patch("service.bejerman_ris._open_ingreso_for_equipment", return_value=abierto):
            _validate_no_open_ingreso([_context("preflight-1"), _context("preflight-2")], issues)

        self.assertEqual(len(issues), 2)
        issue = issues[0]
        # Warning, no error: `can_emit` no se toca y el remito sale igual.
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["code"], "INGRESO_OPEN_CONFLICT")
        # El cartel tiene que decir qué equipo es, no "Equipo #2".
        self.assertIn("SN-1", issue["message"])
        self.assertIn("CPAP", issue["message"])
        self.assertIn("29488", issue["message"])
        self.assertEqual(issue["item_index"], 0)
        self.assertEqual(issue["fix"]["type"], "open_ingreso_conflict")
        self.assertEqual(issue["fix"]["conflicting_ingreso_id"], 29488)
        self.assertEqual(issue["fix"]["numero_serie"], "SN-1")
        self.assertEqual(issue["fix"]["tipo_equipo"], "CPAP")

    def test_el_humidificador_no_choca_contra_el_cpap_con_el_que_vino(self):
        """El chequeo pregunta por serie **y** tipo, así que cada uno mira su propia orden."""

        issues = []
        consultas = []

        def _lookup(serial, tipo):
            consultas.append((serial, tipo))
            return None

        with patch("service.bejerman_ris._open_ingreso_for_equipment", side_effect=_lookup):
            _validate_no_open_ingreso(
                [
                    _context("preflight-1", serial="y662125150133", tipo="CPAP"),
                    _context("preflight-2", serial="y662125150133", tipo="Calentador humidificador"),
                ],
                issues,
            )

        self.assertEqual(issues, [])
        self.assertEqual(
            consultas,
            [("y662125150133", "CPAP"), ("y662125150133", "Calentador humidificador")],
        )

    def test_sin_tipo_de_equipo_no_se_arriesga_un_falso_duplicado(self):
        issues = []

        with patch("service.bejerman_ris._open_ingreso_for_equipment") as lookup:
            _validate_no_open_ingreso([_context("preflight-1", tipo="")], issues)

        self.assertEqual(issues, [])
        lookup.assert_not_called()


def _lote_request(items):
    return SimpleNamespace(
        data={
            "cliente": {"id": 1},
            "empresa_bejerman": "SEPID",
            "items": items,
        },
        user=SimpleNamespace(id=7),
        user_id=7,
        user_role="recepcion",
    )


class NuevoIngresoLoteConflictTests(SimpleTestCase):
    """El caso del mostrador: seis equipos, uno traba, y el remito tiene que salir igual."""

    def _run_lote(self, child_bodies):
        respuestas = [SimpleNamespace(data=body, status_code=201) for body in child_bodies]

        with (
            patch("service.views.ingresos_views.require_any_permission"),
            patch("service.views.ingresos_views.transaction.atomic", return_value=nullcontext()),
            patch(
                "service.views.ingresos_views.preflight_ris_for_request_payload",
                return_value={"can_emit": True},
            ),
            patch("service.views.ingresos_views.NuevoIngresoView.post", side_effect=respuestas),
            patch("service.views.ingresos_views.record_conflict", return_value=55) as record,
            patch("service.views.ingresos_views.notify_ingreso_conflicto_abierto") as notify,
            patch("service.views.ingresos_views.attach_conflict_remito_number") as attach,
            patch(
                "service.views.ingresos_views.emit_or_get_ris_batch",
                return_value={"remito_number": "RIS R 00004-00004988"},
            ),
            patch(
                "service.views.ingresos_views.get_ris_status_for_ingreso",
                return_value={"remito_number": "RIS R 00004-00004988", "document_mode": "emit"},
            ),
            patch(
                "service.views.ingresos_views._ris_lote_result_fields",
                return_value={"remito_numbers": ["RIS R 00004-00004988"], "remitos": []},
            ),
        ):
            response = NuevoIngresoLoteView().post(_lote_request([{} for _ in child_bodies]))
        return response, record, notify, attach

    def test_un_equipo_trabado_ya_no_cancela_el_remito_de_los_demas(self):
        bodies = [
            {"ok": True, "ingreso_id": 101},
            {
                "ok": True,
                "ingreso_id": 102,
                "open_conflict": {
                    "conflicting_ingreso_id": 29488,
                    "conflicting_os": "29488",
                    "conflicting_fecha_ingreso": "2026-05-04",
                    "numero_serie": "y662125150133",
                    "tipo_equipo": "CPAP",
                },
            },
            {"ok": True, "ingreso_id": 103},
        ]

        response, record, notify, attach = self._run_lote(bodies)

        # Antes esto devolvía 409 y no se creaba ni emitía nada.
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["ingreso_ids"], [101, 102, 103])
        self.assertEqual(response.data["remito_number"], "RIS R 00004-00004988")

        record.assert_called_once_with(
            102,
            29488,
            numero_serie="y662125150133",
            tipo_equipo="CPAP",
            user_id=7,
        )
        notify.assert_called_once_with(102, 29488)
        attach.assert_called_once_with([102], "RIS R 00004-00004988")

        conflicts = response.data["open_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["ingreso_id"], 102)
        self.assertEqual(conflicts[0]["item_index"], 1)

    def test_un_lote_sin_conflictos_no_anota_ni_avisa_nada(self):
        response, record, notify, attach = self._run_lote(
            [{"ok": True, "ingreso_id": 101}, {"ok": True, "ingreso_id": 102}]
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["open_conflicts"], [])
        record.assert_not_called()
        notify.assert_not_called()
        attach.assert_not_called()


class RegisterConflictsTests(SimpleTestCase):
    def test_si_falla_el_aviso_el_conflicto_igual_queda_anotado(self):
        """El remito ya salió: perder el mail no puede costar perder el registro."""

        request = SimpleNamespace(user=SimpleNamespace(id=7), user_id=7)
        conflictos = [
            {"ingreso_id": 102, "conflicting_ingreso_id": 29488, "numero_serie": "SN-1", "tipo_equipo": "CPAP"}
        ]

        with (
            patch("service.views.ingresos_views.record_conflict", return_value=55),
            patch(
                "service.views.ingresos_views.notify_ingreso_conflicto_abierto",
                side_effect=RuntimeError("SMTP caído"),
            ),
        ):
            registrados = _register_ingreso_conflicts(conflictos, request)

        self.assertEqual(len(registrados), 1)
        self.assertEqual(registrados[0]["conflict_id"], 55)

    def test_si_no_se_puede_anotar_el_conflicto_la_emision_sigue(self):
        request = SimpleNamespace(user=SimpleNamespace(id=7), user_id=7)
        conflictos = [{"ingreso_id": 102, "conflicting_ingreso_id": 29488}]

        with (
            patch(
                "service.views.ingresos_views.record_conflict",
                side_effect=RuntimeError("relation does not exist"),
            ),
            patch("service.views.ingresos_views.notify_ingreso_conflicto_abierto") as notify,
        ):
            registrados = _register_ingreso_conflicts(conflictos, request)

        self.assertEqual(registrados, [])
        notify.assert_not_called()


class ResolverConflictoViewTests(SimpleTestCase):
    """Quién puede cerrar la orden vieja depende de si Bejerman muestra la prueba."""

    EVIDENCIA = {
        "status": "found",
        "documents": [
            {"remitoNumber": "RSS R 00004-00004123", "issueDate": "2026-06-10", "type": "RSS"}
        ],
    }
    SIN_EVIDENCIA = {"status": "not_found", "documents": [], "reason": "Sin remitos posteriores."}

    NUMERO_DESCONOCIDO = {
        "status": "not_found",
        "documents": [],
        "reason": "Bejerman no tiene ningún comprobante 00004-00009999 en SEPID.",
    }

    def _post(self, *, evidencia, permisos, data=None, documento=None, reingreso=""):
        from service.views import ingreso_conflicts_views as vistas

        request = SimpleNamespace(
            data={"conflicting_ingreso_id": 29488, **(data or {})},
            user=SimpleNamespace(id=7),
            user_id=7,
            user_role="recepcion",
        )
        correccion = SimpleNamespace(data={"ok": True}, status_code=200)

        def _require(req, code, *args, **kwargs):
            if code not in permisos:
                raise AssertionError(f"require_permission inesperado: {code}")

        with (
            patch.object(vistas, "find_exit_evidence", return_value=evidencia),
            patch.object(
                vistas, "find_document_by_number", return_value=documento or self.NUMERO_DESCONOCIDO
            ),
            patch.object(
                vistas,
                "_open_ingreso",
                return_value={
                    "id": 29488,
                    "fecha_ingreso": "2026-05-04",
                    "numero_serie": "y662125150133",
                    "empresa_bejerman": "SEPID",
                },
            ),
            patch.object(vistas, "require_permission", side_effect=_require),
            patch.object(vistas, "user_has_permission", side_effect=lambda req, code: code in permisos),
            patch.object(vistas, "get_conflict", return_value=None),
            patch.object(vistas, "_reingreso_fecha", return_value=reingreso),
            patch.object(
                vistas.IngresoCorreccionesHistoricasView, "post", return_value=correccion
            ) as correccion_post,
        ):
            response = vistas.IngresoConflictoResolverView().post(request)
        return response, correccion_post

    def test_con_prueba_en_bejerman_recepcion_cierra_la_orden_vieja(self):
        response, correccion_post = self._post(
            evidencia=self.EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["remito_salida"], "RSS R 00004-00004123")

        # El cierre pasa por la corrección histórica de siempre, con su rastro auditado.
        child, ingreso_id = correccion_post.call_args[0]
        self.assertEqual(ingreso_id, 29488)
        self.assertTrue(child._historical_correction_authorized)
        self.assertEqual(child.data["accion"], "entrega")
        self.assertEqual(child.data["remito_salida"], "RSS R 00004-00004123")
        self.assertEqual(child.data["fecha_entrega"], "2026-06-10")
        self.assertIn("RSS R 00004-00004123", child.data["motivo"])

    def test_sin_prueba_recepcion_no_puede_cerrar_nada(self):
        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict"},
        )

        self.assertEqual(response.status_code, 403)
        correccion_post.assert_not_called()

    def test_sin_prueba_jefatura_tiene_que_indicar_la_fecha_de_salida(self):
        """Sin comprobante no se inventa la fecha: se pide, en vez de fallar más abajo.

        La corrección histórica exige `fecha_efectiva`. Antes el cierre forzado la mandaba
        vacía y "Cerrar sin comprobante" fallaba siempre con "fecha_efectiva inválida"
        (conflicto de la OS 29875 con la 27479, 14/10/2026).
        """

        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict", "action.ingreso.force_historical"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("fecha", response.data["detail"])
        correccion_post.assert_not_called()

    def test_sin_prueba_jefatura_cierra_con_la_fecha_que_indica(self):
        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict", "action.ingreso.force_historical"},
            data={"fecha_salida": "2026-07-24", "motivo": "Volvió al cliente sin reparar."},
            reingreso="2026-10-13",
        )

        self.assertEqual(response.status_code, 200)
        correccion_post.assert_called_once()
        child, _ = correccion_post.call_args[0]
        self.assertEqual(child.data["accion"], "entrega")
        self.assertEqual(child.data["fecha_efectiva"], "2026-07-24")
        self.assertEqual(child.data["fecha_entrega"], "2026-07-24")
        self.assertEqual(child.data["remito_salida"], "")
        self.assertIn("forzado por jefatura", child.data["motivo"])
        self.assertIn("Fecha de salida indicada por jefatura: 2026-07-24", child.data["motivo"])
        self.assertIn("Volvió al cliente sin reparar.", child.data["motivo"])

    def test_la_fecha_forzada_va_entre_el_ingreso_viejo_y_el_reingreso(self):
        permisos = {"action.ingreso.resolve_open_conflict", "action.ingreso.force_historical"}
        futura = (date.today() + timedelta(days=5)).isoformat()
        casos = (
            # (fecha pedida, reingreso, texto esperado en el rechazo)
            ("2026-05-03", "", "anterior al ingreso"),
            ("2026-10-14", "2026-10-13", "posterior al reingreso"),
            (futura, "", "futura"),
        )
        for fecha, reingreso, esperado in casos:
            with self.subTest(fecha=fecha):
                response, correccion_post = self._post(
                    evidencia=self.SIN_EVIDENCIA,
                    permisos=permisos,
                    data={"fecha_salida": fecha},
                    reingreso=reingreso,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(esperado, response.data["detail"])
                correccion_post.assert_not_called()

        # Los dos extremos del tramo valen: el mismo día del ingreso viejo y el del reingreso.
        for fecha in ("2026-05-04", "2026-10-13"):
            with self.subTest(fecha=fecha):
                response, _ = self._post(
                    evidencia=self.SIN_EVIDENCIA,
                    permisos=permisos,
                    data={"fecha_salida": fecha},
                    reingreso="2026-10-13",
                )
                self.assertEqual(response.status_code, 200)

    def test_el_numero_cargado_a_mano_se_verifica_y_alcanza_para_cerrar(self):
        """El caso real: la serie está escrita distinto acá y en Bejerman.

        La búsqueda automática no encuentra nada, pero quien mira la pantalla tiene el remito.
        Ese número se verifica contra Bejerman: si existe y es posterior, respalda igual que si
        lo hubiera encontrado la búsqueda, así que recepción puede cerrar.
        """

        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict"},
            data={"remito_salida": "00004-00004893"},
            documento={
                "status": "found",
                "documents": [
                    {"remitoNumber": "RTA R 00004-00004893", "issueDate": "2026-07-24", "type": "RTA"}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["remito_salida"], "RTA R 00004-00004893")
        child, _ = correccion_post.call_args[0]
        self.assertEqual(child.data["remito_salida"], "RTA R 00004-00004893")
        self.assertEqual(child.data["fecha_entrega"], "2026-07-24")
        self.assertIn("verificado en Bejerman", child.data["motivo"])

    def test_un_numero_que_bejerman_no_tiene_no_cierra_nada(self):
        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict"},
            data={"remito_salida": "00004-00009999"},
        )

        self.assertEqual(response.status_code, 403)
        # El mensaje tiene que decir qué pasó con el número que se cargó, no repetir que la
        # búsqueda por serie no encontró nada.
        self.assertIn("00004-00009999", response.data["detail"])
        correccion_post.assert_not_called()

    def test_jefatura_puede_forzar_con_el_numero_que_no_se_pudo_verificar(self):
        response, correccion_post = self._post(
            evidencia=self.SIN_EVIDENCIA,
            permisos={"action.ingreso.resolve_open_conflict", "action.ingreso.force_historical"},
            data={"remito_salida": "00004-00009999", "fecha_salida": "2026-07-24"},
        )

        self.assertEqual(response.status_code, 200)
        child, _ = correccion_post.call_args[0]
        # El número igual queda registrado: es mejor rastro que cerrar la orden sin nada.
        self.assertEqual(child.data["remito_salida"], "00004-00009999")
        self.assertEqual(child.data["fecha_entrega"], "2026-07-24")
        self.assertIn("sin respaldo verificable", child.data["motivo"])


class SerialesDelConflictoTests(SimpleTestCase):
    def test_se_buscan_la_serie_anotada_y_la_del_equipo_sin_repetir(self):
        from service.views.ingreso_conflicts_views import _serials_for

        serials = _serials_for(
            {"numero_serie": "MZJ5S 121220"},
            {"numero_serie": "MZJ5S 121220"},
            {"numero_serie": "MZJ5S121220"},
        )

        self.assertEqual(serials, ["MZJ5S 121220", "MZJ5S121220"])


class EvidenciaEnBejermanTests(SimpleTestCase):
    """La serie escrita con un espacio de más no puede significar "el equipo nunca salió"."""

    def _run(self, serials, rows=()):
        import service.bejerman_delivery as delivery
        from service.bejerman_ingreso_conflicts import find_exit_evidence

        capturado = {}

        def _sql(sql, company_key=None, outcome=None):
            capturado["sql"] = sql
            capturado["company_key"] = company_key
            if outcome is not None:
                outcome["status"] = "success"
            return list(rows)

        with patch.object(delivery, "_remote_bejerman_sql_csv", side_effect=_sql):
            evidencia = find_exit_evidence(serials, "2026-07-02", company_key="SEPID")
        return evidencia, capturado

    def test_la_partida_se_compara_sin_espacios_y_en_mayusculas(self):
        _, capturado = self._run(["MZJ5S 121220", "mzj5s121220"])

        # Una sola forma normalizada: las dos escrituras son el mismo equipo.
        self.assertIn("N'MZJ5S121220'", capturado["sql"])
        self.assertNotIn("N'MZJ5S 121220'", capturado["sql"])
        self.assertIn("REPLACE(COALESCE(d.sdvstp_Partida, ''), ' ', '')", capturado["sql"])

    def test_encuentra_el_rta_aunque_la_serie_este_escrita_distinto(self):
        evidencia, _ = self._run(
            ["MZJ5S 121220"],
            rows=[
                {
                    "Comprobante_Tipo": "RTA",
                    "Comprobante_Letra": "R",
                    "Comprobante_PtoVenta": "00004",
                    "Comprobante_Numero": "00004893",
                    "Comprobante_FechaEmision": "2026-07-24",
                    "Item_Partida": "MZJ5S121220",
                    "Item_DescripArticulo": "Concentrador de oxígeno Longfian JAY-5",
                }
            ],
        )

        self.assertEqual(evidencia["status"], "found")
        self.assertEqual(evidencia["documents"][0]["remitoNumber"], "RTA R 00004-00004893")
        self.assertEqual(evidencia["documents"][0]["issueDate"], "2026-07-24")

    def test_sin_resultados_el_motivo_nombra_las_dos_escrituras_buscadas(self):
        evidencia, _ = self._run(["MZJ5S 121220", "MZJ5S121220"])

        self.assertEqual(evidencia["status"], "not_found")
        self.assertIn("MZJ5S 121220 / MZJ5S121220", evidencia["reason"])


class VerificarRemitoPorNumeroTests(SimpleTestCase):
    """Cargar un número a mano no alcanza: tiene que existir en Bejerman y poder ser esa salida."""

    FILA = {
        "Comprobante_Tipo": "RTA",
        "Comprobante_Letra": "R",
        "Comprobante_PtoVenta": "00004",
        "Comprobante_Numero": "00004893",
        "Comprobante_FechaEmision": "2026-07-24",
        "Item_Partida": "MZJ5S121220",
        "Item_DescripArticulo": "Concentrador de oxígeno Longfian JAY-5",
    }

    def _run(self, numero, rows=(), since="2026-07-02"):
        import service.bejerman_delivery as delivery
        from service.bejerman_ingreso_conflicts import find_document_by_number

        capturado = {}

        def _sql(sql, company_key=None, outcome=None):
            capturado["sql"] = sql
            if outcome is not None:
                outcome["status"] = "success"
            return list(rows)

        with patch.object(delivery, "_remote_bejerman_sql_csv", side_effect=_sql):
            resultado = find_document_by_number(numero, company_key="SEPID", since=since)
        return resultado, capturado

    def test_el_remito_existente_y_posterior_sirve_de_respaldo(self):
        resultado, capturado = self._run("RTA R 00004-00004893", rows=[self.FILA])

        self.assertEqual(resultado["status"], "found")
        self.assertEqual(resultado["documents"][0]["remitoNumber"], "RTA R 00004-00004893")
        # El número se busca normalizado: da igual cómo lo haya escrito quien lo cargó.
        self.assertIn("N'00004'", capturado["sql"])
        self.assertIn("N'00004893'", capturado["sql"])

    def test_un_numero_que_no_existe_no_respalda_nada(self):
        resultado, _ = self._run("00004-00009999")

        self.assertEqual(resultado["status"], "not_found")
        self.assertIn("00004-00009999", resultado["reason"])

    def test_una_factura_no_es_un_remito_de_salida(self):
        resultado, _ = self._run("00004-00004893", rows=[{**self.FILA, "Comprobante_Tipo": "FAV"}])

        self.assertEqual(resultado["status"], "mismatch")
        self.assertIn("no un remito de salida", resultado["reason"])

    def test_un_remito_anterior_al_ingreso_no_pudo_ser_esa_salida(self):
        resultado, _ = self._run(
            "00004-00004893", rows=[{**self.FILA, "Comprobante_FechaEmision": "2026-05-30"}]
        )

        self.assertEqual(resultado["status"], "mismatch")
        self.assertIn("anterior al ingreso", resultado["reason"])

    def test_un_numero_ilegible_se_rechaza_sin_consultar_bejerman(self):
        resultado, capturado = self._run("no me acuerdo")

        self.assertEqual(resultado["status"], "invalid")
        self.assertNotIn("sql", capturado)



class ConflictosPendientesTests(SimpleTestCase):
    """Cerrar la orden vieja por cualquier camino tiene que sacar el conflicto de la lista.

    Sólo esta pantalla marca el conflicto como resuelto, pero la orden anterior se puede cerrar
    de varias formas (entrega, corrección histórica, baja). Si el pendiente se derivara nada más
    del estado del conflicto, esos casos quedarían en el panel para siempre.
    """

    def test_las_dos_consultas_ignoran_los_conflictos_con_la_orden_anterior_cerrada(self):
        from service import bejerman_ingreso_conflicts as conflictos

        capturado = []

        def _rows(sql, params=None):
            capturado.append(sql)
            return []

        with (
            patch.object(conflictos, "conflicts_schema_ready", return_value=True),
            patch.object(conflictos, "_rows", side_effect=_rows),
        ):
            conflictos.pending_conflict_ingreso_ids([102])
            conflictos.list_pending_conflicts()

        self.assertEqual(len(capturado), 2)
        for sql in capturado:
            self.assertIn("NOT IN", sql)
            for estado in conflictos.CLOSED_INGRESO_STATES:
                self.assertIn(f"'{estado}'", sql)
