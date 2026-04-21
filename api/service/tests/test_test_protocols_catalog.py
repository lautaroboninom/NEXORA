import json

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service import test_protocols


class TestProtocolsCatalogAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            doc_type = "TEXT"
            datetime_type = "DATETIME"
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            doc_type = "JSONB"
            datetime_type = "TIMESTAMPTZ"
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            doc_type = "JSON"
            datetime_type = "DATETIME"
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
        overrides_sql = f"""
            CREATE TABLE IF NOT EXISTS user_permission_overrides (
                id {auto_inc},
                user_id INT NOT NULL,
                permission_code TEXT NOT NULL,
                effect TEXT NOT NULL,
                updated_by INT NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """
        if vendor == "postgresql":
            protocols_sql = f"""
                CREATE TABLE IF NOT EXISTS test_protocol_templates (
                    id {auto_inc},
                    type_key TEXT NOT NULL UNIQUE,
                    template_key TEXT NOT NULL UNIQUE,
                    active {bool_type} NOT NULL DEFAULT TRUE,
                    doc {doc_type} NOT NULL,
                    created_by INT NULL,
                    updated_by INT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
            """
        else:
            protocols_sql = f"""
                CREATE TABLE IF NOT EXISTS test_protocol_templates (
                    id {auto_inc},
                    type_key TEXT NOT NULL UNIQUE,
                    template_key TEXT NOT NULL UNIQUE,
                    active {bool_type} NOT NULL DEFAULT 1,
                    doc {doc_type} NOT NULL,
                    created_by INT NULL,
                    updated_by INT NULL,
                    created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
            """
        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(overrides_sql)
            cur.execute(protocols_sql)

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM test_protocol_templates")
            cur.execute("DELETE FROM user_permission_overrides")
        User.objects.all().delete()
        cls.admin_user = User.objects.create(
            nombre="Admin Test",
            email="admin-protocols@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.tech_user = User.objects.create(
            nombre="Tech Test",
            email="tech-protocols@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.admin_token = issue_token(cls.admin_user)
        cls.tech_token = issue_token(cls.tech_user)

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM test_protocol_templates")

    @staticmethod
    def _payload(type_key="aspirador", template_key="aspirador_editable_v1"):
        return {
            "type_key": type_key,
            "template_key": template_key,
            "template_version": "1.0.0",
            "display_name": "Aspirador editable",
            "default_instrumentos": "Instrumento demo",
            "active": True,
            "aliases": ["aspirador", "suctor"],
            "references": [
                {
                    "ref_id": "REF-01",
                    "tipo": "norma",
                    "titulo": "ISO Demo",
                    "edicion": "2026",
                    "anio": 2026,
                    "organismo_o_fabricante": "ISO",
                    "url": "",
                    "aplica_a": "Aspirador",
                }
            ],
            "sections": [
                {
                    "id": "seguridad",
                    "title": "Seguridad",
                    "entry_mode": "result_only",
                    "items": [
                        {
                            "key": "asp_visual",
                            "label": "Inspeccion visual",
                            "target": "Sin dano",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        }
                    ],
                }
            ],
            "overrides": [
                {
                    "name": "override_demo",
                    "active": True,
                    "priority": 10,
                    "match": {"marca_contains": "demo", "modelo_contains": ""},
                    "set_fields": {"display_name": "Aspirador demo"},
                    "references": [],
                    "append_ref_to_all_items": "",
                    "item_ref_ids": {},
                }
            ],
        }

    def _client(self, token: str) -> APIClient:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return c

    def test_crud_catalog_requires_manage_permission(self):
        tech = self._client(self.tech_token)
        resp = tech.get("/api/catalogos/tests/protocolos/")
        self.assertEqual(resp.status_code, 403)

        admin = self._client(self.admin_token)
        create_resp = admin.post("/api/catalogos/tests/protocolos/", self._payload(), format="json")
        self.assertEqual(create_resp.status_code, 201)
        pid = create_resp.data.get("id")
        self.assertTrue(pid)

        list_resp = admin.get("/api/catalogos/tests/protocolos/")
        self.assertEqual(list_resp.status_code, 200)
        self.assertTrue(any(int(item.get("id")) == int(pid) for item in (list_resp.data or [])))

        patch_resp = admin.patch(
            f"/api/catalogos/tests/protocolos/{pid}/",
            {"display_name": "Aspirador editable v2"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(patch_resp.data.get("display_name"), "Aspirador editable v2")

        del_resp = admin.delete(f"/api/catalogos/tests/protocolos/{pid}/")
        self.assertEqual(del_resp.status_code, 200)
        detail_resp = admin.get(f"/api/catalogos/tests/protocolos/{pid}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertFalse(detail_resp.data.get("active"))

    def test_create_rejects_invalid_ref_ids(self):
        bad = self._payload(type_key="bpap", template_key="bpap_editable_v1")
        bad["sections"][0]["items"][0]["ref_ids"] = ["REF-99"]
        admin = self._client(self.admin_token)
        resp = admin.post("/api/catalogos/tests/protocolos/", bad, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ref_id", str(resp.data.get("detail") or "").lower())

    def test_runtime_resolution_uses_db_and_updates_immediately(self):
        admin = self._client(self.admin_token)
        payload = self._payload(type_key="respirador", template_key="respirador_editable_v1")
        payload["display_name"] = "Respirador editable"
        payload["aliases"] = ["respirador", "ventilador"]
        payload["sections"][0]["items"][0]["key"] = "resp_visual"
        payload["sections"][0]["items"][0]["label"] = "Visual runtime"

        create_resp = admin.post("/api/catalogos/tests/protocolos/", payload, format="json")
        self.assertEqual(create_resp.status_code, 201)
        pid = create_resp.data.get("id")

        resolved = test_protocols.resolve_protocol_for_equipo("ventilador")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("template_key"), "respirador_editable_v1")
        first_label = (resolved.get("sections") or [{}])[0].get("items", [{}])[0].get("label")
        self.assertEqual(first_label, "Visual runtime")

        patch_resp = admin.patch(
            f"/api/catalogos/tests/protocolos/{pid}/",
            {
                "sections": [
                    {
                        "id": "seguridad",
                        "title": "Seguridad",
                        "entry_mode": "result_only",
                        "items": [
                            {
                                "key": "resp_visual",
                                "label": "Visual runtime actualizado",
                                "target": "OK",
                                "unit": "",
                                "ref_ids": ["REF-01"],
                            }
                        ],
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(patch_resp.status_code, 200)

        resolved_after = test_protocols.resolve_protocol_for_equipo("ventilador")
        self.assertIsNotNone(resolved_after)
        after_label = (resolved_after.get("sections") or [{}])[0].get("items", [{}])[0].get("label")
        self.assertEqual(after_label, "Visual runtime actualizado")

