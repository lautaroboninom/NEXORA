from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase, override_settings

from decimal import Decimal

from reportlab.lib.units import mm

from service.pdf import (
    _exit_order_date_context,
    _exit_order_net_charge,
    _exit_order_observation_blocks,
    _exit_order_observations,
    _layout_observation_columns,
)


class ExitOrderPdfContentTests(SimpleTestCase):
    @override_settings(TIME_ZONE="America/Argentina/Buenos_Aires", USE_TZ=True)
    def test_vendido_entregado_usa_fecha_historica_de_venta(self):
        label, value = _exit_order_date_context(
            {
                "ingreso_estado": "vendido_entregado",
                "fecha_venta": datetime(2026, 7, 21, 15, 30, tzinfo=dt_timezone.utc),
                "mg_venta_fecha": datetime(2026, 7, 22, tzinfo=dt_timezone.utc),
            }
        )

        self.assertEqual(label, "Fecha venta")
        self.assertEqual(value, "21/07/26")

    def test_comprobante_distingue_problema_de_trabajos_realizados(self):
        observations = _exit_order_observations(
            {
                "descripcion_problema": "El equipo no concentraba correctamente.",
                "trabajos_realizados": "Se cambió el kit y se verificó la concentración.",
            }
        )

        self.assertIn("Descripción del problema:\nEl equipo no concentraba correctamente.", observations)
        self.assertIn("Trabajos realizados:\nSe cambió el kit", observations)
        self.assertNotIn("Trabajos a realizar", observations)


