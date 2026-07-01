from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from service.models import User


class NoSeReparaFlowAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        if vendor == "sqlite":
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            bool_default = "1"
            engine_suffix = ""
        elif vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "TRUE"
            engine_suffix = ""
        else:
            auto_inc = "INT AUTO_INCREMENT PRIMARY KEY"
            bool_type = "BOOLEAN"
            bool_default = "1"
            engine_suffix = " ENGINE=InnoDB"

        with connection.cursor() as cur:
            if vendor == "postgresql":
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_state')
                         AND NOT EXISTS (
                           SELECT 1
                             FROM pg_type t
                             JOIN pg_enum e ON e.enumtypid = t.oid
                            WHERE t.typname = 'ticket_state'
                              AND e.enumlabel = 'no_se_repara'
                         ) THEN
                        ALTER TYPE ticket_state ADD VALUE 'no_se_repara' AFTER 'controlado_sin_defecto';
                      END IF;
                    END $$;
                    """
                )
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
                    updated_by INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    razon_social TEXT NOT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    id {auto_inc},
                    customer_id INT NOT NULL,
                    numero_serie TEXT,
                    numero_interno TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingresos (
                    id {auto_inc},
                    device_id INT,
                    estado TEXT,
                    motivo TEXT,
                    resolucion TEXT,
                    presupuesto_estado TEXT,
                    permite_reparacion {bool_type} DEFAULT {bool_default},
                    asignado_a INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS quotes (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    version_num INT DEFAULT 1,
                    estado TEXT
                ){engine_suffix}
                """
            )
            if vendor == "postgresql":
                cur.execute("CREATE SCHEMA IF NOT EXISTS audit")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit.change_log (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        user_id INTEGER NULL,
                        user_role TEXT NULL,
                        table_name TEXT NOT NULL,
                        record_id INTEGER NOT NULL,
                        column_name TEXT NOT NULL,
                        old_value TEXT NULL,
                        new_value TEXT NULL,
                        ingreso_id INTEGER NULL
                    )
                    """
                )
            try:
                cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS razon_social TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS customer_id INT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_serie TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS numero_interno TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS device_id INT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS estado TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS motivo TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS resolucion TEXT")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_estado TEXT")
                cur.execute(f"ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS permite_reparacion {bool_type} DEFAULT {bool_default}")
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS asignado_a INT NULL")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS ingreso_id INT")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS version_num INT DEFAULT 1")
                cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS estado TEXT")
            except Exception:
                connection.rollback()
        super().setUpClass()

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "sqlite":
            cur.execute("SELECT last_insert_rowid()")
        elif connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT LAST_INSERT_ID()")
        return int(cur.fetchone()[0])

    @classmethod
    def setUpTestData(cls):
        User.objects.all().delete()
        cls.jefe = User.objects.create(
            nombre="Jefe No Se Repara",
            email="jefe-no-se-repara@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Técnico No Se Repara",
            email="tecnico-no-se-repara@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.otro_tecnico = User.objects.create(
            nombre="Otro Técnico",
            email="otro-tecnico-no-se-repara@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM quotes")
            cur.execute("DELETE FROM ingresos")
            cur.execute("INSERT INTO customers (razon_social) VALUES (%s)", ["Cliente No se repara"])
            customer_id = self._last_insert_id(cur)
            cur.execute(
                "INSERT INTO devices (customer_id, numero_serie, numero_interno) VALUES (%s, %s, %s)",
                [customer_id, "SER-NO-SE-REPARA", "INT-NO-SE-REPARA"],
            )
            device_id = self._last_insert_id(cur)
            cur.execute(
                """
                INSERT INTO ingresos (
                    device_id,
                    estado,
                    motivo,
                    resolucion,
                    presupuesto_estado,
                    permite_reparacion,
                    asignado_a
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [device_id, "reparar", "reparacion", None, "aprobado", True, self.tecnico.id],
            )
            self.ingreso_id = self._last_insert_id(cur)
            if connection.vendor == "postgresql":
                cur.execute(
                    "DELETE FROM audit.change_log WHERE table_name='ingresos' AND (record_id=%s OR ingreso_id=%s)",
                    [self.ingreso_id, self.ingreso_id],
                )

    def _estado_resolucion(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT estado, resolucion, presupuesto_estado FROM ingresos WHERE id=%s",
                [self.ingreso_id],
            )
            return cur.fetchone()

    def _insert_approved_quote(self):
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO quotes (ingreso_id, version_num, estado) VALUES (%s, %s, %s)",
                [self.ingreso_id, 1, "aprobado"],
            )

    def _clear_estado_audit(self):
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM audit.change_log WHERE table_name='ingresos' AND record_id=%s AND column_name='estado'",
                [self.ingreso_id],
            )

    def _insert_estado_audit(self, old_value, new_value):
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit.change_log (
                    table_name,
                    record_id,
                    column_name,
                    old_value,
                    new_value,
                    ingreso_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                ["ingresos", self.ingreso_id, "estado", old_value, new_value, self.ingreso_id],
            )

    def _insert_presupuesto_audit(self, old_value, new_value):
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit.change_log (
                    table_name,
                    record_id,
                    column_name,
                    old_value,
                    new_value,
                    ingreso_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                ["ingresos", self.ingreso_id, "presupuesto_estado", old_value, new_value, self.ingreso_id],
            )

    def test_tecnico_asignado_puede_quitar_estados_finales(self):
        self.client.force_authenticate(user=self.tecnico)
        cases = [
            ("reparado", "reparado", "aprobado", False, "diagnosticado", "diagnosticado", "aprobado"),
            ("controlado_sin_defecto", "no_se_encontro_falla", "no_aplica", True, "presupuestado", "presupuestado", "aprobado"),
            ("no_se_repara", "no_reparado", "no_aplica", True, None, "diagnosticado", "aprobado"),
        ]

        for estado_final, resolucion, presupuesto_estado, with_quote, estado_audit, expected_estado, expected_presupuesto in cases:
            with self.subTest(estado_final=estado_final):
                with connection.cursor() as cur:
                    cur.execute("DELETE FROM quotes WHERE ingreso_id=%s", [self.ingreso_id])
                    cur.execute(
                        "UPDATE ingresos SET estado=%s, resolucion=%s, presupuesto_estado=%s WHERE id=%s",
                        [estado_final, resolucion, presupuesto_estado, self.ingreso_id],
                    )
                self._clear_estado_audit()
                if estado_audit:
                    self._insert_estado_audit(estado_audit, estado_final)
                if with_quote:
                    self._insert_approved_quote()

                resp = self.client.post(
                    f"/api/ingresos/{self.ingreso_id}/quitar-estado-final-reparacion/",
                    {},
                    format="json",
                )

                self.assertEqual(resp.status_code, 200, resp.data)
                estado, resolucion_actual, presupuesto_actual = self._estado_resolucion()
                self.assertEqual(estado, expected_estado)
                self.assertIsNone(resolucion_actual)
                self.assertEqual(presupuesto_actual, expected_presupuesto)

    def test_tecnico_no_asignado_no_puede_quitar_estado_final(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='controlado_sin_defecto', resolucion='no_se_encontro_falla', presupuesto_estado='no_aplica' WHERE id=%s",
                [self.ingreso_id],
            )
        self.client.force_authenticate(user=self.otro_tecnico)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/quitar-estado-final-reparacion/", {}, format="json")

        self.assertEqual(resp.status_code, 403)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "controlado_sin_defecto")
        self.assertEqual(resolucion, "no_se_encontro_falla")
        self.assertEqual(presupuesto_estado, "no_aplica")

    def test_quitar_estado_final_rechaza_estados_no_finales_y_cerrados(self):
        self.client.force_authenticate(user=self.jefe)
        for estado_invalido in ("reparar", "liberado"):
            with self.subTest(estado_invalido=estado_invalido):
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE ingresos SET estado=%s, resolucion=NULL, presupuesto_estado='aprobado' WHERE id=%s",
                        [estado_invalido, self.ingreso_id],
                    )

                resp = self.client.post(
                    f"/api/ingresos/{self.ingreso_id}/quitar-estado-final-reparacion/",
                    {},
                    format="json",
                )

                self.assertEqual(resp.status_code, 400)
                estado, resolucion, presupuesto_estado = self._estado_resolucion()
                self.assertEqual(estado, estado_invalido)
                self.assertIsNone(resolucion)
                self.assertEqual(presupuesto_estado, "aprobado")

    def test_jefe_puede_quitar_reparar_y_restaurar_estado_y_presupuesto(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='reparar', resolucion=NULL, presupuesto_estado='aprobado' WHERE id=%s",
                [self.ingreso_id],
            )
        self._clear_estado_audit()
        self._insert_estado_audit("presupuestado", "reparar")
        self._insert_presupuesto_audit("presupuestado", "aprobado")
        self.client.force_authenticate(user=self.jefe)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/quitar-reparar/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "presupuestado")
        self.assertIsNone(resolucion)
        self.assertEqual(presupuesto_estado, "presupuestado")

    def test_quitar_reparar_solo_jefe(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='reparar', resolucion=NULL, presupuesto_estado='aprobado' WHERE id=%s",
                [self.ingreso_id],
            )
        self.client.force_authenticate(user=self.tecnico)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/quitar-reparar/", {}, format="json")

        self.assertEqual(resp.status_code, 403)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "reparar")
        self.assertIsNone(resolucion)
        self.assertEqual(presupuesto_estado, "aprobado")

    def test_quitar_reparar_rechaza_si_no_esta_en_reparar(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='diagnosticado', resolucion=NULL, presupuesto_estado='aprobado' WHERE id=%s",
                [self.ingreso_id],
            )
        self.client.force_authenticate(user=self.jefe)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/quitar-reparar/", {}, format="json")

        self.assertEqual(resp.status_code, 400)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "diagnosticado")
        self.assertIsNone(resolucion)
        self.assertEqual(presupuesto_estado, "aprobado")

    def test_tecnico_asignado_puede_marcar_no_se_repara(self):
        self.client.force_authenticate(user=self.tecnico)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/no-se-repara/", {}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "no_se_repara")
        self.assertEqual(resolucion, "no_reparado")
        self.assertEqual(presupuesto_estado, "no_aplica")

    def test_tecnico_no_asignado_no_puede_marcar_no_se_repara(self):
        self.client.force_authenticate(user=self.otro_tecnico)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/no-se-repara/", {}, format="json")

        self.assertEqual(resp.status_code, 403)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "reparar")
        self.assertIsNone(resolucion)
        self.assertEqual(presupuesto_estado, "aprobado")

    def test_no_se_repara_no_vuelve_a_reparar(self):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='no_se_repara', resolucion='no_reparado', presupuesto_estado='no_aplica' WHERE id=%s",
                [self.ingreso_id],
            )
        self.client.force_authenticate(user=self.jefe)

        resp = self.client.post(f"/api/ingresos/{self.ingreso_id}/reparar/", {}, format="json")

        self.assertEqual(resp.status_code, 400)
        estado, resolucion, presupuesto_estado = self._estado_resolucion()
        self.assertEqual(estado, "no_se_repara")
        self.assertEqual(resolucion, "no_reparado")
        self.assertEqual(presupuesto_estado, "no_aplica")

    @patch("service.views.reportes_views.notify_service_order_ready_to_bill")
    @patch("service.views.reportes_views.ensure_service_release_order_for_ingreso")
    @patch("service.views.reportes_views.notify_ingreso_liberado")
    @patch("service.views.reportes_views.enqueue_client_ready_transfer_for_ingreso")
    @patch("service.views.reportes_views.ingreso_is_demo_return", return_value=False)
    @patch("service.views.reportes_views.ingreso_is_internal_equipment", return_value=False)
    @patch("service.views.reportes_views.render_remito_salida_pdf", return_value=(b"%PDF-1.4 test", "salida.pdf"))
    def test_remito_autocompleta_resolucion_no_reparado_para_no_se_repara(
        self,
        _mock_pdf,
        _mock_internal,
        _mock_demo,
        _mock_transfer,
        _mock_notify_liberado,
        _mock_ensure_order,
        _mock_notify_billing,
    ):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE ingresos SET estado='no_se_repara', resolucion=NULL, presupuesto_estado='no_aplica' WHERE id=%s",
                [self.ingreso_id],
            )
        self.client.force_authenticate(user=self.jefe)

        resp = self.client.get(f"/api/ingresos/{self.ingreso_id}/remito/")

        self.assertEqual(resp.status_code, 200)
        with connection.cursor() as cur:
            cur.execute("SELECT resolucion FROM ingresos WHERE id=%s", [self.ingreso_id])
            resolucion = cur.fetchone()[0]
        self.assertEqual(resolucion, "no_reparado")
