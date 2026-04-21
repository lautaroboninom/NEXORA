import copy
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service import test_protocols
from service.auth import issue_token
from service.models import User


class IngresoTestsAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"

        bool_type = "BOOLEAN" if vendor != "sqlite" else "INTEGER"
        datetime_type = "TIMESTAMPTZ" if vendor == "postgresql" else "DATETIME"
        engine_suffix = " ENGINE=InnoDB" if vendor == "mysql" else ""

        users_sql = f"""
            CREATE TABLE IF NOT EXISTS users (
                id {auto_inc},
                nombre TEXT,
                email VARCHAR(320) UNIQUE,
                hash_pw TEXT,
                rol TEXT,
                activo {bool_type} DEFAULT 1
            ){engine_suffix}
        """

        customers_sql = f"""
            CREATE TABLE IF NOT EXISTS customers (
                id {auto_inc},
                razon_social TEXT
            ){engine_suffix}
        """

        marcas_sql = f"""
            CREATE TABLE IF NOT EXISTS marcas (
                id {auto_inc},
                nombre TEXT
            ){engine_suffix}
        """

        models_sql = f"""
            CREATE TABLE IF NOT EXISTS models (
                id {auto_inc},
                marca_id INT,
                nombre TEXT,
                tipo_equipo TEXT
            ){engine_suffix}
        """

        devices_sql = f"""
            CREATE TABLE IF NOT EXISTS devices (
                id {auto_inc},
                customer_id INT,
                marca_id INT,
                model_id INT,
                numero_serie TEXT,
                numero_interno TEXT,
                tipo_equipo TEXT
            ){engine_suffix}
        """

        ingresos_sql = f"""
            CREATE TABLE IF NOT EXISTS ingresos (
                id {auto_inc},
                device_id INT NOT NULL,
                estado TEXT,
                asignado_a INT
            ){engine_suffix}
        """

        if vendor == "sqlite":
            ingreso_tests_sql = """
                CREATE TABLE IF NOT EXISTS ingreso_tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingreso_id INT NOT NULL UNIQUE,
                    template_key TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    tipo_equipo_snapshot TEXT,
                    payload TEXT NOT NULL,
                    schema_snapshot TEXT NOT NULL DEFAULT '{}',
                    references_snapshot TEXT NOT NULL,
                    resultado_global TEXT NOT NULL DEFAULT 'pendiente',
                    conclusion TEXT,
                    instrumentos TEXT,
                    firmado_por TEXT,
                    fecha_ejecucion DATETIME NULL,
                    tecnico_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
        elif vendor == "postgresql":
            ingreso_tests_sql = f"""
                CREATE TABLE IF NOT EXISTS ingreso_tests (
                    id {auto_inc},
                    ingreso_id INT NOT NULL UNIQUE,
                    template_key TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    tipo_equipo_snapshot TEXT,
                    payload JSONB NOT NULL,
                    schema_snapshot JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    references_snapshot JSONB NOT NULL,
                    resultado_global TEXT NOT NULL DEFAULT 'pendiente',
                    conclusion TEXT,
                    instrumentos TEXT,
                    firmado_por TEXT,
                    fecha_ejecucion {datetime_type} NULL,
                    tecnico_id INT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
            """
        else:
            ingreso_tests_sql = f"""
                CREATE TABLE IF NOT EXISTS ingreso_tests (
                    id {auto_inc},
                    ingreso_id INT NOT NULL UNIQUE,
                    template_key TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    tipo_equipo_snapshot TEXT,
                    payload JSON NOT NULL,
                    schema_snapshot JSON NOT NULL,
                    references_snapshot JSON NOT NULL,
                    resultado_global TEXT NOT NULL DEFAULT 'pendiente',
                    conclusion TEXT,
                    instrumentos TEXT,
                    firmado_por TEXT,
                    fecha_ejecucion {datetime_type} NULL,
                    tecnico_id INT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
            """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(customers_sql)
            cur.execute(marcas_sql)
            cur.execute(models_sql)
            cur.execute(devices_sql)
            cur.execute(ingresos_sql)
            cur.execute(ingreso_tests_sql)

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        elif connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return cur.fetchone()[0]

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_tests")
            cur.execute("DELETE FROM ingresos")
            cur.execute("DELETE FROM devices")
            cur.execute("DELETE FROM models")
            cur.execute("DELETE FROM marcas")
            cur.execute("DELETE FROM customers")
        User.objects.all().delete()

        cls.tech_user = User.objects.create(
            nombre="Tech Test",
            email="tech-test@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.tech_token = issue_token(cls.tech_user)

        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers (razon_social) VALUES (%s)", ["Clinica Demo"])
            customer_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["ResMed"])
            marca_resmed = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [marca_resmed, "AirSense 10", "CPAP/AutoCPAP"],
            )
            model_cpap = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_resmed, model_cpap, "CPAP-001", "MG 0001", "CPAP/AutoCPAP"],
            )
            device_cpap = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, asignado_a) VALUES (%s,%s,%s)",
                [device_cpap, "diagnosticado", cls.tech_user.id],
            )
            cls.ingreso_cpap_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["EI"])
            marca_ei = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [marca_ei, "A-550", "Aspirador"],
            )
            model_asp = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_ei, model_asp, "ASP-001", "MG 0002", "Aspirador"],
            )
            device_asp = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, asignado_a) VALUES (%s,%s,%s)",
                [device_asp, "diagnosticado", cls.tech_user.id],
            )
            cls.ingreso_asp_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["Fisher & Paykel"])
            marca_af = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [marca_af, "Airvo 2", "ALTO FLUJO"],
            )
            model_af = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_af, model_af, "AF-001", "MG 0003", "ALTO FLUJO"],
            )
            device_af = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, asignado_a) VALUES (%s,%s,%s)",
                [device_af, "diagnosticado", cls.tech_user.id],
            )
            cls.ingreso_af_id = cls._last_insert_id(cur)

            cur.execute("INSERT INTO marcas (nombre) VALUES (%s)", ["Covidien"])
            marca_pb = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO models (marca_id, nombre, tipo_equipo) VALUES (%s,%s,%s)",
                [marca_pb, "PB 560", "Respirador"],
            )
            model_pb = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno, tipo_equipo)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                [customer_id, marca_pb, model_pb, "PB560-001", "MG 0004", "Respirador"],
            )
            device_pb = cls._last_insert_id(cur)
            cur.execute(
                "INSERT INTO ingresos (device_id, estado, asignado_a) VALUES (%s,%s,%s)",
                [device_pb, "diagnosticado", cls.tech_user.id],
            )
            cls.ingreso_pb560_id = cls._last_insert_id(cur)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tech_token}")
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ingreso_tests")

    def _url(self, ingreso_id: int) -> str:
        return f"/api/ingresos/{ingreso_id}/test/"

    def _url_pdf(self, ingreso_id: int) -> str:
        return f"/api/ingresos/{ingreso_id}/test/pdf/"

    def test_all_templates_define_at_least_one_reference(self):
        self.assertEqual(len(test_protocols.BASE_TEMPLATES.keys()), 8)
        self.assertIn("alto_flujo", test_protocols.BASE_TEMPLATES)
        for key, tpl in test_protocols.BASE_TEMPLATES.items():
            refs = tpl.get("references") or []
            self.assertGreaterEqual(len(refs), 1, f"Template {key} must define references")
            self.assertTrue(all((r.get("tipo") == "norma") for r in refs), f"Template {key} must use norma only")

    def test_get_returns_references_and_ref_ids(self):
        resp = self.client.get(self._url(self.ingreso_cpap_id))
        self.assertEqual(resp.status_code, 200)
        refs = resp.data.get("schema", {}).get("references") or []
        self.assertGreaterEqual(len(refs), 1)
        self.assertTrue(any((r.get("ref_id") or "").startswith("REF-") for r in refs))

        sections = resp.data.get("schema", {}).get("sections") or []
        self.assertTrue(sections and sections[0].get("items"))
        first_item = sections[0]["items"][0]
        self.assertTrue((first_item.get("ref_ids") or []))

    def test_aspirador_get_has_default_instrumentos_and_battery_item(self):
        resp = self.client.get(self._url(self.ingreso_asp_id))
        self.assertEqual(resp.status_code, 200)
        instrumentos = str(resp.data.get("instrumentos") or "")
        self.assertIn("Vacuómetro de referencia con certificado", instrumentos)
        self.assertIn("Flujómetro de referencia", instrumentos)

        sections = resp.data.get("schema", {}).get("sections") or []
        keys = [it.get("key") for sec in sections for it in (sec.get("items") or [])]
        self.assertIn("asp_duracion_bateria", keys)
        self.assertNotIn("asp_alarma_obstruccion", keys)

        seguridad = next((sec for sec in sections if sec.get("id") == "seguridad"), None)
        self.assertIsNotNone(seguridad)
        self.assertEqual(seguridad.get("entry_mode"), "result_only")

        performance = next((sec for sec in sections if sec.get("id") == "performance"), None)
        self.assertIsNotNone(performance)
        self.assertEqual(performance.get("entry_mode"), "measured_only")

        vacio = next(
            (it for sec in sections for it in (sec.get("items") or []) if it.get("key") == "asp_vacio_max"),
            None,
        )
        self.assertIsNotNone(vacio)
        self.assertEqual(vacio.get("target"), ">= 500 mmHg")

        caudal = next(
            (it for sec in sections for it in (sec.get("items") or []) if it.get("key") == "asp_caudal_libre"),
            None,
        )
        self.assertIsNotNone(caudal)
        self.assertEqual(caudal.get("target"), ">= 15 L/min")

        battery = next(
            (it for sec in sections for it in (sec.get("items") or []) if it.get("key") == "asp_duracion_bateria"),
            None,
        )
        self.assertIsNotNone(battery)
        self.assertEqual(battery.get("target"), "Tensión de batería > 11 V luego de 15 min de prueba continua")
        self.assertEqual(battery.get("unit"), "V")

    def test_get_returns_alto_flujo_protocol(self):
        resp = self.client.get(self._url(self.ingreso_af_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("template_key"), "alto_flujo_v1")

        refs = resp.data.get("schema", {}).get("references") or []
        self.assertGreaterEqual(len(refs), 1)
        self.assertTrue(all((r.get("tipo") == "norma") for r in refs))

        sections = resp.data.get("schema", {}).get("sections") or []
        keys = [it.get("key") for sec in sections for it in (sec.get("items") or [])]
        self.assertIn("af_flujo_setpoint", keys)
        self.assertIn("af_fio2_setpoint", keys)

    def test_get_respirador_pb560_returns_ch6_override_protocol(self):
        resp = self.client.get(self._url(self.ingreso_pb560_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("template_key"), "respirador_pb560_v1")

        applied = resp.data.get("applied_overrides") or []
        self.assertIn("covidien_pb560_ch6_performance_verification", applied)

        refs = resp.data.get("schema", {}).get("references") or []
        ref_ids = [r.get("ref_id") for r in refs]
        self.assertIn("REF-PB560-CH6", ref_ids)

        sections = resp.data.get("schema", {}).get("sections") or []
        sec_ids = [s.get("id") for s in sections]
        self.assertIn("pb560_mediciones", sec_ids)
        self.assertIn("pb560_calibraciones", sec_ids)
        self.assertIn("pb560_accuracy", sec_ids)
        self.assertIn("pb560_alarmas_interfaces", sec_ids)

        mediciones_section = next((s for s in sections if s.get("id") == "pb560_mediciones"), None)
        self.assertIsNotNone(mediciones_section)
        self.assertEqual(mediciones_section.get("entry_mode"), "measured_only")

        alarmas_section = next((s for s in sections if s.get("id") == "pb560_alarmas_interfaces"), None)
        self.assertIsNotNone(alarmas_section)
        self.assertEqual(alarmas_section.get("entry_mode"), "result_only")

        keys = [it.get("key") for sec in sections for it in (sec.get("items") or [])]
        self.assertIn("pb560_24v_check", keys)
        self.assertIn("pb560_adult_press", keys)
        self.assertNotIn("pb560_o2_ambient", keys)
        self.assertNotIn("pb560_o2_enriched", keys)
        self.assertNotIn("pb560_dc_led_on", keys)
        self.assertNotIn("pb560_battery_led_on", keys)
        self.assertNotIn("pb560_dc_reentry", keys)
        self.assertNotIn("pb560_ped_vol", keys)
        self.assertNotIn("pb560_ped_vol_pts", keys)
        self.assertNotIn("pb560_ped_press", keys)
        self.assertNotIn("pb560_ped_press_pts", keys)
        self.assertNotIn("pb560_fio2_detection", keys)
        self.assertNotIn("pb560_remote_alarm", keys)
        self.assertNotIn("pb560_usb_ports", keys)
        self.assertNotIn("pb560_clear_faults", keys)

    def test_patch_persists_references_snapshot_norma_only(self):
        payload = {
            "values": {
                "cpap_presion_setpoint": {"valor_a_medir": "10 cmH2O", "measured": "10.0", "result": "ok"},
                "cpap_rampa": {"valor_a_medir": "Rampa 20 min", "measured": "OK", "result": "ok"},
            },
            "resultado_global": "pendiente",
            "conclusion": "Sin desvio relevante",
            "instrumentos": "Analizador flujo/presion",
            "firmado_por": "Tech Test",
        }
        resp = self.client.patch(self._url(self.ingreso_cpap_id), payload, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.get("ok"))

        with connection.cursor() as cur:
            cur.execute("SELECT references_snapshot FROM ingreso_tests WHERE ingreso_id=%s", [self.ingreso_cpap_id])
            raw_refs = cur.fetchone()[0]
        refs = raw_refs if isinstance(raw_refs, list) else json.loads(raw_refs)
        self.assertGreaterEqual(len(refs), 1)
        self.assertTrue(all((r.get("tipo") == "norma") for r in refs))

        get_resp = self.client.get(self._url(self.ingreso_cpap_id))
        self.assertEqual(get_resp.status_code, 200)
        got_val = (
            get_resp.data.get("values", {})
            .get("cpap_presion_setpoint", {})
            .get("valor_a_medir")
        )
        self.assertEqual(got_val, "10 cmH2O")

    def test_patch_rejects_apto_without_references(self):
        original_refs = copy.deepcopy(test_protocols.BASE_TEMPLATES["aspirador"]["references"])
        test_protocols.BASE_TEMPLATES["aspirador"]["references"] = []
        try:
            resp = self.client.patch(
                self._url(self.ingreso_asp_id),
                {"resultado_global": "apto"},
                format="json",
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Apto", str(resp.data.get("detail") or ""))
        finally:
            test_protocols.BASE_TEMPLATES["aspirador"]["references"] = original_refs

    def test_schema_snapshot_freezes_sections_after_first_save(self):
        first_get = self.client.get(self._url(self.ingreso_cpap_id))
        self.assertEqual(first_get.status_code, 200)
        first_sections = first_get.data.get("schema", {}).get("sections") or []
        self.assertTrue(first_sections and first_sections[0].get("items"))
        original_label = first_sections[0]["items"][0].get("label")
        self.assertTrue(original_label)

        save_resp = self.client.patch(
            self._url(self.ingreso_cpap_id),
            {
                "values": {
                    "cpap_presion_setpoint": {"measured": "10", "result": "ok", "observaciones": ""}
                },
                "resultado_global": "pendiente",
            },
            format="json",
        )
        self.assertEqual(save_resp.status_code, 200)

        original_sections = copy.deepcopy(test_protocols.BASE_TEMPLATES["cpap_autocpap"]["sections"])
        try:
            test_protocols.BASE_TEMPLATES["cpap_autocpap"]["sections"][0]["items"][0]["label"] = "CAMBIO GLOBAL"
            after_get = self.client.get(self._url(self.ingreso_cpap_id))
            self.assertEqual(after_get.status_code, 200)
            after_sections = after_get.data.get("schema", {}).get("sections") or []
            self.assertTrue(after_sections and after_sections[0].get("items"))
            self.assertEqual(after_sections[0]["items"][0].get("label"), original_label)
        finally:
            test_protocols.BASE_TEMPLATES["cpap_autocpap"]["sections"] = original_sections

    def test_pdf_uses_references_snapshot(self):
        # First persist a regular row.
        save_resp = self.client.patch(
            self._url(self.ingreso_cpap_id),
            {
                "values": {"cpap_presion_setpoint": {"measured": "10", "result": "ok", "observaciones": ""}},
                "resultado_global": "pendiente",
            },
            format="json",
        )
        self.assertEqual(save_resp.status_code, 200)

        sentinel_refs = [
            {
                "ref_id": "REF-ZZ",
                "tipo": "norma",
                "titulo": "Norma Sentinel",
                "edicion": "v1",
                "anio": 2026,
                "organismo_o_fabricante": "QA",
                "url": "https://example.invalid/sentinel",
                "aplica_a": "sentinel",
            }
        ]
        with connection.cursor() as cur:
            raw = json.dumps(sentinel_refs, ensure_ascii=False)
            if connection.vendor == "postgresql":
                cur.execute("UPDATE ingreso_tests SET references_snapshot=%s::jsonb WHERE ingreso_id=%s", [raw, self.ingreso_cpap_id])
            else:
                cur.execute("UPDATE ingreso_tests SET references_snapshot=%s WHERE ingreso_id=%s", [raw, self.ingreso_cpap_id])

        with patch("service.views.ingreso_tests_views.render_ingreso_test_pdf") as mock_render:
            mock_render.return_value = (b"%PDF-1.4 sentinel", "test.pdf")
            pdf_resp = self.client.get(self._url_pdf(self.ingreso_cpap_id))
            self.assertEqual(pdf_resp.status_code, 200)
            self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
            self.assertTrue(mock_render.called)
            report_arg = mock_render.call_args[0][0]
            self.assertEqual(report_arg.get("references")[0].get("ref_id"), "REF-ZZ")