class ExitOrderNetChargeTests(SimpleTestCase):
    """La caja "Costo neto" es lo que se cobra, no lo que decia el presupuesto."""

    def test_presupuesto_rechazado_cobra_la_revision_aunque_la_resolucion_sea_otra(self):
        # OS 29471: el presupuesto quedo rechazado pero el equipo salio por "No
        # se repara", asi que la resolucion es `no_reparado`. Se cobra la
        # revision, no los $879.046,95 del presupuesto que el cliente rechazo.
        neto = _exit_order_net_charge(
            {
                "resolucion": "no_reparado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("12800.00"))

    def test_resolucion_presupuesto_rechazado_sigue_cobrando_la_revision(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "presupuesto_rechazado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("12800.00"))

    def test_presupuesto_vigente_cobra_el_presupuesto(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "reparado",
                "quote_estado": "aprobado",
                "subtotal": Decimal("173970.00"),
            },
            {"presupuesto_rechazado_cobro_neto": Decimal("12800.00")},
        )

        self.assertEqual(neto, Decimal("173970.00"))

    def test_sin_cobro_cargado_la_caja_va_en_blanco(self):
        neto = _exit_order_net_charge(
            {
                "resolucion": "no_reparado",
                "quote_estado": "rechazado",
                "subtotal": Decimal("879046.95"),
            },
            {"presupuesto_rechazado_cobro_neto": None},
        )

        self.assertIsNone(neto)


class ExitOrderObservationsLayoutTests(SimpleTestCase):
    """La caja de Observaciones tiene alto fijo y el texto largo libre.

    Las medidas son las que arma `render_remito_salida_pdf` para la franja del
    formulario: 174 mm de ancho por 35,8 mm de alto.
    """

    BOX_W = 174 * mm
    BOX_H = 35.8 * mm
    BASE_SIZE = 7.4
    BASE_LEADING = 3.6 * mm

    def _layout(self, head):
        return _layout_observation_columns(
            _exit_order_observation_blocks(head),
            self.BOX_W,
            self.BOX_H,
            base_size=self.BASE_SIZE,
            base_leading=self.BASE_LEADING,
        )

    @staticmethod
    def _text(columns):
        return "\n".join(line for column in columns for line, _bold, _indent in column)

    def test_os_29575_imprime_los_tres_trabajos_y_no_solo_el_primero(self):
        # A todo el ancho entraban 9 renglones: las 5 viñetas del diagnóstico y
        # un solo trabajo. Los otros dos se perdían sin dejar rastro.
        head = {
            "descripcion_problema": (
                "- NO ENCIENDE SIN ESTAR ENCHUFADO\n"
                "- SOPORTE DE BATERIAS DESPEGADO\n"
                "- OSCILACION EXCESIVA DEL VACUOMETRO\n"
                "- RUIDO AGUDO DEL MOTOR\n"
                "- CIRCUITO NEUMATICO GASTADO"
            ),
            "trabajos_realizados": (
                "- CAMBIO DE FUSIBLE DE BATERIA\n"
                "- DESMONTAJE DE BATERIAS, MOTOR Y PORTABATERIAS PARA LUEGO PEGADO "
                "DE PORTABATERIAS Y NUEVO MONTAJE\n"
                "- CAMBIO DE CIRCUITO NEUMATICO"
            ),
        }

        columns, size, _leading, _width = self._layout(head)
        rendered = self._text(columns)

        self.assertEqual(len(columns), 2)
        self.assertEqual(size, self.BASE_SIZE)
        self.assertIn("CAMBIO DE CIRCUITO NEUMATICO", rendered)
        self.assertIn("CIRCUITO NEUMATICO GASTADO", rendered)
        self.assertFalse(rendered.rstrip().endswith("..."))

    def test_cada_seccion_va_en_su_columna(self):
        columns, _size, _leading, _width = self._layout(
            {
                "descripcion_problema": "No enciende.",
                "trabajos_realizados": "Se cambió la fuente.",
            }
        )

        self.assertEqual(len(columns), 2)
        self.assertIn("Descripción del problema:", self._text([columns[0]]))
        self.assertIn("Trabajos realizados:", self._text([columns[1]]))
        self.assertNotIn("Trabajos realizados:", self._text([columns[0]]))

    def test_una_sola_seccion_usa_todo_el_ancho(self):
        columns, _size, _leading, width = self._layout(
            {"descripcion_problema": "- NO ENCIENDE\n- PIERDE PRESION", "trabajos_realizados": ""}
        )

        self.assertEqual(len(columns), 1)
        self.assertGreater(width, self.BOX_W / 2)

    def test_la_vineta_que_no_entra_sigue_sangrada(self):
        # Sin sangría, la segunda mitad de un trabajo largo arranca pegada al
        # margen y se lee como si fuera otro ítem de la lista.
        columns, _size, _leading, _width = self._layout(
            {
                "descripcion_problema": "- NO ENCIENDE",
                "trabajos_realizados": (
                    "- DESMONTAJE DE BATERIAS, MOTOR Y PORTABATERIAS PARA LUEGO "
                    "PEGADO DE PORTABATERIAS Y NUEVO MONTAJE"
                ),
            }
        )

        sangrados = [
            indent for column in columns for _line, _bold, indent in column if indent > 0
        ]
        self.assertTrue(sangrados)

    def test_texto_desmedido_avisa_que_quedo_cortado(self):
        # Sólo pasa con datos rotos (importaciones viejas que se comieron un CSV),
        # pero el papel tiene que decir que falta texto en vez de mentir.
        head = {
            "descripcion_problema": "\n".join(
                f"- FALLA {i:03d} DETECTADA EN LA REVISION INICIAL COMPLETA DEL EQUIPO"
                for i in range(60)
            ),
            "trabajos_realizados": "- SE REEMPLAZO EL MODULO",
        }

        columns, _size, _leading, _width = self._layout(head)

        self.assertTrue(self._text(columns).rstrip().endswith("..."))

    def test_sin_diagnostico_ni_trabajos_la_caja_queda_con_el_guion(self):
        columns, _size, _leading, _width = self._layout(
            {"descripcion_problema": "", "trabajos_realizados": ""}
        )

        self.assertEqual(self._text(columns).strip(), "-")
