"""Dos modelos con el mismo nombre pero distinto tipo de equipo son equipos distintos.

Caso real: el CPAP "DM26" de Yamind y el calentador humidificador "DM26" que va con él.
Antes, crear el segundo pisaba el tipo de equipo y el técnico del primero.
"""

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class ModeloHomonimoTipoEquipoTest(TestCase):
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
                    activo {bool_type} DEFAULT {bool_default},
                    bejerman_seller_code TEXT NULL,
                    bejerman_seller_code_confirmed_at TIMESTAMP NULL
                )
                """
            )
            if vendor == "postgresql":
                # users puede venir creada por otro módulo de test sin estas columnas.
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bejerman_seller_code TEXT NULL")
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "bejerman_seller_code_confirmed_at TIMESTAMP NULL"
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
                    nombre TEXT NOT NULL,
                    tecnico_id INTEGER NULL
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    marca_id INTEGER NOT NULL,
                    nombre TEXT NOT NULL,
                    tecnico_id INTEGER NULL,
                    tipo_equipo TEXT NULL,
                    variante TEXT NULL
                )
                """
            )
            if vendor == "postgresql":
                # marcas/models pueden venir creadas por otro módulo de test, más flacas.
                cur.execute("ALTER TABLE marcas ADD COLUMN IF NOT EXISTS tecnico_id INTEGER NULL")
                for col in ("tecnico_id INTEGER", "tipo_equipo TEXT", "variante TEXT"):
                    cur.execute(f"ALTER TABLE models ADD COLUMN IF NOT EXISTS {col} NULL")
            # Misma unicidad que producción: marca + nombre + tipo de equipo.
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_models_marca_nombre_tipo_ci
                  ON models (marca_id, (LOWER(nombre)), (LOWER(COALESCE(TRIM(tipo_equipo), ''))))
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
                CREATE TABLE IF NOT EXISTS catalogo_tipos_equipo (
                    id {auto_inc},
                    nombre VARCHAR(160) NOT NULL UNIQUE,
                    activo {bool_type} NOT NULL DEFAULT {bool_default}
                )
                """
            )

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for table in (
                "catalogo_tipos_equipo",
                "marca_tipos_equipo",
                "models",
                "marcas",
                "user_permission_overrides",
            ):
                cur.execute(f"DELETE FROM {table}")
        User.objects.all().delete()
        self.admin = User.objects.create(
            nombre="Admin Catalogo",
            email="admin-homonimos@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        self.tecnico = User.objects.create(
            nombre="Tecnico Yamind",
            email="tecnico-homonimos@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

        with connection.cursor() as cur:
            cur.execute("INSERT INTO marcas(nombre) VALUES (%s)", ["Yamind"])
            if connection.vendor == "postgresql":
                cur.execute("SELECT LASTVAL()")
            elif connection.vendor == "mysql":
                cur.execute("SELECT LAST_INSERT_ID()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            self.marca_id = int(cur.fetchone()[0])

    def _modelos(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, nombre, COALESCE(tipo_equipo,''), COALESCE(variante,''), tecnico_id
                FROM models WHERE marca_id=%s ORDER BY id
                """,
                [self.marca_id],
            )
            return [
                {"id": r[0], "nombre": r[1], "tipo_equipo": r[2], "variante": r[3], "tecnico_id": r[4]}
                for r in cur.fetchall()
            ]

    def _crear_modelo(self, payload):
        return self.client.post(
            f"/api/catalogos/marcas/{self.marca_id}/modelos/", payload, format="json"
        )

    def test_mismo_nombre_distinto_tipo_convive_sin_pisar(self):
        resp = self._crear_modelo({
            "nombre": "DM26",
            "tipo_equipo": "CPAP",
            "tecnico_id": self.tecnico.id,
            "variante": "20C",
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get("created"))

        # El humidificador que va con ese CPAP: mismo nombre, otro equipo.
        resp = self._crear_modelo({"nombre": "DM26", "tipo_equipo": "Calentador humidificador"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get("created"))

        modelos = self._modelos()
        self.assertEqual(len(modelos), 2, modelos)

        cpap = next(m for m in modelos if m["tipo_equipo"] == "CPAP")
        humi = next(m for m in modelos if m["tipo_equipo"] == "Calentador humidificador")
        self.assertNotEqual(cpap["id"], humi["id"])
        # El CPAP quedó intacto: tipo, técnico y variante.
        self.assertEqual(cpap["nombre"], "DM26")
        self.assertEqual(cpap["tecnico_id"], self.tecnico.id)
        self.assertEqual(cpap["variante"], "20C")
        self.assertEqual(humi["nombre"], "DM26")

    def test_alta_sin_tecnico_no_borra_el_tecnico_existente(self):
        resp = self._crear_modelo({
            "nombre": "DM26", "tipo_equipo": "CPAP", "tecnico_id": self.tecnico.id,
        })
        self.assertEqual(resp.status_code, 200, resp.content)

        # Reenvío del mismo modelo sin el campo técnico: no debe limpiarlo.
        resp = self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json().get("created"))

        modelos = self._modelos()
        self.assertEqual(len(modelos), 1, modelos)
        self.assertEqual(modelos[0]["tecnico_id"], self.tecnico.id)

    def test_tecnico_explicitamente_vacio_si_lo_limpia(self):
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP", "tecnico_id": self.tecnico.id})
        resp = self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP", "tecnico_id": None})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(self._modelos()[0]["tecnico_id"])

    def test_modelo_sin_tipo_se_completa_en_vez_de_duplicarse(self):
        resp = self._crear_modelo({"nombre": "DM26"})
        self.assertEqual(resp.status_code, 200, resp.content)

        resp = self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json().get("created"))

        modelos = self._modelos()
        self.assertEqual(len(modelos), 1, modelos)
        self.assertEqual(modelos[0]["tipo_equipo"], "CPAP")

    def test_alta_sin_tipo_con_homonimos_ambiguos_pide_el_tipo(self):
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "Calentador humidificador"})

        resp = self._crear_modelo({"nombre": "DM26", "tecnico_id": self.tecnico.id})
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(len(self._modelos()), 2)

    def test_renombrar_a_homonimo_de_otro_tipo_es_valido(self):
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self._crear_modelo({"nombre": "DM26-20C", "tipo_equipo": "Calentador humidificador"})
        humi = next(m for m in self._modelos() if m["tipo_equipo"] == "Calentador humidificador")

        resp = self.client.patch(
            f"/api/catalogos/modelos/{humi['id']}/", {"nombre": "DM26"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        nombres = sorted(m["nombre"] for m in self._modelos())
        self.assertEqual(nombres, ["DM26", "DM26"])

    def test_renombrar_a_homonimo_del_mismo_tipo_choca(self):
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self._crear_modelo({"nombre": "DM28", "tipo_equipo": "CPAP"})
        dm28 = next(m for m in self._modelos() if m["nombre"] == "DM28")

        resp = self.client.patch(
            f"/api/catalogos/modelos/{dm28['id']}/", {"nombre": "DM26"}, format="json"
        )
        self.assertEqual(resp.status_code, 409, resp.content)

    def test_cambiar_tipo_hacia_uno_ya_ocupado_choca(self):
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "CPAP"})
        self._crear_modelo({"nombre": "DM26", "tipo_equipo": "Calentador humidificador"})
        humi = next(m for m in self._modelos() if m["tipo_equipo"] == "Calentador humidificador")

        resp = self.client.patch(
            f"/api/catalogos/marcas/{self.marca_id}/modelos/{humi['id']}/tipo-equipo/",
            {"tipo_equipo": "CPAP"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409, resp.content)
        # Nada cambió: el humidificador sigue siendo humidificador.
        self.assertEqual(
            next(m for m in self._modelos() if m["id"] == humi["id"])["tipo_equipo"],
            "Calentador humidificador",
        )
