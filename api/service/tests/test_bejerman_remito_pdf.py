"""Candados de la geometría del remito que genera NEXORA.

Las coordenadas de `bejerman_remito_pdf` no son un gusto: salieron de medir 28
remitos reales de Bejerman, y el PDF se imprime sobre papel preimpreso, así que
correr cualquiera de estos números desalinea el papel. Por eso los tests fijan
posiciones concretas en vez de comprobar que "algo se dibujó".

Se mide sobre la línea base (`origin`) y no sobre el bbox, que cambia según qué
letras tenga el texto.
"""

from django.test import SimpleTestCase

from service.bejerman_remito_pdf import (
    TEMPLATE_MGBIO,
    TEMPLATE_SEPID,
    _format_quantity,
    _wrap_descripcion,
    company_for,
    render_remito_pdf,
    template_for,
)


CHAR_WIDTH_EM = 0.6
TOLERANCIA = 0.5


def spans(pdf_bytes):
    """[(baseline_y_desde_arriba, x, size, texto)] del PDF, ordenados."""

    import fitz

    page = fitz.open(stream=pdf_bytes, filetype="pdf")[0]
    salida = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span["text"].strip():
                    salida.append(
                        (
                            round(span["origin"][1], 2),
                            round(span["origin"][0], 2),
                            round(span["size"], 2),
                            span["text"],
                        )
                    )
    salida.sort()
    return salida


def find(pdf_spans, texto):
    for y, x, size, valor in pdf_spans:
        if valor.strip() == texto.strip():
            return y, x, size, valor
    raise AssertionError(f"No se dibujó {texto!r}. Presentes: {[s[3] for s in pdf_spans]}")


def center_of(span):
    _y, x, size, texto = span
    return x + len(texto) * size * CHAR_WIDTH_EM / 2


COMPROBANTE = {
    "Comprobante_Tipo": "RT",
    "Comprobante_Letra": "R",
    "Comprobante_PtoVenta": "00004",
    "Comprobante_Numero": "00004982",
    "Comprobante_FechaEmision": "2026-08-06",
    "Comprobante_ValorDeclarado": "2766400",
    "Cliente_RazonSocial": "TERAPIAS MEDICAS DOMICILIARIAS S.A.",
    "Cliente_Direccion": "Correa 3039",
    "Cliente_CodigoPostal": "1429",
    "Cliente_Localidad": "Capital Federal",
    "Cliente_DescripSitIVA": "Responsable Inscripto",
    "Cliente_NroDocumento": "30678203757",
    "Comprobante_Items": [
        {"Item_NumeroRenglon": 1, "Item_Tipo": "L", "Item_DescripArticulo": "-" * 30},
        {"Item_NumeroRenglon": 2, "Item_Tipo": "L", "Item_DescripArticulo": "Orden OE-00273"},
        {
            "Item_NumeroRenglon": 3,
            "Item_CodigoArticulo": "1204001",
            "Item_CantidadUM1": "140",
            "Item_DescripArticulo": "Jarra para aspirador a batería BEMIS 800 c.c.",
            "Item_PM": "2046-10",
            "Item_Partida": "20260007",
            "Item_Vencimiento": "2029-03-01",
        },
        {
            "Item_NumeroRenglon": 4,
            "Item_CodigoArticulo": "1205013",
            "Item_CantidadUM1": "20",
            "Item_DescripArticulo": "Cámara p/calentador BMC GII H-60 Original",
            "Item_PM": "2046-12",
            "Item_Partida": "R22082822",
            "Item_Vencimiento": "2027-10-28",
        },
    ],
}


