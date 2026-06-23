from django.test import SimpleTestCase

from service.serializers import IngresoListItemSerializer


class IngresoListItemSerializerTest(SimpleTestCase):
    def test_includes_resolucion(self):
        serializer = IngresoListItemSerializer(
            {
                "id": 1,
                "estado": "reparado",
                "presupuesto_estado": "aprobado",
                "resolucion": "reparado",
            }
        )

        self.assertEqual(serializer.data["resolucion"], "reparado")
