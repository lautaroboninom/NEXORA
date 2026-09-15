"""El mismo equipo cargado dos veces el mismo día.

El caso real: recepción cargó cuatro concentradores de MERNO como baja de alquiler, la pantalla
se colgó esperando el PDF del remito y volvió a cargar los mismos cuatro. Salieron ocho OS —
29711 a 29718— con las mismas cuatro series, el mismo cliente y el mismo día. El lote pedía
`_allow_open_duplicate` para no trabar un remito por una OS vieja abierta, y ese permiso tapaba
también este caso, que no es un conflicto sino un error de carga.
"""

from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from service.bejerman_sdk import BejermanSdkResponseError
from service.bejerman_ris import (
    _ensure_equipment_remito_not_duplicated,
    _local_ingress_equipment_remito_duplicate,
    _remote_duplicate_already_registered,
    _validate_equipment_remito_not_duplicated,
)
from service.ingreso_duplicados import ingreso_es_de_hoy
from service.views.ingresos_views import (
    NuevoIngresoLoteError,
    NuevoIngresoLoteView,
    _ingreso_duplicado_hoy_response,
)


class IngresoEsDeHoyTests(SimpleTestCase):
    def test_la_os_creada_hoy_es_una_recarga_del_mismo_dia(self):
        self.assertTrue(ingreso_es_de_hoy({"fecha_creacion": timezone.now()}))

    def test_la_os_de_ayer_sigue_siendo_un_conflicto_legitimo(self):
        ayer = timezone.now() - timedelta(days=1)
        self.assertFalse(ingreso_es_de_hoy({"fecha_creacion": ayer, "fecha_ingreso": ayer}))

    def test_alcanza_con_que_una_de_las_dos_fechas_sea_de_hoy(self):
        # `fecha_ingreso` se puede escribir a mano: una OS cargada hoy con fecha vieja sigue
        # siendo la misma carga repetida, y una fecha de ingreso de hoy sobre una OS vieja
        # también cuenta.
        vieja = timezone.now() - timedelta(days=30)
        self.assertTrue(ingreso_es_de_hoy({"fecha_creacion": timezone.now(), "fecha_ingreso": vieja}))
        self.assertTrue(ingreso_es_de_hoy({"fecha_creacion": vieja, "fecha_ingreso": timezone.localdate()}))

    def test_una_fecha_sin_hora_se_compara_igual(self):
        self.assertTrue(ingreso_es_de_hoy({"fecha_ingreso": timezone.localdate()}))
        self.assertFalse(ingreso_es_de_hoy({"fecha_ingreso": date(2020, 1, 1)}))

    def test_una_hora_de_la_madrugada_no_se_corre_de_dia_por_la_zona_horaria(self):
        """En UTC son las 02:00 del día siguiente; para la casa sigue siendo hoy."""

        hoy = timezone.localdate()
        madrugada = timezone.make_aware(
            datetime.combine(hoy, datetime.min.time()).replace(hour=23, minute=30),
            timezone.get_current_timezone(),
        )
        self.assertTrue(ingreso_es_de_hoy({"fecha_creacion": madrugada}))

    def test_sin_fechas_no_se_inventa_un_duplicado(self):
        self.assertFalse(ingreso_es_de_hoy({}))
        self.assertFalse(ingreso_es_de_hoy(None))
        self.assertFalse(ingreso_es_de_hoy({"fecha_creacion": None, "fecha_ingreso": ""}))


class DuplicadoHoyResponseTests(SimpleTestCase):
    def test_nombra_la_os_que_ya_existe_en_vez_de_crear_otra(self):
        dup = {"id": 29712, "fecha_ingreso": datetime(2026, 9, 7, 10, 30)}

        response = _ingreso_duplicado_hoy_response(
            dup,
            numero_serie="S12BEA05013",
            numero_interno="",
            tipo_equipo="Concentrador de oxígeno",
        )

        # 409 y no 200: no se reutiliza la OS ni se crea una nueva, se frena.
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["conflict_type"], "INGRESO_DUPLICADO_HOY")
        self.assertIn("S12BEA05013", response.data["detail"])
        self.assertIn("29712", response.data["detail"])
        self.assertEqual(response.data["payload"]["ingreso_id"], 29712)
        self.assertEqual(response.data["payload"]["os"], "29712")
        self.assertEqual(response.data["payload"]["numero_serie"], "S12BEA05013")

    def test_un_equipo_sin_serie_se_nombra_por_su_numero_interno(self):
        response = _ingreso_duplicado_hoy_response(
            {"id": 29719, "fecha_creacion": datetime(2026, 9, 7, 9, 0)},
            numero_serie="",
            numero_interno="MG 7144",
            tipo_equipo="Concentrador de oxígeno",
        )

        self.assertIn("MG 7144", response.data["detail"])
        self.assertEqual(response.data["payload"]["numero_interno"], "MG 7144")


