import datetime as dt

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.models import User


class RepuestosCompraMovimientoAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        auto_inc = "INT AUTO_INCREMENT PRIMARY KEY" if vendor != "sqlite" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        bool_type = "BOOLEAN" if vendor != "sqlite" else "INTEGER"
        datetime_type = "DATETIME"
        engine_suffix = "" if vendor == "sqlite" else " ENGINE=InnoDB"

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

        catalogo_repuestos_sql = f"""
            CREATE TABLE IF NOT EXISTS catalogo_repuestos (
                id {auto_inc},
                codigo VARCHAR(64) UNIQUE,
                nombre TEXT,
                costo_neto NUMERIC(12,2) NOT NULL DEFAULT 0,
                costo_usd NUMERIC(12,2) NULL,
                costo_moneda VARCHAR(3) DEFAULT 'USD',
                multiplicador NUMERIC(10,4) NULL,
                stock_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0,
                stock_min NUMERIC(12,2) NOT NULL DEFAULT 0,
                tipo_articulo TEXT NULL,
                categoria TEXT NULL,
                unidad_medida TEXT NULL,
                marca_fabricante TEXT NULL,
                nro_parte TEXT NULL,
                ubicacion_deposito TEXT NULL,
                estado TEXT NULL,
                notas TEXT NULL,
                fecha_ultima_compra DATE NULL,
                fecha_ultimo_conteo DATE NULL,
                fecha_vencimiento DATE NULL,
                activo {bool_type} DEFAULT 1,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """

        proveedores_sql = f"""
            CREATE TABLE IF NOT EXISTS proveedores_externos (
                id {auto_inc},
                nombre VARCHAR(255) UNIQUE,
                contacto TEXT NULL,
                telefono TEXT NULL,
                email TEXT NULL,
                direccion TEXT NULL,
                notas TEXT NULL
            ){engine_suffix}
        """

        repuestos_proveedores_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_proveedores (
                id {auto_inc},
                repuesto_id INT NOT NULL,
                proveedor_id INT NOT NULL,
                sku_proveedor TEXT NULL,
                lead_time_dias INT NULL,
                prioridad INT NULL,
                ultima_compra DATE NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                updated_at {datetime_type} DEFAULT CURRENT_TIMESTAMP
            ){engine_suffix}
        """

        repuestos_movimientos_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_movimientos (
                id {auto_inc},
                repuesto_id INT NOT NULL,
                tipo TEXT NOT NULL,
                qty NUMERIC(12,2) NOT NULL,
                stock_prev NUMERIC(12,2) NULL,
                stock_new NUMERIC(12,2) NULL,
                ref_tipo TEXT NULL,
                ref_id INT NULL,
                nota TEXT NULL,
                fecha_compra DATE NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                created_by INT NULL
            ){engine_suffix}
        """

        repuestos_stock_permisos_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_stock_permisos (
                id {auto_inc},
                tecnico_id INT NOT NULL,
                enabled_by INT NULL,
                created_at {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                expires_at {datetime_type} NOT NULL,
                revoked_at {datetime_type} NULL,
                revoked_by INT NULL,
                nota TEXT NULL
            ){engine_suffix}
        """

        repuestos_config_sql = f"""
            CREATE TABLE IF NOT EXISTS repuestos_config (
                id {auto_inc},
                dolar_ars NUMERIC(12,4) NOT NULL DEFAULT 0,
                multiplicador_general NUMERIC(10,4) NOT NULL DEFAULT 1
            ){engine_suffix}
        """

        with connection.cursor() as cur:
            cur.execute(users_sql)
            cur.execute(catalogo_repuestos_sql)
            cur.execute(proveedores_sql)
            cur.execute(repuestos_proveedores_sql)
            cur.execute(repuestos_movimientos_sql)
            cur.execute(repuestos_stock_permisos_sql)
            cur.execute(repuestos_config_sql)
        super().setUpClass()

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return cur.fetchone()[0]

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM repuestos_proveedores")
            cur.execute("DELETE FROM repuestos_stock_permisos")
            cur.execute("DELETE FROM catalogo_repuestos")
            cur.execute("DELETE FROM proveedores_externos")
            cur.execute("DELETE FROM repuestos_config")
        User.objects.all().delete()

        cls.jefe_user = User.objects.create(
            nombre="Jefe",
            email="jefe-repuestos@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tech_perm_user = User.objects.create(
            nombre="Tech Perm",
            email="tech-perm@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.tech_no_perm_user = User.objects.create(
            nombre="Tech Sin Perm",
            email="tech-no-perm@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalogo_repuestos
                  (codigo, nombre, stock_on_hand, stock_min, activo, updated_at, fecha_ultima_compra)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                ["1517001", "Valvula test", 10, 2, 1, timezone.now(), None],
            )
            cls.repuesto_id = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO proveedores_externos (nombre)
                VALUES (%s)
                """,
                ["Proveedor Base"],
            )
            cls.proveedor_id = cls._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO repuestos_config (dolar_ars, multiplicador_general)
                VALUES (%s,%s)
                """,
                [1, 1],
            )
            cur.execute(
                """
                INSERT INTO repuestos_stock_permisos
                  (tecnico_id, enabled_by, created_at, expires_at, nota)
                VALUES (%s,%s,%s,%s,%s)
                """,
                [
                    cls.tech_perm_user.id,
                    cls.jefe_user.id,
                    timezone.now(),
                    timezone.now() + dt.timedelta(hours=24),
                    "permiso de test",
                ],
            )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM repuestos_movimientos")
            cur.execute("DELETE FROM repuestos_proveedores")
            cur.execute(
                """
                UPDATE catalogo_repuestos
                   SET stock_on_hand=%s,
                       fecha_ultima_compra=%s,
                       updated_at=%s
                 WHERE id=%s
                """,
                [10, None, timezone.now(), self.repuesto_id],
            )

    def _url_compra(self):
        return "/api/repuestos/movimientos/compra/"

    def _url_movimientos(self):
        return "/api/repuestos/movimientos/"

    def _post_compra(self, user, payload):
        self.client.force_authenticate(user=user)
        return self.client.post(self._url_compra(), payload, format="json")

    def test_jefe_compra_con_proveedor_existente_registra_movimiento_y_stock(self):
        payload = {
            "repuesto_id": self.repuesto_id,
            "cantidad": 5,
            "fecha_compra": "2026-02-13",
            "proveedor_id": self.proveedor_id,
            "nota": "Factura A-0001",
        }
        resp = self._post_compra(self.jefe_user, payload)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data.get("ok"))
        self.assertEqual(int(resp.data["repuesto"]["stock_on_hand"]), 15)
        self.assertEqual(resp.data["movimiento"]["tipo"], "ingreso_compra")
        self.assertEqual(int(resp.data["movimiento"]["qty"]), 5)
        self.assertEqual(resp.data["movimiento"]["proveedor_nombre"], "Proveedor Base")
        self.assertEqual(resp.data["movimiento"]["created_by"], self.jefe_user.id)

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT stock_on_hand, fecha_ultima_compra
                FROM catalogo_repuestos
                WHERE id=%s
                """,
                [self.repuesto_id],
            )
            stock, fecha = cur.fetchone()
            self.assertEqual(int(stock), 15)
            self.assertEqual(str(fecha), "2026-02-13")
            cur.execute(
                """
                SELECT ref_tipo, ref_id, created_by
                FROM repuestos_movimientos
                WHERE repuesto_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                [self.repuesto_id],
            )
            ref_tipo, ref_id, created_by = cur.fetchone()
            self.assertEqual(ref_tipo, "proveedor_externo")
            self.assertEqual(int(ref_id), self.proveedor_id)
            self.assertEqual(int(created_by), self.jefe_user.id)
            cur.execute(
                """
                SELECT ultima_compra
                FROM repuestos_proveedores
                WHERE repuesto_id=%s AND proveedor_id=%s
                """,
                [self.repuesto_id, self.proveedor_id],
            )
            ultima = cur.fetchone()[0]
            self.assertEqual(str(ultima), "2026-02-13")

    def test_tecnico_sin_permiso_no_puede_registrar_compra(self):
        resp = self._post_compra(
            self.tech_no_perm_user,
            {
                "repuesto_id": self.repuesto_id,
                "cantidad": 2,
                "fecha_compra": "2026-02-13",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_tecnico_con_permiso_puede_crear_proveedor_y_vincular(self):
        payload = {
            "repuesto_id": self.repuesto_id,
            "cantidad": 3,
            "fecha_compra": "2026-02-10",
            "proveedor_nombre": "Proveedor Nuevo Test",
        }
        resp = self._post_compra(self.tech_perm_user, payload)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(int(resp.data["repuesto"]["stock_on_hand"]), 13)
        self.assertEqual(resp.data["movimiento"]["proveedor_nombre"], "Proveedor Nuevo Test")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM proveedores_externos WHERE LOWER(nombre)=LOWER(%s)",
                ["Proveedor Nuevo Test"],
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            new_pid = int(row[0])
            cur.execute(
                """
                SELECT ultima_compra
                FROM repuestos_proveedores
                WHERE repuesto_id=%s AND proveedor_id=%s
                """,
                [self.repuesto_id, new_pid],
            )
            rel = cur.fetchone()
            self.assertIsNotNone(rel)
            self.assertEqual(str(rel[0]), "2026-02-10")

    def test_compra_sin_proveedor_es_valida(self):
        resp = self._post_compra(
            self.jefe_user,
            {
                "repuesto_id": self.repuesto_id,
                "cantidad": 1,
                "fecha_compra": "2026-02-11",
            },
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["movimiento"].get("proveedor_nombre"), None)
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT ref_tipo, ref_id
                FROM repuestos_movimientos
                WHERE id=%s
                """,
                [resp.data["movimiento"]["id"]],
            )
            ref_tipo, ref_id = cur.fetchone()
            self.assertIsNone(ref_tipo)
            self.assertIsNone(ref_id)

    def test_fecha_ultima_compra_no_retrocede(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE catalogo_repuestos
                   SET fecha_ultima_compra=%s
                 WHERE id=%s
                """,
                ["2026-02-12", self.repuesto_id],
            )
            cur.execute(
                """
                INSERT INTO repuestos_proveedores
                  (repuesto_id, proveedor_id, ultima_compra, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s)
                """,
                [self.repuesto_id, self.proveedor_id, "2026-02-11", timezone.now(), timezone.now()],
            )

        resp = self._post_compra(
            self.jefe_user,
            {
                "repuesto_id": self.repuesto_id,
                "cantidad": 2,
                "fecha_compra": "2026-02-10",
                "proveedor_id": self.proveedor_id,
            },
        )
        self.assertEqual(resp.status_code, 201)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT fecha_ultima_compra FROM catalogo_repuestos WHERE id=%s",
                [self.repuesto_id],
            )
            self.assertEqual(str(cur.fetchone()[0]), "2026-02-12")
            cur.execute(
                """
                SELECT ultima_compra
                FROM repuestos_proveedores
                WHERE repuesto_id=%s AND proveedor_id=%s
                """,
                [self.repuesto_id, self.proveedor_id],
            )
            self.assertEqual(str(cur.fetchone()[0]), "2026-02-11")

    def test_get_movimientos_devuelve_proveedor_y_fecha_compra(self):
        create = self._post_compra(
            self.jefe_user,
            {
                "repuesto_id": self.repuesto_id,
                "cantidad": 4,
                "fecha_compra": "2026-02-09",
                "proveedor_id": self.proveedor_id,
                "nota": "ingreso test",
            },
        )
        self.assertEqual(create.status_code, 201)

        self.client.force_authenticate(user=self.jefe_user)
        resp = self.client.get(f"{self._url_movimientos()}?repuesto_id={self.repuesto_id}&limit=10")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(len(resp.data) >= 1)
        first = resp.data[0]
        self.assertEqual(first.get("tipo"), "ingreso_compra")
        self.assertEqual(first.get("fecha_compra"), "2026-02-09")
        self.assertEqual(first.get("proveedor_nombre"), "Proveedor Base")
