from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.auth import issue_token
from service.models import User
from service.permissions import resolve_effective_permissions


class RecepcionPermissionsAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ensure_tables()
        super().setUpClass()

    @classmethod
    def _ensure_tables(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            bool_default = "1"
            bool_false = "0"
            datetime_type = "DATETIME"
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "TRUE"
            bool_false = "FALSE"
            datetime_type = "TIMESTAMPTZ"
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "1"
            bool_false = "0"
            datetime_type = "DATETIME"
        engine_suffix = " ENGINE=InnoDB" if vendor == "mysql" else ""

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
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
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
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    razon_social TEXT,
                    cod_empresa TEXT,
                    telefono TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT,
                    tecnico_id INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS models (
                    id {auto_inc},
                    marca_id INT NULL,
                    nombre TEXT,
                    tipo_equipo TEXT,
                    tecnico_id INT NULL,
                    variante TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INT,
                    marca_id INT NULL,
                    model_id INT NULL,
                    numero_serie TEXT,
                    numero_interno TEXT,
                    propietario TEXT,
                    propietario_nombre TEXT,
                    propietario_contacto TEXT,
                    propietario_doc TEXT,
                    variante TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS locations (
                    id {auto_inc},
                    nombre TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    device_id INT,
                    motivo TEXT,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    resolucion TEXT,
                    fecha_ingreso {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    fecha_creacion {datetime_type} DEFAULT CURRENT_TIMESTAMP,
                    fecha_servicio {datetime_type} NULL,
                    garantia_reparacion {bool_type} DEFAULT {bool_false},
                    garantia_fabrica {bool_type} DEFAULT {bool_false},
                    etiq_garantia_ok {bool_type} DEFAULT {bool_default},
                    faja_garantia TEXT,
                    remito_ingreso TEXT,
                    remito_salida TEXT,
                    factura_numero TEXT,
                    fecha_entrega {datetime_type} NULL,
                    alquilado {bool_type} DEFAULT {bool_false},
                    alquiler_a TEXT,
                    alquiler_remito TEXT,
                    alquiler_fecha DATE NULL,
                    informe_preliminar TEXT,
                    descripcion_problema TEXT,
                    trabajos_realizados TEXT,
                    comentarios TEXT,
                    accesorios TEXT,
                    equipo_variante TEXT,
                    ubicacion_id INT,
                    asignado_a INT NULL,
                    recibido_por INT NULL
                ){engine_suffix}
                """
            )
            if vendor == "postgresql":
                for column_sql in (
                    "ADD COLUMN IF NOT EXISTS propietario TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_nombre TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_contacto TEXT",
                    "ADD COLUMN IF NOT EXISTS propietario_doc TEXT",
                ):
                    cur.execute(f"ALTER TABLE devices {column_sql}")
                for column_sql in (
                    "ADD COLUMN IF NOT EXISTS device_id INT",
                    "ADD COLUMN IF NOT EXISTS motivo TEXT",
                    "ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT",
                    "ADD COLUMN IF NOT EXISTS resolucion TEXT",
                    "ADD COLUMN IF NOT EXISTS fecha_ingreso TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
                    "ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
                    "ADD COLUMN IF NOT EXISTS fecha_servicio TIMESTAMPTZ NULL",
                    "ADD COLUMN IF NOT EXISTS garantia_reparacion BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS garantia_fabrica BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS etiq_garantia_ok BOOLEAN DEFAULT TRUE",
                    "ADD COLUMN IF NOT EXISTS faja_garantia TEXT",
                    "ADD COLUMN IF NOT EXISTS remito_ingreso TEXT",
                    "ADD COLUMN IF NOT EXISTS remito_salida TEXT",
                    "ADD COLUMN IF NOT EXISTS factura_numero TEXT",
                    "ADD COLUMN IF NOT EXISTS fecha_entrega TIMESTAMPTZ NULL",
                    "ADD COLUMN IF NOT EXISTS alquilado BOOLEAN DEFAULT FALSE",
                    "ADD COLUMN IF NOT EXISTS alquiler_a TEXT",
                    "ADD COLUMN IF NOT EXISTS alquiler_remito TEXT",
                    "ADD COLUMN IF NOT EXISTS alquiler_fecha DATE NULL",
                    "ADD COLUMN IF NOT EXISTS informe_preliminar TEXT",
                    "ADD COLUMN IF NOT EXISTS descripcion_problema TEXT",
                    "ADD COLUMN IF NOT EXISTS trabajos_realizados TEXT",
                    "ADD COLUMN IF NOT EXISTS comentarios TEXT",
                    "ADD COLUMN IF NOT EXISTS accesorios TEXT",
                    "ADD COLUMN IF NOT EXISTS equipo_variante TEXT",
                    "ADD COLUMN IF NOT EXISTS ubicacion_id INT",
                    "ADD COLUMN IF NOT EXISTS asignado_a INT NULL",
                    "ADD COLUMN IF NOT EXISTS recibido_por INT NULL",
                ):
                    cur.execute(f"ALTER TABLE ingresos {column_sql}")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id {auto_inc},
                    ingreso_id INT,
                    a_estado TEXT,
                    ts {datetime_type} DEFAULT CURRENT_TIMESTAMP
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS catalogo_accesorios (
                    id {auto_inc},
                    nombre TEXT,
                    activo {bool_type} DEFAULT {bool_default}
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingreso_accesorios (
                    id {auto_inc},
                    ingreso_id INT,
                    accesorio_id INT,
                    referencia TEXT,
                    descripcion TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingreso_alquiler_accesorios (
                    id {auto_inc},
                    ingreso_id INT,
                    accesorio_id INT,
                    referencia TEXT,
                    descripcion TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS catalogo_tipos_equipo (
                    id {auto_inc},
                    nombre TEXT,
                    activo {bool_type} DEFAULT {bool_default}
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marca_tipos_equipo (
                    id {auto_inc},
                    marca_id INT,
                    nombre TEXT,
                    activo {bool_type} DEFAULT {bool_default}
                ){engine_suffix}
                """
            )

    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cur:
            for table in (
                "ingreso_events",
                "ingresos",
                "devices",
                "models",
                "marcas",
                "locations",
                "customers",
                "catalogo_accesorios",
                "ingreso_accesorios",
                "ingreso_alquiler_accesorios",
                "catalogo_tipos_equipo",
                "marca_tipos_equipo",
                "user_permission_overrides",
            ):
                cur.execute(f"DELETE FROM {table}")
        User.objects.all().delete()

        cls.recepcion_user = User.objects.create(
            nombre="Recepción Test",
            email="recepcion-permisos@example.com",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        cls.tecnico_user = User.objects.create(
            nombre="Técnico Test",
            email="tecnico-permisos@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )

        with connection.cursor() as cur:
            cur.execute("INSERT INTO customers(id, razon_social) VALUES (1, 'Cliente Test')")
            cur.execute("INSERT INTO marcas(id, nombre) VALUES (1, 'Marca Test')")
            cur.execute(
                "INSERT INTO models(id, marca_id, nombre, tipo_equipo, tecnico_id) VALUES (1, 1, 'Modelo Test', 'CPAP', %s)",
                [cls.tecnico_user.id],
            )
            cur.execute("INSERT INTO devices(id, customer_id, marca_id, model_id, numero_serie, numero_interno) VALUES (1, 1, 1, 1, 'NS-1', 'MG 0001')")
            cur.execute("INSERT INTO locations(id, nombre) VALUES (1, 'taller')")
            cur.execute(
                """
                INSERT INTO ingresos(id, device_id, motivo, estado, presupuesto_estado, resolucion, ubicacion_id, asignado_a, recibido_por)
                VALUES (1, 1, 'reparacion', 'liberado', 'no_aplica', 'reparado', 1, %s, %s)
                """,
                [cls.tecnico_user.id, cls.recepcion_user.id],
            )
            cur.execute(
                """
                INSERT INTO ingresos(id, device_id, motivo, estado, presupuesto_estado, resolucion, ubicacion_id, asignado_a, recibido_por)
                VALUES (2, 1, 'reparacion', 'ingresado', 'pendiente', NULL, 1, %s, %s)
                """,
                [cls.tecnico_user.id, cls.recepcion_user.id],
            )
            cur.execute("INSERT INTO ingreso_events(ingreso_id, a_estado) VALUES (1, 'liberado')")
            cur.execute("INSERT INTO catalogo_accesorios(id, nombre, activo) VALUES (1, 'Bolso', TRUE)")
            cur.execute("INSERT INTO catalogo_tipos_equipo(id, nombre, activo) VALUES (1, 'CPAP', TRUE)")

        cls.recepcion_token = issue_token(cls.recepcion_user)

    def _client(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.recepcion_token}")
        return client

    def test_recepcion_does_not_view_legacy_liberados_page(self):
        client = self._client()

        listos_resp = client.get("/api/listos-para-retiro/")
        self.assertEqual(listos_resp.status_code, 403)

        derivados_resp = client.get("/api/ingresos/derivados/")
        self.assertEqual(derivados_resp.status_code, 403)

        historico_resp = client.get("/api/ingresos/")
        self.assertEqual(historico_resp.status_code, 403)

    def test_recepcion_can_read_new_ingreso_dependencies(self):
        client = self._client()

        self.assertEqual(client.get("/api/catalogos/tecnicos/").status_code, 200)
        self.assertEqual(client.get("/api/catalogos/accesorios/").status_code, 200)
        self.assertEqual(client.get("/api/catalogos/tipos-equipo/").status_code, 200)
        self.assertEqual(client.get("/api/scan/lookup/").status_code, 400)
        self.assertEqual(client.get("/api/equipos/garantia-reparacion/").status_code, 200)
        self.assertEqual(client.get("/api/equipos/garantia-fabrica/").status_code, 200)

    def test_page_liberados_does_not_allow_non_liberado_detail(self):
        resp = self._client().get("/api/ingresos/2/")
        self.assertEqual(resp.status_code, 403)

    def test_service_sheet_principal_override_grants_detail_permission(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_permission_overrides (user_id, permission_code, effect)
                VALUES (%s, 'page.service_sheet_principal', 'allow')
                """,
                [self.recepcion_user.id],
            )

        permissions = resolve_effective_permissions(user_id=self.recepcion_user.id, role=self.recepcion_user.rol)

        self.assertTrue(permissions["page.service_sheet_principal"])
        self.assertFalse(permissions["page.liberados"])