class RemitoPdfGeometriaTests(SimpleTestCase):
    """Posiciones medidas sobre el remito real SEPID 0004-00004982."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.spans = spans(render_remito_pdf(COMPROBANTE, company_key="SEPID"))

    def test_fecha_va_arriba_a_la_derecha_en_ddmmaa(self):
        y, x, size, _texto = find(self.spans, "06/08/26")
        self.assertAlmostEqual(y, TEMPLATE_SEPID.date_baseline, delta=TOLERANCIA)
        self.assertAlmostEqual(x, TEMPLATE_SEPID.date_x, delta=TOLERANCIA)
        self.assertAlmostEqual(size, TEMPLATE_SEPID.date_size, delta=TOLERANCIA)

    def test_cabecera_usa_las_cuatro_lineas_medidas(self):
        esperado = {
            "Señor/es:": 0,
            "Domicilio:": 1,
            "C.P./Localidad:": 2,
        }
        for texto, indice in esperado.items():
            y, x, _size, _valor = find(self.spans, texto)
            self.assertAlmostEqual(
                y, TEMPLATE_SEPID.header_first_baseline + indice * TEMPLATE_SEPID.header_pitch, delta=TOLERANCIA
            )
            self.assertAlmostEqual(x, TEMPLATE_SEPID.header_label_x, delta=TOLERANCIA)

        # La localidad va en su propia columna, no en la de los otros valores.
        _y, x, _size, _valor = find(self.spans, "1429 - Capital Federal")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.header_localidad_x, delta=TOLERANCIA)

        # PyMuPDF fusiona 'CUIT:' con su valor porque quedan pegados en la misma
        # línea base, así que se mide dónde arrancan los dígitos dentro del span.
        _y, x, size, valor = find(self.spans, "CUIT: 30678203757")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.header_cuit_label_x, delta=TOLERANCIA)
        inicio_digitos = x + valor.index("3") * size * CHAR_WIDTH_EM
        self.assertAlmostEqual(inicio_digitos, TEMPLATE_SEPID.header_cuit_value_x, delta=TOLERANCIA)

    def test_renglones_caen_en_la_grilla_de_17_puntos(self):
        for texto, renglon in (("-" * 30, 1), ("Orden OE-00273", 2), ("1204001", 3), ("1205013", 4)):
            y, _x, _size, _valor = find(self.spans, texto)
            self.assertAlmostEqual(
                y,
                TEMPLATE_SEPID.items_first_baseline + (renglon - 1) * TEMPLATE_SEPID.items_row_pitch,
                delta=TOLERANCIA,
                msg=f"renglón {renglon} ({texto!r})",
            )

    def test_codigo_y_descripcion_van_alineados_a_la_izquierda(self):
        _y, x, _size, _valor = find(self.spans, "1204001")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.col_codigo_x, delta=TOLERANCIA)
        _y, x, _size, _valor = find(self.spans, "Jarra para aspirador a batería BEMIS")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.col_descripcion_x, delta=TOLERANCIA)

    def test_pm_va_alineado_a_la_izquierda(self):
        # Comprobado contra los PDF reales: el PM arranca en la misma x aunque
        # cambie el cuerpo de la tipografía, así que no está centrado.
        _y, x, _size, _valor = find(self.spans, "2046-10")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.col_pm_x, delta=TOLERANCIA)

    def test_cantidad_serie_y_vencimiento_van_centrados(self):
        self.assertAlmostEqual(
            center_of(find(self.spans, "140,00")), TEMPLATE_SEPID.col_cantidad_center_x, delta=TOLERANCIA
        )
        for serie in ("20260007", "R22082822"):
            self.assertAlmostEqual(
                center_of(find(self.spans, serie)),
                TEMPLATE_SEPID.col_serie_center_x,
                delta=TOLERANCIA,
                msg=f"serie {serie} de {len(serie)} caracteres",
            )
        self.assertAlmostEqual(
            center_of(find(self.spans, "01/03/2029")), TEMPLATE_SEPID.col_vto_center_x, delta=TOLERANCIA
        )

    def test_vencimiento_se_imprime_en_ddmmaaaa(self):
        find(self.spans, "01/03/2029")
        find(self.spans, "28/10/2027")

    def test_no_dibuja_logo_ni_lineas_solo_texto(self):
        # El PDF es un overlay sobre papel preimpreso: si apareciera un dibujo,
        # taparía el formulario.
        import fitz

        page = fitz.open(stream=render_remito_pdf(COMPROBANTE, company_key="SEPID"), filetype="pdf")[0]
        self.assertEqual(page.get_drawings(), [])
        self.assertEqual(page.get_images(), [])


class RemitoPdfCantidadTests(SimpleTestCase):
    def test_la_cantidad_reserva_el_lugar_del_signo(self):
        self.assertEqual(_format_quantity(1), " 1,00")
        self.assertEqual(_format_quantity("140"), " 140,00")

    def test_la_cantidad_se_imprime_siempre_en_positivo(self):
        # El negativo con el que viajan RDA/RD/RDN es la dirección del movimiento de
        # stock, no una cantidad: en el papel del cliente se lee como un error y es
        # lo que reclamó cobranzas.
        self.assertEqual(_format_quantity(-1), " 1,00")
        self.assertEqual(_format_quantity(-2.5), " 2,50")

    def test_positivo_y_negativo_centran_en_el_mismo_punto(self):
        # Es la prueba de que la columna está centrada y no alineada de un lado: en
        # los remitos reales '-1,00' y ' 1,00' caen exactamente igual, así que el
        # renglón cae en su lugar aunque el signo ya no se imprima.
        def centro(cantidad):
            comprobante = dict(
                COMPROBANTE,
                Comprobante_Items=[
                    {
                        "Item_NumeroRenglon": 1,
                        "Item_CodigoArticulo": "1111009",
                        "Item_CantidadUM1": cantidad,
                        "Item_DescripArticulo": "X",
                    }
                ],
            )
            return center_of(find(spans(render_remito_pdf(comprobante, company_key="SEPID")), " 1,00"))

        self.assertAlmostEqual(centro(1), centro(-1), delta=0.01)
        self.assertAlmostEqual(centro(1), TEMPLATE_SEPID.col_cantidad_center_x, delta=TOLERANCIA)


class RemitoPdfDescripcionTests(SimpleTestCase):
    """Cortes verificados contra los remitos reales."""

    def test_corta_por_palabra(self):
        self.assertEqual(
            _wrap_descripcion("Jarra para aspirador a batería BEMIS 800 c.c.", 37),
            ["Jarra para aspirador a batería BEMIS ", "800 c.c."],
        )

    def test_corta_despues_de_un_guion(self):
        # Sin esta regla "KSOC-10" pasaría entero al renglón siguiente y el remito
        # dejaría de coincidir con el de Bejerman.
        self.assertEqual(
            _wrap_descripcion("Concentrador de oxígeno Konsung KSOC-10", 37),
            ["Concentrador de oxígeno Konsung KSOC-", "10"],
        )

    def test_prefiere_el_espacio_cuando_entra(self):
        self.assertEqual(
            _wrap_descripcion("Cámara p/calentador BMC GII H-60 Original", 37),
            ["Cámara p/calentador BMC GII H-60 ", "Original"],
        )

    def test_respeta_los_saltos_de_linea_explicitos(self):
        self.assertEqual(
            _wrap_descripcion("Cilindro de oxígeno ZS 2.8L\nc/válvula CGA870", 37),
            ["Cilindro de oxígeno ZS 2.8L", "c/válvula CGA870"],
        )

    def test_parte_una_palabra_mas_larga_que_la_columna(self):
        self.assertEqual(_wrap_descripcion("A" * 40, 37), ["A" * 37, "AAA"])

    def test_la_continuacion_baja_un_paso_de_media_linea(self):
        pdf_spans = spans(render_remito_pdf(COMPROBANTE, company_key="SEPID"))
        primera = find(pdf_spans, "Jarra para aspirador a batería BEMIS")
        segunda = find(pdf_spans, "800 c.c.")
        self.assertAlmostEqual(segunda[0] - primera[0], TEMPLATE_SEPID.items_wrap_pitch, delta=TOLERANCIA)
        self.assertAlmostEqual(segunda[1], TEMPLATE_SEPID.col_descripcion_x, delta=TOLERANCIA)


class RemitoPdfEmpresaTests(SimpleTestCase):
    """El formulario de MGBIO tiene el bloque de renglones 22.6 pt a la izquierda."""

    def test_el_punto_de_venta_identifica_la_empresa(self):
        self.assertEqual(company_for({"Comprobante_PtoVenta": "00004"}), "SEPID")
        self.assertEqual(company_for({"Comprobante_PtoVenta": "00007"}), "MGBIO")
        self.assertEqual(company_for({"Comprobante_PtoVenta": "00007"}, "SEPID"), "SEPID")
        self.assertEqual(template_for("MGBIO"), TEMPLATE_MGBIO)

    def test_los_renglones_de_mgbio_van_corridos_a_la_izquierda(self):
        pdf_spans = spans(render_remito_pdf(dict(COMPROBANTE, Comprobante_PtoVenta="00007")))
        _y, x, _size, _valor = find(pdf_spans, "1204001")
        self.assertAlmostEqual(x, TEMPLATE_MGBIO.col_codigo_x, delta=TOLERANCIA)
        _y, x, _size, _valor = find(pdf_spans, "Jarra para aspirador a batería BEMIS")
        self.assertAlmostEqual(x, TEMPLATE_MGBIO.col_descripcion_x, delta=TOLERANCIA)
        self.assertAlmostEqual(
            center_of(find(pdf_spans, "20260007")), TEMPLATE_MGBIO.col_serie_center_x, delta=TOLERANCIA
        )

    def test_la_cabecera_es_igual_en_las_dos_empresas(self):
        pdf_spans = spans(render_remito_pdf(dict(COMPROBANTE, Comprobante_PtoVenta="00007")))
        _y, x, _size, _valor = find(pdf_spans, "Señor/es:")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.header_label_x, delta=TOLERANCIA)
        _y, x, _size, _valor = find(pdf_spans, "TERAPIAS MEDICAS DOMICILIARIAS S.A.")
        self.assertAlmostEqual(x, TEMPLATE_SEPID.header_value_x, delta=TOLERANCIA)


class RemitoPdfRobustezTests(SimpleTestCase):
    def test_un_comprobante_minimo_no_explota(self):
        self.assertTrue(render_remito_pdf({"Comprobante_Items": []}).startswith(b"%PDF"))

    def test_rechaza_algo_que_no_sea_un_comprobante(self):
        with self.assertRaises(ValueError):
            render_remito_pdf("RIS 00004-00004342")

    def test_ignora_renglones_fuera_del_formulario(self):
        comprobante = dict(
            COMPROBANTE,
            Comprobante_Items=[
                {"Item_NumeroRenglon": 99, "Item_CodigoArticulo": "9999999", "Item_DescripArticulo": "fuera"},
            ],
        )
        textos = [s[3].strip() for s in spans(render_remito_pdf(comprobante, company_key="SEPID"))]
        self.assertNotIn("9999999", textos)

    def test_nunca_imprime_el_valor_declarado(self):
        # No va en el remito, aunque el comprobante traiga el importe y el PDF de
        # Crystal que se reproduce sí lo imprima.
        for company_key in ("SEPID", "MGBIO"):
            textos = [s[3].strip() for s in spans(render_remito_pdf(COMPROBANTE, company_key=company_key))]
            self.assertNotIn("VALOR DECLARADO:", textos, msg=company_key)
            self.assertNotIn("2.766.400,00", textos, msg=company_key)
