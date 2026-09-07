"""La suplencia tiene que poder deshacerse exactamente.

Lo que falla cuando se hace a mano no es poner el reemplazo: es sacarlo. Nadie
revierte lo que nadie anotó, y la persona se queda con permisos que ya no le tocan.
Por eso lo que más se prueba acá es el cierre — que devuelva el rol de antes, que
saque **sólo** los permisos que la suplencia dio, y que una preferencia de
notificación que la persona ya tenía configurada vuelva a su valor y no se borre.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from unittest import skipUnless

from service.management.commands.suplencia import notificaciones_a_prender


@skipUnless(connection.vendor == "postgresql", "La suplencia requiere PostgreSQL")
class SuplenciaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  nombre TEXT NOT NULL,
                  email TEXT NOT NULL UNIQUE,
                  hash_pw TEXT NOT NULL,
                  rol TEXT NOT NULL,
                  activo BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
        call_command("apply_notifications_schema", verbosity=0)
        call_command("apply_user_permissions_schema", verbosity=0)
        call_command("apply_suplencias_schema", verbosity=0)

    def setUp(self):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM suplencias")
            cur.execute("DELETE FROM notification_user_preferences")
            cur.execute("DELETE FROM user_permission_overrides")
            cur.execute(
                """
                INSERT INTO users (nombre, email, hash_pw, rol)
                VALUES ('Lucas', 'suplente@test.local', 'x', 'recepcion')
                ON CONFLICT (email) DO UPDATE SET rol = 'recepcion', activo = TRUE
                RETURNING id
                """
            )
            self.suplente_id = cur.fetchone()[0]

    def _rol(self):
        with connection.cursor() as cur:
            cur.execute("SELECT rol FROM users WHERE id = %s", [self.suplente_id])
            return cur.fetchone()[0]

    def _permisos(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT permission_code, effect FROM user_permission_overrides WHERE user_id = %s",
                [self.suplente_id],
            )
            return dict(cur.fetchall())

    def _preferencias(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT notification_key, bell_enabled, email_enabled
                  FROM notification_user_preferences WHERE user_id = %s
                """,
                [self.suplente_id],
            )
            return {fila[0]: (fila[1], fila[2]) for fila in cur.fetchall()}

    def _activar(self, **extra):
        salida = StringIO()
        call_command("suplencia", activar=True, usuario="suplente@test.local", stdout=salida, **extra)
        return salida.getvalue()

    def _cerrar(self, **extra):
        salida = StringIO()
        call_command("suplencia", cerrar=True, usuario="suplente@test.local", stdout=salida, **extra)
        return salida.getvalue()

    # -- catálogo ------------------------------------------------------------

    def test_la_lista_de_avisos_sale_del_catalogo(self):
        claves = notificaciones_a_prender("jefe_veedor", "jefe")

        # Se calcula, no está escrita a mano: un aviso nuevo para jefatura entra solo.
        self.assertIn("presupuesto_pendiente", claves)
        self.assertIn("solicitud_baja", claves)
        # `resumen_operativo` ya le llega a jefe_veedor por su rol: no hay que tocarla.
        self.assertNotIn("resumen_operativo", claves)

    # -- simulación ----------------------------------------------------------

    def test_sin_apply_no_escribe_nada(self):
        salida = self._activar()

        self.assertIn("SIMULACION", salida)
        self.assertEqual(self._rol(), "recepcion")
        self.assertEqual(self._permisos(), {})
        self.assertEqual(self._preferencias(), {})

    # -- activación ----------------------------------------------------------

    def test_activar_cambia_rol_y_prende_avisos(self):
        self._activar(apply=True)

        self.assertEqual(self._rol(), "jefe_suplente")
        preferencias = self._preferencias()
        self.assertIn("presupuesto_pendiente", preferencias)
        self.assertEqual(preferencias["presupuesto_pendiente"], (True, True))

    def test_el_suplente_sigue_teniendo_todo_lo_de_tecnico(self):
        # Es el bug que motivó el rol: al reemplazarle el rol por uno de jefatura, un
        # técnico dejaba de ser asignable y la hoja de servicio tiraba
        # "400: técnico inválido".
        from service.permission_catalog import get_role_defaults

        tecnico = get_role_defaults("tecnico")
        suplente = get_role_defaults("jefe_suplente")
        perdidos = sorted(c for c, v in tecnico.items() if v and not suplente.get(c))

        self.assertEqual(perdidos, [])

    def test_no_se_puede_abrir_dos_veces(self):
        self._activar(apply=True)

        with self.assertRaisesRegex(CommandError, "ya tiene la suplencia"):
            self._activar(apply=True)

    def test_un_usuario_desactivado_no_puede_cubrir(self):
        with connection.cursor() as cur:
            cur.execute("UPDATE users SET activo = FALSE WHERE id = %s", [self.suplente_id])

        with self.assertRaisesRegex(CommandError, "desactivado"):
            self._activar(apply=True)

    # -- cierre --------------------------------------------------------------

    def test_cerrar_devuelve_todo_como_estaba(self):
        self._activar(apply=True)

        self._cerrar(apply=True)

        self.assertEqual(self._rol(), "recepcion")
        self.assertEqual(self._permisos(), {})
        self.assertEqual(self._preferencias(), {})

    def test_cerrar_no_toca_los_permisos_propios_de_la_persona(self):
        # Un override que la persona ya tenía por su cuenta no lo puso la suplencia,
        # así que cerrarla no puede quitárselo.
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_permission_overrides (user_id, permission_code, effect)
                VALUES (%s, 'action.presupuesto.manage', 'allow')
                """,
                [self.suplente_id],
            )

        self._activar(apply=True)
        self._cerrar(apply=True)

        self.assertEqual(self._permisos().get("action.presupuesto.manage"), "allow")

    def test_cerrar_restaura_una_preferencia_previa_en_vez_de_borrarla(self):
        # Tenía el aviso apagado a propósito. Al cerrar tiene que volver a estar
        # apagado, no desaparecer y quedar en "lo que diga el rol".
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences
                       (user_id, notification_key, bell_enabled, email_enabled)
                VALUES (%s, 'presupuesto_pendiente', FALSE, FALSE)
                """,
                [self.suplente_id],
            )

        self._activar(apply=True)
        self.assertEqual(self._preferencias()["presupuesto_pendiente"], (True, True))

        self._cerrar(apply=True)
        self.assertEqual(self._preferencias()["presupuesto_pendiente"], (False, False))

    def test_cerrar_sin_suplencia_abierta_avisa(self):
        with self.assertRaisesRegex(CommandError, "no tiene ninguna suplencia abierta"):
            self._cerrar(apply=True)

    def test_se_puede_reabrir_despues_de_cerrar(self):
        self._activar(apply=True)
        self._cerrar(apply=True)

        self._activar(apply=True)

        self.assertEqual(self._rol(), "jefe_suplente")

    # -- estado --------------------------------------------------------------

    def test_estado_lista_la_suplencia_abierta(self):
        self._activar(apply=True, motivo="vacaciones")

        salida = StringIO()
        call_command("suplencia", estado=True, stdout=salida)

        texto = salida.getvalue()
        self.assertIn("Lucas", texto)
        self.assertIn("antes era: recepcion", texto)
        self.assertIn("vacaciones", texto)


