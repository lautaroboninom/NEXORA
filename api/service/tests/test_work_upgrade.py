import datetime as dt

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from service.models import User


class WorkUpgradeAPITest(TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = connection.vendor
        if vendor == "postgresql":
            auto_inc = "BIGSERIAL PRIMARY KEY"
            bool_type = "BOOLEAN"
            datetime_type = "TIMESTAMPTZ"
            date_type = "DATE"
        else:
            auto_inc = "INTEGER PRIMARY KEY AUTOINCREMENT"
            bool_type = "INTEGER"
            datetime_type = "DATETIME"
            date_type = "DATE"
        engine_suffix = " ENGINE=InnoDB" if vendor == "mysql" else ""

        with connection.cursor() as cur:
            if vendor == "postgresql":
                cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION set_updated_at()
                    RETURNS TRIGGER AS $$
                    BEGIN
                      NEW.updated_at := CURRENT_TIMESTAMP;
                      RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;
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
                    activo {bool_type} DEFAULT TRUE
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS customers (
                    id {auto_inc},
                    cod_empresa TEXT NULL,
                    razon_social TEXT,
                    cuit TEXT NULL,
                    telefono TEXT NULL,
                    email TEXT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS marcas (
                    id {auto_inc},
                    nombre TEXT
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
                    device_id INT NOT NULL,
                    estado TEXT,
                    presupuesto_estado TEXT,
                    motivo TEXT,
                    equipo_variante TEXT,
                    fecha_ingreso {datetime_type} NULL,
                    fecha_creacion {datetime_type} NULL,
                    fecha_servicio {datetime_type} NULL,
                    fecha_entrega {datetime_type} NULL,
                    ubicacion_id INT NULL,
                    asignado_a INT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS quotes (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    version_num INT NOT NULL DEFAULT 1,
                    origen_quote_id INT NULL,
                    estado TEXT NOT NULL DEFAULT 'pendiente',
                    moneda TEXT NOT NULL DEFAULT 'ARS',
                    total NUMERIC(12,2) NOT NULL DEFAULT 0,
                    fecha_emitido {datetime_type} NULL,
                    fecha_aprobado {datetime_type} NULL,
                    fecha_rechazado {datetime_type} NULL,
                    rechazo_comentario TEXT NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ingreso_events (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    ticket_id INT NULL,
                    a_estado TEXT,
                    ts {datetime_type} NULL
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS proveedores_externos (
                    id {auto_inc},
                    nombre TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipos_derivados (
                    id {auto_inc},
                    ingreso_id INT NOT NULL,
                    proveedor_id INT NULL,
                    fecha_deriv {date_type} NULL,
                    fecha_entrega {date_type} NULL,
                    estado TEXT
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS preventivo_planes (
                    id {auto_inc},
                    scope_type TEXT NOT NULL DEFAULT 'device',
                    device_id INT NULL,
                    customer_id INT NULL,
                    proxima_revision_fecha {date_type} NULL,
                    activa {bool_type} DEFAULT TRUE
                ){engine_suffix}
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS delivery_orders (
                    id TEXT PRIMARY KEY,
                    order_number TEXT,
                    customer_id INT NULL,
                    bejerman_customer_code TEXT NULL,
                    customer_name TEXT,
                    delivery_type TEXT,
                    status TEXT,
                    priority TEXT,
                    order_date {date_type} NULL,
                    equipment_model TEXT NULL,
                    equipment_serial TEXT NULL,
                    equipment_internal_number TEXT NULL,
                    remito_number TEXT NULL,
                    remito_location TEXT NULL,
                    invoice_number TEXT NULL,
                    created_at {datetime_type} NULL,
                    updated_at {datetime_type} NULL
                ){engine_suffix}
                """
            )
            for statement in (
                "ALTER TABLE customers ADD COLUMN cod_empresa TEXT NULL",
                "ALTER TABLE customers ADD COLUMN cuit TEXT NULL",
                "ALTER TABLE customers ADD COLUMN telefono TEXT NULL",
                "ALTER TABLE customers ADD COLUMN email TEXT NULL",
                "ALTER TABLE models ADD COLUMN tipo_equipo TEXT",
                "ALTER TABLE models ADD COLUMN variante TEXT",
                "ALTER TABLE devices ADD COLUMN numero_interno TEXT",
                "ALTER TABLE devices ADD COLUMN variante TEXT",
                f"ALTER TABLE ingresos ADD COLUMN fecha_creacion {datetime_type} NULL",
                f"ALTER TABLE ingresos ADD COLUMN fecha_servicio {datetime_type} NULL",
                f"ALTER TABLE ingresos ADD COLUMN fecha_entrega {datetime_type} NULL",
                "ALTER TABLE ingresos ADD COLUMN presupuesto_estado TEXT",
                "ALTER TABLE ingresos ADD COLUMN motivo TEXT",
                "ALTER TABLE ingresos ADD COLUMN equipo_variante TEXT",
                "ALTER TABLE ingresos ADD COLUMN ubicacion_id INT NULL",
                "ALTER TABLE ingresos ADD COLUMN asignado_a INT NULL",
                "ALTER TABLE quotes ADD COLUMN moneda TEXT NOT NULL DEFAULT 'ARS'",
                "ALTER TABLE quotes ADD COLUMN total NUMERIC(12,2) NOT NULL DEFAULT 0",
                f"ALTER TABLE quotes ADD COLUMN fecha_emitido {datetime_type} NULL",
                f"ALTER TABLE quotes ADD COLUMN fecha_aprobado {datetime_type} NULL",
                "ALTER TABLE ingreso_events ADD COLUMN ticket_id INT NULL",
                "ALTER TABLE ingreso_events ADD COLUMN a_estado TEXT",
                f"ALTER TABLE ingreso_events ADD COLUMN ts {datetime_type} NULL",
                "ALTER TABLE equipos_derivados ADD COLUMN proveedor_id INT NULL",
                f"ALTER TABLE equipos_derivados ADD COLUMN fecha_deriv {date_type} NULL",
                f"ALTER TABLE equipos_derivados ADD COLUMN fecha_entrega {date_type} NULL",
                "ALTER TABLE equipos_derivados ADD COLUMN estado TEXT",
                "ALTER TABLE preventivo_planes ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'device'",
                "ALTER TABLE preventivo_planes ADD COLUMN device_id INT NULL",
                "ALTER TABLE preventivo_planes ADD COLUMN customer_id INT NULL",
                f"ALTER TABLE preventivo_planes ADD COLUMN proxima_revision_fecha {date_type} NULL",
                f"ALTER TABLE preventivo_planes ADD COLUMN activa {bool_type} DEFAULT TRUE",
                "ALTER TABLE delivery_orders ADD COLUMN order_number TEXT",
                "ALTER TABLE delivery_orders ADD COLUMN customer_id INT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN bejerman_customer_code TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN customer_name TEXT",
                "ALTER TABLE delivery_orders ADD COLUMN delivery_type TEXT",
                "ALTER TABLE delivery_orders ADD COLUMN status TEXT",
                "ALTER TABLE delivery_orders ADD COLUMN priority TEXT",
                f"ALTER TABLE delivery_orders ADD COLUMN order_date {date_type} NULL",
                "ALTER TABLE delivery_orders ADD COLUMN equipment_model TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN equipment_serial TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN equipment_internal_number TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN remito_number TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN remito_location TEXT NULL",
                "ALTER TABLE delivery_orders ADD COLUMN invoice_number TEXT NULL",
                f"ALTER TABLE delivery_orders ADD COLUMN created_at {datetime_type} NULL",
                f"ALTER TABLE delivery_orders ADD COLUMN updated_at {datetime_type} NULL",
            ):
                try:
                    cur.execute(statement)
                except Exception:
                    pass
        super().setUpClass()

    @classmethod
    def _last_insert_id(cls, cur):
        if connection.vendor == "postgresql":
            cur.execute("SELECT LASTVAL()")
        else:
            cur.execute("SELECT last_insert_rowid()")
        return int(cur.fetchone()[0])

    @classmethod
    def _insert_id(cls, cur, sql, params=None):
        if connection.vendor == "postgresql":
            cur.execute(f"{sql} RETURNING id", params or [])
            return int(cur.fetchone()[0])
        cur.execute(sql, params or [])
        return cls._last_insert_id(cur)

    @classmethod
    def setUpTestData(cls):
        call_command("apply_work_upgrade_schema", verbosity=0)
        for table in (
            "work_objectives",
            "work_alert_snoozes",
            "work_notification_state",
            "delivery_orders",
            "preventivo_planes",
            "equipos_derivados",
            "ingreso_events",
            "quotes",
            "ingresos",
            "devices",
            "models",
            "marcas",
            "customers",
            "locations",
        ):
            with connection.cursor() as cur:
                cur.execute(f"DELETE FROM {table}")

        User.objects.filter(
            email__in=[
                "jefe-work@example.com",
                "tecnico-work@example.com",
                "tecnico-otro-work@example.com",
                "recepcion-work@example.com",
                "admin-work@example.com",
                "cobranzas-work@example.com",
            ]
        ).delete()
        cls.jefe = User.objects.create(
            nombre="Jefe Trabajo",
            email="jefe-work@example.com",
            hash_pw="",
            rol="jefe",
            activo=True,
        )
        cls.tecnico = User.objects.create(
            nombre="Técnico Trabajo",
            email="tecnico-work@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.otro_tecnico = User.objects.create(
            nombre="Otro Técnico Trabajo",
            email="tecnico-otro-work@example.com",
            hash_pw="",
            rol="tecnico",
            activo=True,
        )
        cls.recepcion = User.objects.create(
            nombre="Recepción Trabajo",
            email="recepcion-work@example.com",
            hash_pw="",
            rol="recepcion",
            activo=True,
        )
        cls.admin = User.objects.create(
            nombre="Administración Trabajo",
            email="admin-work@example.com",
            hash_pw="",
            rol="admin",
            activo=True,
        )
        cls.cobranzas = User.objects.create(
            nombre="Cobranzas Trabajo",
            email="cobranzas-work@example.com",
            hash_pw="",
            rol="cobranzas",
            activo=True,
        )

        now = timezone.now()
        with connection.cursor() as cur:
            taller_id = cls._insert_id(cur, "INSERT INTO locations(nombre) VALUES (%s)", ["Taller"])
            customer_id = cls._insert_id(
                cur,
                "INSERT INTO customers(cod_empresa, razon_social, cuit, telefono, email) VALUES (%s,%s,%s,%s,%s)",
                ["C001", "Clínica Central", "20-1", "111", "ops@example.com"],
            )
            marca_id = cls._insert_id(cur, "INSERT INTO marcas(nombre) VALUES (%s)", ["ResMed"])
            model_id = cls._insert_id(
                cur,
                "INSERT INTO models(marca_id, nombre, tipo_equipo, variante) VALUES (%s,%s,%s,%s)",
                [marca_id, "AirSense", "CPAP", "10"],
            )

            def device(serie, interno):
                return cls._insert_id(
                    cur,
                    "INSERT INTO devices(customer_id, marca_id, model_id, numero_serie, numero_interno, variante) VALUES (%s,%s,%s,%s,%s,%s)",
                    [customer_id, marca_id, model_id, serie, interno, ""],
                )

            old_device = device("SERIE-WIP", "MG 2001")
            unassigned_device = device("SERIE-SIN-TEC", "MG 2002")
            budget_device = device("SERIE-PRESU", "MG 2003")
            delivered_device = device("SERIE-ENT", "MG 2004")
            liberado_device = device("SERIE-LIB", "MG 2005")
            derivado_device = device("SERIE-DER", "MG 2006")
            otro_device = device("SERIE-OTRO", "MG 2007")

            cls.old_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [old_device, "diagnosticado", "pendiente", "reparación", now - dt.timedelta(days=20), now - dt.timedelta(days=20), taller_id, cls.tecnico.id],
            )

            cls.unassigned_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [unassigned_device, "ingresado", "pendiente", "reparación", now - dt.timedelta(days=3), now - dt.timedelta(days=3), taller_id, None],
            )

            budget_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [budget_device, "diagnosticado", "presupuestado", "reparación", now - dt.timedelta(days=9), now - dt.timedelta(days=9), taller_id, cls.tecnico.id],
            )
            cur.execute(
                "INSERT INTO quotes(ingreso_id, estado, moneda, fecha_emitido) VALUES (%s,%s,%s,%s)",
                [budget_ingreso_id, "presupuestado", "ARS", now - dt.timedelta(days=8)],
            )

            cls.delivered_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, fecha_entrega, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [delivered_device, "entregado", "no_aplica", "reparación", now, now, now, taller_id, cls.tecnico.id],
            )

            cls.liberado_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [liberado_device, "liberado", "no_aplica", "reparación", now - dt.timedelta(days=5), now - dt.timedelta(days=5), taller_id, cls.tecnico.id],
            )

            cls.derivado_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [derivado_device, "derivado", "no_aplica", "reparación", now - dt.timedelta(days=12), now - dt.timedelta(days=12), taller_id, cls.tecnico.id],
            )
            cur.execute(
                "INSERT INTO equipos_derivados(ingreso_id, proveedor_id, fecha_deriv, fecha_entrega, estado) VALUES (%s,%s,%s,%s,%s)",
                [cls.derivado_ingreso_id, None, timezone.localdate() - dt.timedelta(days=10), None, "derivado"],
            )

            cls.otro_ingreso_id = cls._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [otro_device, "diagnosticado", "pendiente", "reparación", now - dt.timedelta(days=22), now - dt.timedelta(days=22), taller_id, cls.otro_tecnico.id],
            )

            cur.execute(
                "INSERT INTO preventivo_planes(scope_type, device_id, customer_id, proxima_revision_fecha, activa) VALUES (%s,%s,%s,%s,TRUE)",
                ["device", old_device, None, timezone.localdate() - dt.timedelta(days=1)],
            )
            cur.execute(
                "INSERT INTO preventivo_planes(scope_type, device_id, customer_id, proxima_revision_fecha, activa) VALUES (%s,%s,%s,%s,TRUE)",
                ["device", delivered_device, None, timezone.localdate() + dt.timedelta(days=10)],
            )

            def delivery_order(order_id, number, status, priority="normal", remito_number=None, invoice_number=None):
                cur.execute(
                    """
                    INSERT INTO delivery_orders(
                      id, order_number, customer_id, bejerman_customer_code, customer_name,
                      delivery_type, status, priority, order_date, equipment_model,
                      equipment_serial, equipment_internal_number, remito_number,
                      remito_location, invoice_number, created_at, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        order_id,
                        number,
                        customer_id,
                        "C001",
                        "Clínica Central",
                        "sale",
                        status,
                        priority,
                        timezone.localdate(),
                        "AirSense",
                        "SERIE-PED",
                        "MG 3001",
                        remito_number,
                        "recepcion" if remito_number else None,
                        invoice_number,
                        now,
                        now,
                    ],
                )

            delivery_order("do-work-1", "OE-1", "pendiente_armado", priority="urgente")
            delivery_order("do-work-2", "OE-2", "armado_pendiente_entrega")
            delivery_order("do-work-3", "OE-3", "entregado_pendiente_facturacion", remito_number="R 0001-00000001")
            delivery_order("do-work-4", "OE-4", "facturado", remito_number="R 0001-00000002", invoice_number="F 0001-00000002")

            cur.execute(
                """
                INSERT INTO work_objectives(scope_type, period_type, metric_key, label, target_value, direction, active, valid_from, created_by, updated_by)
                VALUES ('global', 'daily', 'entregados', 'Entregas del día', 1, 'gte', TRUE, CURRENT_DATE, %s, %s)
                """,
                [cls.jefe.id, cls.jefe.id],
            )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.jefe)

    def test_apply_work_upgrade_schema_crea_tablas_base(self):
        rows = []
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name IN ('work_alert_rules','work_objectives','work_alert_snoozes','work_notification_state')
                """
            )
            rows = [r[0] for r in cur.fetchall()]
        self.assertEqual(set(rows), {"work_alert_rules", "work_objectives", "work_alert_snoozes", "work_notification_state"})

    def test_resumen_expone_alertas_y_prioridades(self):
        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["scope"]["dashboard_variant"], "jefe")
        alert_keys = {row["key"] for row in resp.data["alerts"]}
        self.assertIn("wip_critico", alert_keys)
        self.assertIn("sin_tecnico", alert_keys)
        self.assertIn("presupuesto_sin_aprobar", alert_keys)
        self.assertIn("liberado_sin_entregar", alert_keys)
        self.assertIn("derivado_sin_devolucion", alert_keys)
        prioridad_ids = {row["ingreso_id"] for row in resp.data["prioridades"] if row.get("ingreso_id")}
        self.assertIn(self.old_ingreso_id, prioridad_ids)
        kpi_keys = {row["key"] for row in resp.data["kpis"]}
        self.assertEqual(
            kpi_keys,
            {
                "en_taller",
                "wip_critico",
                "sin_tecnico",
                "presupuestos_demorados",
                "liberados_en_espera",
                "derivados_en_espera",
                "pedidos_abiertos",
            },
        )
        self.assertEqual(resp.data["delivery_orders"]["counts"]["active"], 3)
        self.assertEqual(len(resp.data["delivery_orders"]["items"]), 3)

    def test_resumen_deduplica_ingresos_que_caen_en_multiples_alertas(self):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute("SELECT device_id, ubicacion_id FROM ingresos WHERE id = %s", [self.old_ingreso_id])
            device_id, ubicacion_id = cur.fetchone()
            duplicado_ingreso_id = self._insert_id(
                cur,
                """
                INSERT INTO ingresos(device_id, estado, presupuesto_estado, motivo, fecha_ingreso, fecha_creacion, ubicacion_id, asignado_a)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [device_id, "diagnosticado", "presupuestado", "reparacion", now - dt.timedelta(days=20), now - dt.timedelta(days=20), ubicacion_id, self.tecnico.id],
            )
            cur.execute(
                "INSERT INTO quotes(ingreso_id, estado, moneda, fecha_emitido) VALUES (%s,%s,%s,%s)",
                [duplicado_ingreso_id, "presupuestado", "ARS", now - dt.timedelta(days=18)],
            )

        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        duplicados = [row for row in resp.data["prioridades"] if row.get("ingreso_id") == duplicado_ingreso_id]
        self.assertEqual(len(duplicados), 1)
        self.assertEqual(duplicados[0]["alert_key"], "presupuesto_sin_aprobar")

    def test_dashboard_tecnico_muestra_solo_datos_propios(self):
        self.client.force_authenticate(user=self.tecnico)
        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["scope"]["dashboard_variant"], "tecnico")
        kpi_keys = {row["key"] for row in resp.data["kpis"]}
        self.assertEqual(kpi_keys, {"en_taller", "wip_critico", "derivados_en_espera"})
        alert_keys = {row["key"] for row in resp.data["alerts"]}
        self.assertNotIn("sin_tecnico", alert_keys)
        self.assertNotIn("liberado_sin_entregar", alert_keys)
        self.assertNotIn("presupuesto_sin_aprobar", alert_keys)
        self.assertTrue(alert_keys.issubset({"wip_critico", "derivado_sin_devolucion"}))
        for row in resp.data["prioridades"]:
            if row.get("ingreso_id"):
                self.assertEqual(row.get("asignado_a"), self.tecnico.id)
        prioridad_ids = {row["ingreso_id"] for row in resp.data["prioridades"] if row.get("ingreso_id")}
        self.assertIn(self.old_ingreso_id, prioridad_ids)
        self.assertIn(self.derivado_ingreso_id, prioridad_ids)
        self.assertNotIn(self.otro_ingreso_id, prioridad_ids)
        self.assertNotIn(self.liberado_ingreso_id, prioridad_ids)

    def test_dashboard_recepcion_muestra_pedidos_activos_sin_liberados(self):
        self.client.force_authenticate(user=self.recepcion)
        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["scope"]["dashboard_variant"], "recepcion")
        self.assertEqual(
            {row["key"] for row in resp.data["kpis"]},
            {
                "pedidos_pendientes_armado",
                "pedidos_listos_entrega",
                "remitos_pendientes_facturacion",
            },
        )
        self.assertEqual(resp.data["alerts"], [])
        self.assertEqual(resp.data["objetivos"], [])
        self.assertEqual(len(resp.data["delivery_orders"]["items"]), 3)
        self.assertEqual(resp.data["prioridades"], [])

    def test_dashboard_admin_muestra_logistica_y_preventivos(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["scope"]["dashboard_variant"], "admin")
        kpi_keys = {row["key"] for row in resp.data["kpis"]}
        self.assertEqual(
            kpi_keys,
            {
                "liberados_en_espera",
                "derivados_en_espera",
                "preventivos_vencidos",
                "preventivos_proximos",
                "pedidos_pendientes_armado",
                "pedidos_listos_entrega",
            },
        )
        self.assertEqual(
            {row["status"] for row in resp.data["delivery_orders"]["items"]},
            {"pendiente_armado", "armado_pendiente_entrega"},
        )
        alert_keys = {row["key"] for row in resp.data["alerts"]}
        self.assertIn("liberado_sin_entregar", alert_keys)
        self.assertIn("derivado_sin_devolucion", alert_keys)
        self.assertIn("preventivo_vencido", alert_keys)
        self.assertIn("preventivo_proximo", alert_keys)
        self.assertNotIn("wip_critico", alert_keys)
        self.assertNotIn("sin_tecnico", alert_keys)
        self.assertNotIn("presupuesto_sin_aprobar", alert_keys)
        self.assertEqual(resp.data["objetivos"], [])

    def test_dashboard_cobranzas_muestra_remitos_pendientes(self):
        self.client.force_authenticate(user=self.cobranzas)
        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["scope"]["dashboard_variant"], "cobranzas")
        self.assertEqual(
            {row["key"] for row in resp.data["kpis"]},
            {"remitos_pendientes_facturacion", "clientes_pendientes_facturacion"},
        )
        kpis = {row["key"]: row["value"] for row in resp.data["kpis"]}
        self.assertEqual(kpis["clientes_pendientes_facturacion"], 1)
        self.assertEqual(resp.data["alerts"], [])
        self.assertEqual(resp.data["objetivos"], [])
        self.assertEqual(
            [row["status"] for row in resp.data["delivery_orders"]["items"]],
            ["entregado_pendiente_facturacion"],
        )

    def test_objetivos_calculan_progreso_desde_entregas(self):
        resp = self.client.get("/api/trabajo/objetivos/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        items = resp.data["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["metric_key"], "entregados")
        self.assertEqual(items[0]["progress"], 1)
        self.assertEqual(items[0]["status"], "cumplido")

    def test_busqueda_global_agrupa_resultados(self):
        resp = self.client.get("/api/busqueda/global/?q=MG%202001")
        self.assertEqual(resp.status_code, 200)
        groups = {group["key"]: group["items"] for group in resp.data["groups"]}
        self.assertTrue(groups["ingresos"])
        self.assertEqual(groups["ingresos"][0]["ingreso_id"], self.old_ingreso_id)

    def test_snooze_oculta_aviso_para_usuario(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_alert_snoozes(user_id, alert_key, alert_ref, snoozed_until)
                VALUES (%s, %s, %s, %s)
                """,
                [self.jefe.id, "wip_critico", str(self.old_ingreso_id), timezone.now() + dt.timedelta(days=1)],
            )

        resp = self.client.get("/api/trabajo/resumen/?periodo=hoy")
        self.assertEqual(resp.status_code, 200)
        wip = [row for row in resp.data["alerts"] if row["key"] == "wip_critico"]
        wip_ids = {
            item.get("ingreso_id")
            for alert in wip
            for item in (alert.get("items") or [])
        }
        self.assertNotIn(self.old_ingreso_id, wip_ids)