def _lote_request(items):
    return SimpleNamespace(
        data={"cliente": {"id": 1}, "empresa_bejerman": "SEPID", "items": items},
        user=SimpleNamespace(id=7),
        user_id=7,
        user_role="recepcion",
    )


class LoteDuplicadoHoyTests(SimpleTestCase):
    """El lote sí se frena entero: es el mismo remito, no seis equipos independientes."""

    def test_el_remito_repetido_no_crea_ninguna_os_y_dice_cual_equipo_es(self):
        respuestas = [
            SimpleNamespace(data={"ok": True, "ingreso_id": 101}, status_code=201),
            SimpleNamespace(
                data={
                    "detail": "El equipo S12BEA05013 ya se ingresó hoy en la OS 29712.",
                    "conflict_type": "INGRESO_DUPLICADO_HOY",
                    "payload": {"ingreso_id": 29712, "os": "29712"},
                },
                status_code=409,
            ),
        ]

        with (
            patch("service.views.ingresos_views.require_any_permission"),
            patch("service.views.ingresos_views.transaction.atomic", return_value=nullcontext()),
            patch(
                "service.views.ingresos_views.preflight_ris_for_request_payload",
                return_value={"can_emit": True},
            ),
            patch("service.views.ingresos_views.NuevoIngresoView.post", side_effect=respuestas),
            patch("service.views.ingresos_views.emit_or_get_ris_batch") as emit,
        ):
            response = NuevoIngresoLoteView().post(_lote_request([{}, {}]))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["conflict_type"], "INGRESO_DUPLICADO_HOY")
        # El operador tiene seis equipos sobre la mesa: hay que decirle cuál es.
        self.assertEqual(response.data["item_index"], 1)
        self.assertIn("29712", response.data["detail"])
        # Nada de emitir: el remito anterior ya salió.
        emit.assert_not_called()

    def test_el_error_del_hijo_viaja_entero_hasta_la_pantalla(self):
        error = NuevoIngresoLoteError(
            {"detail": "ya se ingresó hoy", "conflict_type": "INGRESO_DUPLICADO_HOY"},
            409,
            2,
        )

        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.item_index, 2)
        self.assertEqual(error.payload["conflict_type"], "INGRESO_DUPLICADO_HOY")


def _rda_payload(serial="MZJ5S121193"):
    return {
        "companyKey": "SEPID",
        "issueDate": "2026-09-07",
        "documentProfile": {"type": "RDA"},
        "equipments": [{"serial": serial, "internalNumber": ""}],
    }


def _remoto_encontrado(numero="00004367"):
    return {
        "source": "remote",
        "identifier": "MZJ5S121193",
        "issueDate": "2026-09-07",
        "remitoNumber": f"RDA R 00004-{numero}",
        "raw": {
            "Comprobante_Tipo": "RDA",
            "Comprobante_Letra": "R",
            "Comprobante_PtoVenta": "00004",
            "Comprobante_Numero": numero,
        },
    }


