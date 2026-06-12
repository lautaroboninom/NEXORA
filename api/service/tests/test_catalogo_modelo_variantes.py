from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class ModeloVariantesAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        vendor = connection.vendor
        auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT" if vendor == "sqlite" else "BIGSERIAL PRIMARY KEY"
        bool_type = "INTEGER" if vendor == "sqlite" else "BOOLEAN"
        bool_default = "1" if vendor == "sqlite" else "TRUE"
        with connection.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id {auto_inc},
                    nombre TEXT,
                    email VARCHAR(320) UNIQUE,
                    hash_pw TEXT,
                    rol TEXT,
                    activo {bool_type} DEFAULT {bool_default}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS user_permission_overrides (
                    id {auto_inc},
                    user_id INTEGER NOT NULL,
                    permission_code TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    updated_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT NOT NULL
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    marca_id INTEGER,
                    nombre TEXT NOT NULL,
                    tipo_equipo TEXT,
                    variante TEXT
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marca_tipos_equipo (
                    id {auto_inc},
                    marca_id INTEGER NOT NULL,
                    nombre TEXT NOT NULL,
                    activo {bool_type} NOT NULL DEFAULT {bool_default}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marca_series (
                    id {auto_inc},
                    marca_id INTEGER NOT NULL,
                    tipo_id INTEGER NOT NULL,
                    nombre TEXT NOT NULL,
                    alias TEXT NULL,
                    activo {bool_type} NOT NULL DEFAULT {bool_default}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marca_series_variantes (
                    id {auto_inc},
                    marca_id INTEGER NOT NULL,
                    tipo_id INTEGER NOT NULL,
                    serie_id INTEGER NOT NULL,
                    nombre TEXT NOT NULL,
                    activo {bool_type} NOT NULL DEFAULT {bool_default}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS model_hierarchy (
                    id {auto_inc},
                    model_id INTEGER NOT NULL,
                    marca_id INTEGER NOT NULL,
                    tipo_id INTEGER NOT NULL,
                    serie_id INTEGER NOT NULL,
                    variante_id INTEGER NULL,
                    full_name TEXT NOT NULL
                )
                """
            )

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for table in (
                "model_hierarchy",
                "marca_series_variantes",
                "marca_series",
                "marca_tipos_equipo",
                "models",
                "marcas",
                "user_permission_overrides",
            ):
                cur.execute(f"DELETE FROM {table}")
        User.objects.all().delete()
        self.user = User.objects.create(
            nombre="Admin Catalogo",
            email="admin-modelo-variantes@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @staticmethod
    def _last_id(cur):
        if connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        elif connection.vendor == "mysql":
            cur.execute("SELECT LAST_INSERT_ID()")
        else:
            cur.execute("SELECT last_insert_rowid()")
        return int(cur.fetchone()[0])

    def _insert(self, sql, params):
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return self._last_id(cur)

    def _seed_model(self, marca_id, nombre, tipo="", variante=""):
        return self._insert(
            "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s)",
            [marca_id, nombre, tipo, variante],
        )

    def _names_for_model(self, model_id):
        resp = self.client.get(f"/api/catalogos/modelos/{model_id}/variantes/")
        self.assertEqual(resp.status_code, 200)
        return [row.get("name") for row in resp.json()]

    def test_modelo_variantes_uses_model_hierarchy_not_brand(self):
        marca_id = self._insert("INSERT INTO marcas(nombre) VALUES (%s)", ["BMC"])
        alto_tipo = self._insert(
            "INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,%s)",
            [marca_id, "ALTO FLUJO", True],
        )
        cpap_tipo = self._insert(
            "INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,%s)",
            [marca_id, "CPAP", True],
        )
        alto_serie = self._insert(
            "INSERT INTO marca_series(marca_id, tipo_id, nombre, activo) VALUES (%s,%s,%s,%s)",
            [marca_id, alto_tipo, "ALTO FLUJO", True],
        )
        cpap_serie = self._insert(
            "INSERT INTO marca_series(marca_id, tipo_id, nombre, activo) VALUES (%s,%s,%s,%s)",
            [marca_id, cpap_tipo, "CPAP G2S", True],
        )
        alto_model = self._seed_model(marca_id, "ALTO FLUJO", "ALTO FLUJO")
        cpap_model = self._seed_model(marca_id, "CPAP G2S", "CPAP")

        for name in ("B-20V", "B-25ST"):
            self._insert(
                "INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo) VALUES (%s,%s,%s,%s,%s)",
                [marca_id, alto_tipo, alto_serie, name, True],
            )
        self._insert(
            "INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo) VALUES (%s,%s,%s,%s,%s)",
            [marca_id, cpap_tipo, cpap_serie, "G2S-AUTO", True],
        )
        self._insert(
            "INSERT INTO model_hierarchy(model_id, marca_id, tipo_id, serie_id, full_name) VALUES (%s,%s,%s,%s,%s)",
            [alto_model, marca_id, alto_tipo, alto_serie, "ALTO FLUJO"],
        )
        self._insert(
            "INSERT INTO model_hierarchy(model_id, marca_id, tipo_id, serie_id, full_name) VALUES (%s,%s,%s,%s,%s)",
            [cpap_model, marca_id, cpap_tipo, cpap_serie, "CPAP G2S"],
        )

        self.assertEqual(self._names_for_model(alto_model), ["B-20V", "B-25ST"])
        self.assertEqual(self._names_for_model(cpap_model), ["G2S-AUTO"])

    def test_modelo_variantes_resolves_exact_series_without_hierarchy(self):
        marca_id = self._insert("INSERT INTO marcas(nombre) VALUES (%s)", ["AirSep"])
        tipo_id = self._insert(
            "INSERT INTO marca_tipos_equipo(marca_id, nombre, activo) VALUES (%s,%s,%s)",
            [marca_id, "CONCENTRADOR", True],
        )
        serie_id = self._insert(
            "INSERT INTO marca_series(marca_id, tipo_id, nombre, activo) VALUES (%s,%s,%s,%s)",
            [marca_id, tipo_id, "NewLife Elite", True],
        )
        model_id = self._seed_model(marca_id, "NewLife Elite", "CONCENTRADOR")
        self._insert(
            "INSERT INTO marca_series_variantes(marca_id, tipo_id, serie_id, nombre, activo) VALUES (%s,%s,%s,%s,%s)",
            [marca_id, tipo_id, serie_id, "Elite-5", True],
        )

        self.assertEqual(self._names_for_model(model_id), ["Elite-5"])

    def test_modelo_variantes_uses_simple_model_variant_without_series(self):
        marca_id = self._insert("INSERT INTO marcas(nombre) VALUES (%s)", ["Legacy"])
        model_id = self._seed_model(marca_id, "Modelo viejo", "Tipo viejo", "VIEJA-1")

        self.assertEqual(self._names_for_model(model_id), ["VIEJA-1"])

    def test_modelo_variantes_empty_without_model_variants(self):
        marca_id = self._insert("INSERT INTO marcas(nombre) VALUES (%s)", ["Sin variantes"])
        model_id = self._seed_model(marca_id, "Modelo vacio", "Tipo")

        self.assertEqual(self._names_for_model(model_id), [])
