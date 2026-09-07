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

    def test_las_os_sin_presupuesto_no_se_ven_afectadas(self):
        # Garantías, equipos propios y todo lo que se repara sin presupuestar: el corte
        # sólo mira el presupuesto ya emitido, nunca el que no existe.
        for presupuesto in (None, "", "pendiente", "no_aplica"):
            with self.subTest(presupuesto=presupuesto):
                self.assertFalse(presupuesto_bloquea_salida("reparado", presupuesto))

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
