"""Un job que dice `sal_done` pero cuyo equipo volvió al origen no puede saltear la SAL.

Cuando el ENT al destino falla, el botón de recuperación devuelve la unidad al
depósito origen con un ENT de reposición, pero el `request_payload` del job
sigue marcado `sal_done`. Si el reintento cree en esa marca, emite sólo el ENT y
el stock queda DUPLICADO: 1 en el origen y 1 en el destino. Pasó de verdad el
27/08/2026 con 18 equipos, al destrabar los jobs que el bug de `stk_FecMod`
tenía frenados desde julio.
"""

from django.test import SimpleTestCase

from service.bejerman_sync import _records_for_equipment_article

KSOC5 = "1115001"
OTRO = "1104008"
PARTIDA = "MA23031000042"


def _stock(article_code: str, *, deposit: str = "STR", cantidad: float = 1) -> dict:
    return {
        "Art_CodGen": article_code,
        "Art_Partida": PARTIDA,
        "Art_CodDeposito": deposit,
        "Art_RealUM1": cantidad,
    }


class DeteccionDeReposicionTests(SimpleTestCase):
    """Cubre la condición exacta que dispara la reemisión de la SAL."""

    def test_el_origen_todavia_tiene_el_articulo(self):
        # El repos devolvió la unidad: hay que volver a emitir la SAL.
        sal_records = [_stock(KSOC5)]
        self.assertTrue(_records_for_equipment_article(sal_records, KSOC5))

    def test_el_origen_quedo_vacio(self):
        # La SAL de verdad salió: no hay nada que reemitir.
        self.assertFalse(_records_for_equipment_article([], KSOC5))

    def test_el_origen_tiene_otro_articulo(self):
        # Lo que quedó en el origen es el gemelo de la partida, no este equipo.
        sal_records = [_stock(OTRO)]
        self.assertFalse(_records_for_equipment_article(sal_records, KSOC5))

    def test_sin_articulo_resuelto_no_se_puede_decidir(self):
        # Sin artículo el filtro devuelve todo, que es el comportamiento previo.
        sal_records = [_stock(KSOC5), _stock(OTRO)]
        self.assertEqual(len(_records_for_equipment_article(sal_records, "")), 2)