class RolSuplenteDadoDeAltaEnTodosLadosTests(SimpleTestCase):
    """El riesgo real de un rol nuevo: olvidarse de darlo de alta en algún lado.

    El código autoriza de dos maneras distintas —por permiso y por listas de roles
    escritas a mano— y las segundas son las peligrosas: si a una le falta el rol
    nuevo, el suplente recibe un "No autorizado" en una pantalla suelta, con el
    titular de vacaciones y sin nadie que pueda diagnosticarlo.

    Estos tests recorren las listas en vez de confiar en que se revisaron todas.
    """

    ROL = "jefe_suplente"

    def test_toda_coleccion_que_acepta_jefatura_acepta_al_suplente(self):
        from service import notifications
        from service.bejerman_user_credentials import TECHNICAL_ROLES
        from service.views import clientes_views, media_views, preventivos_views, work_views
        from service.views import ingreso_tests_views, metricas_views

        colecciones = {
            "MANAGEMENT_NOTIFICATION_ROLES": notifications.MANAGEMENT_NOTIFICATION_ROLES,
            "TECHNICAL_NOTIFICATION_ROLES": notifications.TECHNICAL_NOTIFICATION_ROLES,
            "DELIVERY_NOTIFICATION_ROLES": notifications.DELIVERY_NOTIFICATION_ROLES,
            "BILLING_NOTIFICATION_ROLES": notifications.BILLING_NOTIFICATION_ROLES,
            "REMITO_PDF_DELIVERY_ROLES": notifications.REMITO_PDF_DELIVERY_ROLES,
            "REMITO_PDF_INGRESS_ROLES": notifications.REMITO_PDF_INGRESS_ROLES,
            "TECHNICAL_ROLES": TECHNICAL_ROLES,
            "CUSTOMER_ADMIN_ROLES": clientes_views.CUSTOMER_ADMIN_ROLES,
            "CUSTOMER_MANAGE_ROLES": clientes_views.CUSTOMER_MANAGE_ROLES,
            "MEDIA_VIEW_ROLES": media_views.MEDIA_VIEW_ROLES,
            "MEDIA_MANAGE_ROLES": media_views.MEDIA_MANAGE_ROLES,
            "_PLAN_ROLES": preventivos_views._PLAN_ROLES,
            "_REVISION_ROLES": preventivos_views._REVISION_ROLES,
            "_VIEW_ROLES(preventivos)": preventivos_views._VIEW_ROLES,
            "_EDIT_ROLES(tests)": ingreso_tests_views._EDIT_ROLES,
            "_VIEW_ROLES(tests)": ingreso_tests_views._VIEW_ROLES,
            "VIEW_ROLES(work)": work_views.VIEW_ROLES,
            "MANAGE_ROLES(work)": work_views.MANAGE_ROLES,
            "ACTIVITY_ACTOR_ROLES": metricas_views.ACTIVITY_ACTOR_ROLES,
        }

        faltantes = [
            nombre
            for nombre, valores in colecciones.items()
            if "jefe" in valores and self.ROL not in valores
        ]

        self.assertEqual(faltantes, [], f"Listas sin el rol de suplencia: {faltantes}")

    def test_no_quedan_listas_sql_de_tecnicos_sin_el_suplente(self):
        # La lista de técnicos asignables es la que rompió: un suplente técnico
        # dejaba de poder recibir equipos.
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parents[2] / "service"
        sospechosas = []
        for archivo in raiz.rglob("*.py"):
            if "tests" in archivo.parts:
                continue
            texto = archivo.read_text(encoding="utf-8", errors="ignore")
            if "rol IN ('tecnico','jefe')" in texto:
                sospechosas.append(str(archivo.relative_to(raiz)))

        self.assertEqual(sospechosas, [], f"Consultas de técnicos sin el suplente: {sospechosas}")

    def test_el_suplente_no_puede_configurar_usuarios(self):
        from service.permission_catalog import SUPLENTE_EXCLUSIONES, get_role_defaults

        suplente = get_role_defaults(self.ROL)
        for codigo in SUPLENTE_EXCLUSIONES:
            self.assertFalse(suplente.get(codigo), f"El suplente no debería tener {codigo}")

    def test_el_suplente_tiene_todo_lo_demas(self):
        from service.permission_catalog import PERMISSION_CODES, SUPLENTE_EXCLUSIONES, get_role_defaults

        suplente = get_role_defaults(self.ROL)
        faltantes = sorted(
            codigo
            for codigo in PERMISSION_CODES
            if codigo not in SUPLENTE_EXCLUSIONES and not suplente.get(codigo)
        )

        self.assertEqual(faltantes, [])

    def test_el_rol_figura_en_el_listado_de_roles(self):
        from service.roles import ROLE_KEYS

        self.assertIn(self.ROL, ROLE_KEYS)