class RemitoDeUnCicloCerradoTests(SimpleTestCase):
    """El equipo que se alquiló y vuelve no es un remito emitido dos veces.

    08/09/2026: la validación previa del RDA frenaba la devolución de MZJ5S121193 porque
    Bejerman tenía un RDA de esa misma fecha. La OS anterior ya estaba cerrada —el equipo se
    había alquilado y entregado—, así que lo que volvía era un ingreso nuevo, no una segunda
    emisión.

    Lo que decide no es si el equipo tiene *alguna* otra OS abierta, sino de quién es el
    comprobante que apareció. Preguntar sólo por el equipo apaga la validación entera en el
    caso más común —una sola OS, la que se está emitiendo—, que es justo donde vive el
    duplicado real: el reintento de una emisión que en Bejerman ya había entrado.
    """

    def test_el_remito_que_otra_os_ya_tiene_anotado_no_frena_la_devolucion(self):
        issues = []

        with (
            patch("service.bejerman_ris._local_ingress_equipment_remito_duplicate", return_value=None),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=object()),
            patch(
                "service.bejerman_ris._remote_ingress_equipment_remito_duplicate",
                return_value=_remoto_encontrado(),
            ),
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value={"?column?": 1}) as consulta,
        ):
            _validate_equipment_remito_not_duplicated(_rda_payload(), issues, ingreso_ids=[10])

        self.assertEqual(issues, [])
        tipo, punto, numero, excluidos = consulta.call_args.args[1]
        self.assertEqual((tipo, punto, numero), ("RDA", 4, 4367))
        self.assertEqual(excluidos, [10])

    def test_el_remito_que_nadie_reclama_se_sigue_frenando(self):
        """La protección de fondo: una sola OS reintentando algo que ya entró en Bejerman."""

        issues = []

        with (
            patch("service.bejerman_ris._local_ingress_equipment_remito_duplicate", return_value=None),
            patch("service.bejerman_ris.BejermanSDKClient", return_value=object()),
            patch(
                "service.bejerman_ris._remote_ingress_equipment_remito_duplicate",
                return_value=_remoto_encontrado(),
            ),
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value=None),
        ):
            _validate_equipment_remito_not_duplicated(_rda_payload(), issues, ingreso_ids=[10])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "INGRESO_REMITO_EQUIPMENT_DUPLICATE_REMOTE")
        self.assertIn("RDA R 00004-00004367", issues[0]["message"])

    def test_el_duplicado_local_se_sigue_frenando(self):
        issues = []
        duplicado = {
            "source": "local",
            "identifier": "MZJ5S121193",
            "issueDate": "2026-09-07",
            "remitoNumber": "RDA R 00004-00004367",
        }

        with patch(
            "service.bejerman_ris._local_ingress_equipment_remito_duplicate",
            return_value=duplicado,
        ):
            _validate_equipment_remito_not_duplicated(_rda_payload(), issues, ingreso_ids=[])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "INGRESO_REMITO_EQUIPMENT_DUPLICATE_LOCAL")
        self.assertIn("RDA R 00004-00004367", issues[0]["message"])

    def test_en_la_emision_el_remito_rendido_deja_pasar_y_el_huerfano_levanta(self):
        with (
            patch("service.bejerman_ris._local_ingress_equipment_remito_duplicate", return_value=None),
            patch(
                "service.bejerman_ris._remote_ingress_equipment_remito_duplicate",
                return_value=_remoto_encontrado(),
            ),
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value={"?column?": 1}),
        ):
            _ensure_equipment_remito_not_duplicated(_rda_payload(), [4321], client=None)

        with (
            patch("service.bejerman_ris._local_ingress_equipment_remito_duplicate", return_value=None),
            patch(
                "service.bejerman_ris._remote_ingress_equipment_remito_duplicate",
                return_value=_remoto_encontrado(),
            ),
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value=None),
            self.assertRaises(BejermanSdkResponseError),
        ):
            _ensure_equipment_remito_not_duplicated(_rda_payload(), [4321], client=None)


class RemitoRendidoPorOtraOsTests(SimpleTestCase):
    def test_sin_referencia_de_comprobante_no_se_consulta_nada(self):
        """Sin tipo/punto/número no hay con qué buscar: se decide como si nadie lo reclamara."""

        with (
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q") as consulta,
        ):
            self.assertFalse(_remote_duplicate_already_registered({"remitoNumber": "RDA R 4-7"}, [1]))
            self.assertFalse(_remote_duplicate_already_registered(None, [1]))
        consulta.assert_not_called()

    def test_sin_ingresos_propios_que_excluir_la_consulta_no_lleva_ese_filtro(self):
        with (
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value=None) as consulta,
        ):
            self.assertFalse(_remote_duplicate_already_registered(_remoto_encontrado(), []))

        sql = consulta.call_args.args[0]
        self.assertNotIn("r.ingreso_id <> ALL(%s)", sql)
        self.assertEqual(len(consulta.call_args.args[1]), 3)


class DuplicadoLocalSoloConLaOsAbiertaTests(SimpleTestCase):
    """El remito de una OS ya cerrada es de un ciclo terminado, no un duplicado."""

    def test_la_consulta_deja_afuera_las_os_cerradas(self):
        with (
            patch("service.bejerman_ris._has_ris_schema", return_value=True),
            patch("service.bejerman_ris.q", return_value=None) as consulta,
        ):
            self.assertIsNone(_local_ingress_equipment_remito_duplicate(_rda_payload(), [10]))

        sql, params = consulta.call_args.args[0], consulta.call_args.args[1]
        self.assertIn("t.estado", sql)
        estados = params[4]
        self.assertIn("alquilado", estados)
        self.assertIn("entregado", estados)
        self.assertEqual(params[5], [10])
