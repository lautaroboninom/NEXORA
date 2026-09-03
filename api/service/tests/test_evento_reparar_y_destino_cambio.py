"""Dos cortes que faltaban en el circuito de reparación y de cambio de equipo.

1. **El evento `reparar`**: `metricas_views` calcula el MTTR con un INNER JOIN contra
   `ingreso_events WHERE a_estado='reparar'`, y nadie escribía nunca ese evento, así que la
   métrica daba cero. Es el único estado que se leía sin escribirse.

2. **El destino del equipo recibido**: `mg`, `venta` y `desguace` pasan el equipo a
   propiedad de SEPID (y los dos últimos además mueven stock real en Bejerman: VAL y DES).
   Esa misma conversión, por su endpoint propio, exige `baja_alta` o
   `devices_preventivos.manage`; sin este chequeo el cambio de equipo era la puerta de atrás
   para recepción, que tiene `device_swap` y ninguno de los otros dos.

`SimpleTestCase` con el acceso a base mockeado: se prueba la decisión, no la escritura.
"""

from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from rest_framework.exceptions import PermissionDenied

from service.management.commands.backfill_ingreso_events_desde_audit import ESTADOS_A_RECUPERAR
from service.views import ingresos_views as iv


class EventoRepararTests(SimpleTestCase):
    def _marcar(self, estado_cur):
        view = iv.MarcarParaRepararView()
        request = RequestFactory().post("/", {})
        request.user = type("U", (), {"id": 3})()

        fila = {
            "estado": estado_cur,
            "presupuesto_estado": "aprobado",
            "asignado_a": 9,
            "motivo": "reparación",
            "permite_reparacion": True,
        }
        with (
            patch.object(iv, "require_roles"),
            patch.object(iv, "_rol", return_value="jefe"),
            patch.object(iv, "_set_audit_user"),
            patch.object(iv, "_ingresos_has_permite_reparacion_col", return_value=True),
            patch.object(iv, "q", return_value=fila),
            patch.object(iv, "exec_void"),
            patch.object(iv, "registrar_ingreso_event") as evento,
            patch.object(iv, "notify_ingreso_para_reparar", create=True),
        ):
            try:
                view.post(request, 42)
            except Exception:
                # Lo que sigue al UPDATE (mails, notificaciones) no es parte de este corte.
                pass
        return evento

    def test_deja_el_evento_que_arranca_el_mttr(self):
        evento = self._marcar("presupuestado")

        evento.assert_called_once()
        args, kwargs = evento.call_args
        self.assertEqual(args[0], 42)
        self.assertEqual(args[1], "reparar")
        self.assertEqual(kwargs.get("de_estado"), "presupuestado")

    def test_no_duplica_el_evento_si_ya_estaba_en_reparar(self):
        self._marcar("reparar").assert_not_called()

    def test_el_backfill_recupera_el_historico_de_reparar(self):
        # Sin esto el MTTR arrancaría recién desde el deploy.
        self.assertIn("reparar", ESTADOS_A_RECUPERAR)
        self.assertIn("diagnosticado", ESTADOS_A_RECUPERAR)


class DestinoDelEquipoRecibidoTests(SimpleTestCase):
    """Sólo el gate de permiso: el resto del POST ya lo cubre `test_device_swap`."""

    # `post` está decorado con `@transaction.atomic`, que abre conexión antes de ejecutar
    # nada: se llama la función de adentro para no necesitar base. El gate que se prueba está
    # en el cuerpo, así que saltear la transacción no cambia lo que se mide.
    _post_sin_transaccion = staticmethod(iv.IngresoCambioEquipoView.post.__wrapped__)

    def _post(self, destino, *, autorizado):
        view = iv.IngresoCambioEquipoView()
        request = RequestFactory().post("/", {})
        request.data = {"destino_viejo": destino, "sin_entrega": True}

        def _require_any(_request, permisos):
            if not autorizado:
                raise PermissionDenied("No autorizado")

        with (
            patch.object(iv, "require_permission"),
            patch.object(iv, "require_any_permission", side_effect=_require_any) as gate,
            patch.object(iv, "_has_device_swap_schema", return_value=True),
            patch.object(iv, "_set_audit_user"),
            # Corta acá: lo único que se prueba es si el gate se aplicó antes de tocar nada.
            patch.object(iv, "q", return_value=None),
        ):
            try:
                response = self._post_sin_transaccion(view, request, 42)
            except PermissionDenied:
                return gate, "denegado"
        return gate, response

    def test_pendiente_no_pide_permiso_extra(self):
        gate, response = self._post("pendiente", autorizado=False)

        gate.assert_not_called()
        # Llegó hasta la búsqueda del ingreso (mockeada en None → 404).
        self.assertEqual(getattr(response, "status_code", None), 404)

    def test_los_destinos_de_propiedad_sepid_piden_el_permiso_patrimonial(self):
        for destino in ("mg", "venta", "desguace"):
            with self.subTest(destino=destino):
                gate, resultado = self._post(destino, autorizado=False)

                self.assertEqual(resultado, "denegado")
                gate.assert_called_once()
                self.assertEqual(
                    list(gate.call_args.args[1]),
                    ["action.ingreso.baja_alta", "action.devices_preventivos.manage"],
                )

    def test_con_el_permiso_el_destino_firme_sigue_andando(self):
        gate, response = self._post("mg", autorizado=True)

        gate.assert_called_once()
        self.assertEqual(getattr(response, "status_code", None), 404)

    def test_un_destino_invalido_se_rechaza_antes_del_gate(self):
        gate, response = self._post("cualquiera", autorizado=False)

        gate.assert_not_called()
        self.assertEqual(getattr(response, "status_code", None), 400)

    def test_el_unico_destino_sin_gate_es_pendiente(self):
        # Si mañana se agrega un destino que pasa el equipo a nuestro nombre y no entra en
        # la lista patrimonial, recepción vuelve a tener la puerta de atrás.
        self.assertEqual(
            set(iv.DEVICE_SWAP_DESTINOS) - set(iv.DEVICE_SWAP_DESTINOS_PATRIMONIALES),
            {"pendiente"},
        )

    def test_los_destinos_con_movimiento_de_stock_son_patrimoniales(self):
        # Mover stock sin haber pasado el equipo a nuestro patrimonio sería mover la unidad
        # de un cliente; el orden del POST (convertir y después encolar) depende de esto.
        self.assertTrue(
            set(iv.DEVICE_SWAP_DESTINOS_CON_MOVIMIENTO)
            <= set(iv.DEVICE_SWAP_DESTINOS_PATRIMONIALES)
        )
        self.assertEqual(set(iv.DEVICE_SWAP_DESTINOS_CON_MOVIMIENTO), {"venta", "desguace"})
