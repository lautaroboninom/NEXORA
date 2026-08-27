"""Buscar el remito por número de serie en vez de por número de comprobante.

Módulo de puros `SimpleTestCase`: el acceso a Postgres y el canal SQL a SAMTRONIC van
mockeados, así que corre sin contenedor.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from service import bejerman_remito_lookup as lookup


EQUIPO = {
    "device_id": 7,
    "numero_serie": "MZJ5S121220",
    "numero_interno": "MG 6416",
    "marca": "Longfian",
    "modelo": "JAY-5",
}


def _fila(
    numero="00004833",
    fecha="2026-07-24",
    tipo="RSS",
    punto="00004",
    partida="MZJ5S121220",
    articulo="1115003",
    descripcion="Concentrador de oxígeno",
    cantidad="-1",
    renglon="1",
):
    return {
        "comprobante_id": "10919",
        "tipo": tipo,
        "letra": "R",
        "punto": punto,
        "numero": numero,
        "fecha": fecha,
        "cliente_codigo": "MEDAUX",
        "cliente_nombre": "MEDICINA AUXILIAR SRL",
        "cliente_cuit": "30-11111111-1",
        "observaciones": "Entrega en domicilio",
        "renglon": renglon,
        "partida": partida,
        "articulo": articulo,
        "descripcion": descripcion,
        "cantidad": cantidad,
        "deposito": "STR",
    }


class DocumentKeyTests(SimpleTestCase):
    def test_lee_punto_y_numero_de_cualquier_formato(self):
        self.assertEqual(lookup._document_key("RSS R 00004-00004833"), ("00004", "00004833"))
        self.assertEqual(lookup._document_key("4-4833"), ("00004", "00004833"))

    def test_ignora_texto_sin_numero_de_comprobante(self):
        self.assertIsNone(lookup._document_key("pendiente"))

    def test_saca_lo_que_podria_cerrar_el_literal_sql(self):
        self.assertEqual(lookup._sanitize_serial("D9#1807'; DROP TABLE"), "D9#1807 DROP TABLE")


class SearchRemitosBySerialTests(SimpleTestCase):
    def _buscar(
        self,
        filas,
        *,
        usados=None,
        equipo=EQUIPO,
        flow="service_release",
        since="2026-07-01",
        clientes=None,
        **kwargs,
    ):
        with (
            patch.object(lookup, "resolve_equipment_by_identifier", return_value=dict(equipo) if equipo else None),
            patch.object(lookup, "_remote_rows", return_value=list(filas)) as remote,
            patch.object(lookup, "used_document_keys_for_serial", return_value=set(usados or ())),
            patch.object(lookup, "ingreso_reference_date", return_value=""),
            patch.object(lookup, "local_customers_by_code", return_value=dict(clientes or {})),
        ):
            resultado = lookup.search_remitos_by_serial("MG 6416", flow=flow, since=since, **kwargs)
        resultado["_remote_call"] = remote.call_args
        return resultado

    def test_devuelve_el_remito_que_cita_la_serie(self):
        resultado = self._buscar([_fila()])

        self.assertEqual(resultado["status"], "ok")
        self.assertEqual([c["remitoNumber"] for c in resultado["candidates"]], ["RSS R 00004-00004833"])
        self.assertEqual(resultado["serial"], "MZJ5S121220")

    def test_encuentra_el_equipo_por_mg(self):
        resultado = self._buscar([_fila()])

        self.assertEqual(resultado["equipment"]["numero_interno"], "MG 6416")

    def test_no_ofrece_el_remito_ya_asociado_a_esa_serie(self):
        resultado = self._buscar([_fila()], usados={("00004", "00004833")})

        self.assertEqual(resultado["candidates"], [])
        self.assertEqual([c["remitoNumber"] for c in resultado["alreadyLinked"]], ["RSS R 00004-00004833"])

    def test_el_mismo_numero_sirve_para_otra_serie(self):
        # Un remito puede llevar varios equipos: el número se repite legítimamente en
        # otra OS. La exclusión mira la serie, no el número suelto.
        resultado = self._buscar([_fila()], usados={("00004", "00009999")})

        self.assertEqual([c["remitoNumber"] for c in resultado["candidates"]], ["RSS R 00004-00004833"])

    def test_la_fecha_de_ingreso_viaja_como_filtro_a_bejerman(self):
        resultado = self._buscar([_fila()], since="2026-07-10")

        _args, kwargs = resultado["_remote_call"]
        self.assertEqual(_args[2], "2026-07-10")

    def test_una_fecha_invalida_no_se_manda(self):
        resultado = self._buscar([_fila()], since="ayer")

        _args, _kwargs = resultado["_remote_call"]
        self.assertEqual(_args[2], "")

    def test_pide_rss_para_entregas_y_rta_para_alquiler(self):
        entrega = self._buscar([_fila()])
        alquiler = self._buscar([_fila(tipo="RTA")], flow="rental")

        self.assertEqual(entrega["_remote_call"][0][1], ("RSS",))
        self.assertEqual(alquiler["_remote_call"][0][1], ("RTA",))

    def test_agrupa_los_renglones_del_mismo_comprobante(self):
        resultado = self._buscar([_fila(), _fila()])

        self.assertEqual(len(resultado["candidates"]), 1)

    def test_trae_el_cliente_local_del_remito(self):
        # El operador elige el RTA y con eso ya sabe a quién salió: el código de Bejerman
        # no le sirve, el campo "¿A quién?" se escribe con la razón social del maestro.
        cliente = {"id": 12, "code": "MEDAUX", "name": "Medicina Auxiliar S.R.L.", "alias": "MedAux", "matched": True}
        resultado = self._buscar([_fila(tipo="RTA")], flow="rental", clientes={"MEDAUX": cliente})

        candidato = resultado["candidates"][0]
        self.assertEqual(candidato["customerCode"], "MEDAUX")
        self.assertEqual(candidato["customerName"], "MEDICINA AUXILIAR SRL")
        self.assertEqual(candidato["customer"], cliente)

    def test_sin_cliente_en_el_maestro_el_candidato_igual_sale(self):
        resultado = self._buscar([_fila(tipo="RTA")], flow="rental", clientes={})

        candidato = resultado["candidates"][0]
        self.assertIsNone(candidato["customer"])
        self.assertEqual(candidato["customerName"], "MEDICINA AUXILIAR SRL")

    def test_sin_equipo_no_consulta_bejerman(self):
        with (
            patch.object(lookup, "resolve_equipment_by_identifier", return_value=None),
            patch.object(lookup, "_remote_rows") as remote,
        ):
            resultado = lookup.search_remitos_by_serial("", flow="service_release")

        self.assertEqual(resultado["status"], "equipment_not_found")
        remote.assert_not_called()

    def test_trae_todos_los_renglones_del_comprobante(self):
        # Antes se devolvía sólo la línea del equipo: sin el resto, el operador no podía
        # saber qué más llevaba el remito que estaba por asociar.
        resultado = self._buscar([
            _fila(),
            _fila(partida="D9#1807", articulo="1115001", descripcion="Humidificador", renglon="2"),
        ])

        candidato = resultado["candidates"][0]
        self.assertEqual(candidato["lineCount"], 2)
        self.assertEqual([linea["partida"] for linea in candidato["lines"]], ["MZJ5S121220", "D9#1807"])
        self.assertEqual([linea["isSearchedItem"] for linea in candidato["lines"]], [True, False])
        self.assertEqual(candidato["otherItemCount"], 1)

    def test_el_renglon_del_equipo_manda_sobre_los_demas(self):
        # El artículo del candidato es el del equipo buscado, no el del primer renglón:
        # es el dato con el que se cuelga la partida en la carga manual.
        resultado = self._buscar([
            _fila(partida="D9#1807", articulo="1115001", descripcion="Humidificador", renglon="1"),
            _fila(renglon="2"),
        ])

        candidato = resultado["candidates"][0]
        self.assertEqual(candidato["partida"], "MZJ5S121220")
        self.assertEqual(candidato["articleCode"], "1115003")

    def test_la_cantidad_sale_positiva(self):
        # El signo de Bejerman es dirección de stock, no cantidad.
        resultado = self._buscar([_fila(cantidad="-1")])

        self.assertEqual(resultado["candidates"][0]["lines"][0]["quantity"], 1.0)

    def test_devuelve_la_cabecera_del_comprobante(self):
        candidato = self._buscar([_fila()])["candidates"][0]

        self.assertEqual(candidato["customerCuit"], "30-11111111-1")
        self.assertEqual(candidato["notes"], "Entrega en domicilio")
        self.assertEqual(candidato["documentId"], "10919")

    def test_avisa_si_el_canal_a_bejerman_esta_apagado(self):
        with (
            patch.object(lookup, "resolve_equipment_by_identifier", return_value=dict(EQUIPO)),
            patch.object(lookup, "used_document_keys_for_serial", return_value=set()),
            patch.object(lookup, "ingreso_reference_date", return_value=""),
            patch.object(lookup, "_remote_rows", side_effect=lambda *a, **k: (a[4].update({"status": "disabled"}), [])[1]),
            patch.object(lookup, "local_customers_by_code", return_value={}),
        ):
            resultado = lookup.search_remitos_by_serial("MG 6416", flow="service_release")

        self.assertEqual(resultado["status"], "disabled")
        self.assertEqual(resultado["candidates"], [])


class RemoteSqlTests(SimpleTestCase):
    """La consulta tiene que traer el comprobante entero, no sólo el renglón que matchea."""

    def _sql(self, since=""):
        capturado = {}

        def fake(sql, **kwargs):
            capturado["sql"] = sql
            return []

        with patch("service.bejerman_delivery._remote_bejerman_sql_csv", side_effect=fake):
            lookup._remote_rows("MZJ5S121220", ("RTA",), since, None, {})
        return capturado["sql"]

    def test_pide_todos_los_renglones_de_los_comprobantes_que_citan_la_partida(self):
        sql = self._sql()

        self.assertIn("WHERE c.scv_ID IN (", sql)
        self.assertIn("UPPER(LTRIM(RTRIM(COALESCE(d2.sdvstp_Partida, '')))) = UPPER('MZJ5S121220')", sql)
        self.assertIn("d.sdv_CantUM1", sql)
        self.assertIn("d.sdvdep_Cod", sql)

    def test_la_fecha_filtra_dentro_de_la_subconsulta(self):
        sql = self._sql(since="2026-07-10")

        self.assertIn("AND c2.scv_FEmision >= '2026-07-10'", sql)
        self.assertIn("GROUP BY c2.scv_ID", sql)
