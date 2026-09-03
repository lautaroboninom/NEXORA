from django.test import SimpleTestCase

from service.serializers import IngresoListItemSerializer
from service.views.ingresos_views import _ingreso_customer_label, _owner_list_select_sql


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

    def test_includes_propietario_nombre(self):
        serializer = IngresoListItemSerializer(
            {
                "id": 1,
                "estado": "reparado",
                "presupuesto_estado": "aprobado",
                "razon_social": "Particular",
                "propietario_nombre": "Juan Perez",
            }
        )

        self.assertEqual(serializer.data["propietario_nombre"], "Juan Perez")

    def test_propietario_nombre_es_opcional(self):
        """Los listados que todavía no lo traen viajan con el campo en null."""
        serializer = IngresoListItemSerializer({"id": 1, "estado": "reparado"})

        self.assertIsNone(serializer.data["propietario_nombre"])

    def test_includes_active_derivation_contract(self):
        serializer = IngresoListItemSerializer(
            {
                "id": 1,
                "estado": "diagnosticado",
                "derivacion_activa": True,
                "derivacion_id": 8,
                "derivacion_estado": "en_servicio",
                "derivacion_proveedor_id": 3,
                "derivacion_proveedor": "Laboratorio externo",
                "derivacion_fecha": "2026-09-01",
            }
        )

        self.assertTrue(serializer.data["derivacion_activa"])
        self.assertEqual(serializer.data["derivacion_estado"], "en_servicio")
        self.assertEqual(serializer.data["derivacion_proveedor"], "Laboratorio externo")


class IngresoCustomerLabelTest(SimpleTestCase):
    """'Particular' y 'MG BIO' son contenedores: manda el dueño del equipo."""

    def test_particular_muestra_el_dueno(self):
        row = {"razon_social": "Particular", "propietario_nombre": "Juan Perez"}
        self.assertEqual(_ingreso_customer_label(row), "Juan Perez")

    def test_mg_bio_muestra_el_dueno(self):
        row = {"razon_social": "MG BIO S.R.L.", "propietario_nombre": "Ana Gomez"}
        self.assertEqual(_ingreso_customer_label(row), "Ana Gomez")

    def test_cliente_real_manda_sobre_el_dueno(self):
        row = {"razon_social": "SEDESA S.A.", "propietario_nombre": "Juan Perez"}
        self.assertEqual(_ingreso_customer_label(row), "SEDESA S.A.")

    def test_sin_dueno_queda_el_contenedor(self):
        self.assertEqual(_ingreso_customer_label({"razon_social": "Particular"}), "Particular")

    def test_lee_el_alias_cliente_de_los_exports(self):
        row = {"cliente": "Particular", "propietario_nombre": "Juan Perez"}
        self.assertEqual(_ingreso_customer_label(row), "Juan Perez")


class OwnerListSelectSqlTest(SimpleTestCase):
    def test_devuelve_una_sola_columna_con_coma(self):
        sql = _owner_list_select_sql()

        self.assertTrue(sql.strip().endswith("AS propietario_nombre,"))
        self.assertEqual(sql.count("AS propietario_nombre"), 1)
