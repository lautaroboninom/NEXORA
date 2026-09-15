"""La orden de salida no se emite con el presupuesto sin aprobar.

Caso del 07/09/2026 (OS 29666): el equipo se marcó reparado el 2/9, el presupuesto se
emitió el 3/9 y ese mismo día se imprimió la orden de salida. La OS quedó `liberado` con
el presupuesto todavía en `presupuestado`: se entregó -y se mandó a facturar- un trabajo
que el cliente nunca aprobó. No había ningún control que mirara el presupuesto entre
"reparado" y "liberado".

Son SimpleTestCase a propósito: el corte vive en una función pura y la view se ejercita
sin su decorador `@transaction.atomic`, así que no necesitan Postgres.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from service.views.reportes_views import (
    ESTADOS_SALIDA_YA_EMITIDA,
    RemitoSalidaPdfView,
    ingreso_requiere_presupuesto,
    presupuesto_bloquea_salida,
)


def _without_atomic(method):
    while hasattr(method, "__wrapped__"):
        method = method.__wrapped__
    return method


def _request():
    return SimpleNamespace(
        data={},
        user=SimpleNamespace(id=7, nombre="Operador"),
        user_id=7,
        query_params={},
        GET={},
    )


class PresupuestoBloqueaSalidaTests(SimpleTestCase):
    def test_anulado_o_desconocido_no_habilitan_una_reparacion_cobrable(self):
        for presupuesto in ("anulado", "desconocido", "emitido"):
            with self.subTest(presupuesto=presupuesto):
                self.assertTrue(presupuesto_bloquea_salida("reparado", presupuesto))

    def test_emitido_sin_respuesta_bloquea_tambien_las_excepciones(self):
        self.assertTrue(presupuesto_bloquea_salida(
            "reparado", "emitido", requiere_presupuesto=False
        ))

    def test_presupuesto_emitido_sin_respuesta_frena_la_salida(self):
        self.assertTrue(presupuesto_bloquea_salida("reparado", "presupuestado"))

    def test_tambien_frena_las_otras_resoluciones_de_taller(self):
        # Devolver el equipo sin repararlo con el presupuesto en el aire es el mismo
        # problema: primero hay que rechazarlo formalmente.
        for estado in ("controlado_sin_defecto", "no_se_repara"):
            with self.subTest(estado=estado):
                self.assertTrue(presupuesto_bloquea_salida(estado, "presupuestado"))

    def test_aprobado_o_rechazado_dejan_salir(self):
        for presupuesto in ("aprobado", "rechazado"):
            with self.subTest(presupuesto=presupuesto):
                self.assertFalse(presupuesto_bloquea_salida("reparado", presupuesto))

    def test_la_reparacion_que_nadie_presupuesto_tampoco_sale(self):
        # Caso del 09/09/2026 (OS 29639): el equipo de un cliente, sin garantía, reparado
        # y liberado el mismo día con el presupuesto en 'pendiente'. El corte del 07/09
        # sólo miraba 'presupuestado' y esta OS pasó de largo.
        for presupuesto in (None, "", "pendiente"):
            with self.subTest(presupuesto=presupuesto):
                self.assertTrue(presupuesto_bloquea_salida("reparado", presupuesto))

    def test_el_no_aplica_es_la_salida_del_trabajo_que_no_se_cobra(self):
        self.assertFalse(presupuesto_bloquea_salida("reparado", "no_aplica"))


    def test_lo_que_no_se_le_cobra_a_nadie_sale_sin_presupuesto(self):
        # Garantías, equipos propios, alquileres y demos se reparan sin presupuestar:
        # `requiere_presupuesto` en False es lo que los deja pasar.
        for presupuesto in (None, "", "pendiente"):
            with self.subTest(presupuesto=presupuesto):
                self.assertFalse(
                    presupuesto_bloquea_salida(
                        "reparado", presupuesto, requiere_presupuesto=False
                    )
                )

    def test_sin_reparacion_hecha_el_equipo_vuelve_igual_cuando_no_se_repara(self):
        # `no_se_repara` devuelve el equipo sin trabajo que cobrar y no frena por presupuesto pendiente.
        self.assertFalse(presupuesto_bloquea_salida("no_se_repara", "pendiente"))

    def test_controlado_sin_defecto_en_cliente_frena_si_presupuesto_sigue_pendiente(self):
        # Para el equipo de un cliente, el control sin defecto exige presupuesto cerrado (o no_aplica explícito)
        for presupuesto in (None, "", "pendiente"):
            with self.subTest(presupuesto=presupuesto):
                self.assertTrue(presupuesto_bloquea_salida("controlado_sin_defecto", presupuesto))

    def test_controlado_sin_defecto_propio_o_garantia_no_frena(self):
        self.assertFalse(
            presupuesto_bloquea_salida(
                "controlado_sin_defecto", "pendiente", requiere_presupuesto=False
            )
        )


class QuienNecesitaPresupuestoTests(SimpleTestCase):
    """De quién es el equipo y quién paga el trabajo."""

    def test_serie_con_formato_mg_no_acredita_equipo_propio(self):
        self.assertTrue(ingreso_requiere_presupuesto({"numero_serie": "MG 1234"}))

    def test_garantia_falsa_en_texto_no_exime_del_presupuesto(self):
        self.assertTrue(ingreso_requiere_presupuesto({"garantia_fabrica": "false"}))

    def test_el_equipo_de_un_cliente_sin_garantia_necesita_presupuesto(self):
        self.assertTrue(
            ingreso_requiere_presupuesto(
                {"motivo": "reparación", "numero_interno": "", "garantia_fabrica": False}
            )
        )

    def test_las_garantias_no(self):
        for campo in ("garantia_fabrica", "garantia_reparacion"):
            with self.subTest(campo=campo):
                self.assertFalse(ingreso_requiere_presupuesto({campo: True}))

    def test_los_equipos_propios_no(self):
        for numero_interno in ("MG 1234", "mg1234", "NM 12", "NV 0007", "CE 500"):
            with self.subTest(numero_interno=numero_interno):
                self.assertFalse(
                    ingreso_requiere_presupuesto({"numero_interno": numero_interno})
                )

    def test_el_mg_vendido_vuelve_a_necesitarlo(self):
        # Dejó de ser nuestro: la reparación se le cobra al cliente que lo compró.
        self.assertTrue(
            ingreso_requiere_presupuesto(
                {"numero_interno": "MG 1234", "mg_estado": "inactivo_venta"}
            )
        )

    def test_alquileres_y_demos_no(self):
        for motivo in (
            "baja alquiler",
            "reparación alquiler",
            "reparacion alquiler",
            "devolución demo",
            "cotización de equipo",
        ):
            with self.subTest(motivo=motivo):
                self.assertFalse(ingreso_requiere_presupuesto({"motivo": motivo}))

    def test_un_service_preventivo_de_un_cliente_si(self):
        # Es trabajo cobrable sobre el equipo de otro: sale con presupuesto o con un
        # "No aplica" puesto a mano.
        self.assertTrue(ingreso_requiere_presupuesto({"motivo": "service preventivo"}))

    def test_una_os_ya_liberada_puede_reimprimir(self):
        # La orden ya salió: bloquear acá sólo impediría reimprimir el papel perdido, y
        # el daño -si lo hubo- ya está hecho.
        for estado in sorted(ESTADOS_SALIDA_YA_EMITIDA):
            with self.subTest(estado=estado):
                self.assertFalse(presupuesto_bloquea_salida(estado, "presupuestado"))

    def test_no_se_escapa_por_mayusculas_ni_espacios(self):
        self.assertTrue(presupuesto_bloquea_salida("Reparado", " Presupuestado "))


class OrdenDeSalidaConPresupuestoSinAprobarTests(SimpleTestCase):
    """El corte tiene que estar antes de cualquier escritura.

    Liberar no tiene vuelta atrás: cambia el estado, notifica, encola el movimiento de
    stock y avisa que la OS quedó lista para facturar. Si el 409 llegara tarde, la OS ya
    estaría liberada igual que en el caso que originó esto.
    """

    @patch("service.views.reportes_views.get_active_derivation", return_value=None)
    @patch(
        "service.views.reportes_views.q",
        return_value={
            "estado": "reparado",
            "resolucion": "reparado",
            "presupuesto_estado": "presupuestado",
        },
    )
    @patch("service.views.reportes_views.exec_void")
    @patch("service.views.reportes_views.require_roles")
    @patch("service.views.reportes_views._set_audit_user")
    def test_devuelve_409_sin_tocar_la_os(self, _audit, _roles, exec_void, _query, _deriv):
        response = _without_atomic(RemitoSalidaPdfView.get)(
            RemitoSalidaPdfView(),
            _request(),
            29666,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "presupuesto_sin_aprobar")
        self.assertIn("Aprobalo o rechazalo", response.data["detail"])
        exec_void.assert_not_called()

    @patch("service.views.reportes_views.get_active_derivation", return_value=None)
    @patch(
        "service.views.reportes_views.q",
        return_value={
            "estado": "reparado",
            "resolucion": "reparado",
            "presupuesto_estado": "pendiente",
            "garantia_fabrica": False,
            "garantia_reparacion": False,
            "motivo": "reparación",
            "numero_interno": "",
            "mg_estado": "activo",
        },
    )
    @patch("service.views.reportes_views.exec_void")
    @patch("service.views.reportes_views.require_roles")
    @patch("service.views.reportes_views._set_audit_user")
    def test_la_reparacion_sin_presupuesto_tampoco_se_libera(self, _audit, _roles, exec_void, _query, _deriv):
        # OS 29639: el presupuesto ni siquiera se emitió. Antes esto pasaba derecho.
        response = _without_atomic(RemitoSalidaPdfView.get)(
            RemitoSalidaPdfView(),
            _request(),
            29639,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "presupuesto_sin_aprobar")
        self.assertIn("No aplica", response.data["detail"])
        exec_void.assert_not_called()
