"""Candados de la pestaña que espera el PDF y lo manda a imprimir.

La página se arma como un string en Python, así que nada la valida en tiempo de
importación: ya se perdió una vez la línea que renderiza el PDF (02e168c). Estos
tests fijan lo que el operador necesita: que el archivo se muestre y que el diálogo
de impresión salga solo, sin apretar ningún botón.
"""

import json
import re

from django.test import SimpleTestCase

from service.views.pdf_wait_pages import render_bejerman_pdf_wait_page


class PdfWaitPageTests(SimpleTestCase):
    def setUp(self):
        self.html = render_bejerman_pdf_wait_page(
            pdf_url="/api/ordenes-entrega/remito-bejerman/abc/pdf/",
            document_name="remito",
        )

    def test_muestra_el_pdf_en_un_iframe(self):
        # Navegar la pestaña revoca el blob que la creó (ERR_FILE_NOT_FOUND), por eso
        # el PDF va sí o sí en un iframe.
        self.assertIn("createElement('iframe')", self.html)
        self.assertIn("visorEl.appendChild(visor)", self.html)

    def test_imprime_solo_cuando_el_pdf_termino_de_cargar(self):
        self.assertIn("visor.addEventListener('load'", self.html)
        self.assertIn("printPdf", self.html)
        self.assertIn("contentWindow.print()", self.html)

    def test_deja_un_boton_por_si_el_navegador_bloquea_la_impresion(self):
        self.assertIn('id="print"', self.html)
        self.assertIn("printEl.addEventListener('click', printPdf)", self.html)

    def test_la_url_del_pdf_viaja_escapada(self):
        html = render_bejerman_pdf_wait_page(pdf_url="/api/x/pdf/?a=1&b=</script>")
        self.assertNotIn("&b=</script>", html)

    def test_la_configuracion_viaja_como_json_valido(self):
        # Escapar `</` no puede romper el JSON: si se rompe, la pestaña queda
        # en blanco y el operador no ve ni el error.
        html = render_bejerman_pdf_wait_page(
            pdf_url="/api/x/pdf/?a=1&b=</script>",
            document_name="remito RSS",
        )
        bloque = re.search(
            r'<script id="nexora-espera" type="application/json">(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(bloque)
        config = json.loads(bloque.group(1))
        self.assertEqual(config["pdfUrl"], "/api/x/pdf/?a=1&b=</script>")
        self.assertEqual(config["documentName"], "remito RSS")

    def test_cuenta_en_que_etapa_esta_la_espera(self):
        # Lo que devuelve el backend en el 202 (`stage_label`) es la diferencia
        # entre "algo está pasando" y un spinner que parece colgado.
        self.assertIn("payload.stage_label", self.html)
        self.assertIn('data-paso="pdf"', self.html)
        self.assertIn('data-paso="print"', self.html)
        self.assertIn("marcarPaso('print'", self.html)

    def test_muestra_cuanto_hace_que_espera(self):
        self.assertIn('id="reloj"', self.html)
        self.assertIn("pintarReloj", self.html)

    def test_cada_elemento_que_el_script_busca_existe_en_el_marcado(self):
        # Un id mal escrito revienta el script entero y la pestaña queda muda:
        # ni muestra el PDF ni el error.
        marcado = re.sub(r"<script.*?</script>", "", self.html, flags=re.S)
        # El bloque de configuración y el iframe del visor no están en el marcado:
        # uno es un `<script>` y el otro lo crea el propio script.
        creados = set(re.findall(r"\.id = '([^']+)'", self.html)) | {"nexora-espera"}
        for elemento_id in re.findall(r"getElementById\('([^']+)'\)", self.html):
            if elemento_id in creados:
                continue
            self.assertIn(f'id="{elemento_id}"', marcado, elemento_id)
