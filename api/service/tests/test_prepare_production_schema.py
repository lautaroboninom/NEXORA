from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from django.core.management import get_commands
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from service.management.commands.prepare_production_schema import (
    EXCLUDED_SCHEMA_COMMANDS,
    SCHEMA_COMMANDS,
    Command,
)

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "management" / "commands"


def _apply_commands_on_disk() -> set[str]:
    return {path.stem for path in COMMANDS_DIR.glob("apply_*.py")}


class SchemaCommandInventoryTests(SimpleTestCase):
    """Candado contra la deriva.

    La lista de esquemas vivió repetida en cuatro archivos desincronizados (los tres compose y el
    script de deploy) hasta que se centralizó en prepare_production_schema. Estos tests son lo que
    impide que vuelva a pasar: un `apply_*` nuevo que nadie declaró rompe acá, no en producción.
    """

    def test_todo_apply_del_repo_esta_declarado(self):
        declarados = {name for name, _ in SCHEMA_COMMANDS} | set(EXCLUDED_SCHEMA_COMMANDS)
        sin_declarar = _apply_commands_on_disk() - declarados

        self.assertEqual(
            sin_declarar,
            set(),
            "Hay comandos apply_* sin declarar: "
            f"{sorted(sin_declarar)}. Agregalos a SCHEMA_COMMANDS en "
            "prepare_production_schema.py si tienen que correr al preparar la base, o a "
            "EXCLUDED_SCHEMA_COMMANDS con el motivo por el que no.",
        )

    def test_no_se_declara_nada_que_no_exista(self):
        declarados = {name for name, _ in SCHEMA_COMMANDS} | set(EXCLUDED_SCHEMA_COMMANDS)
        fantasmas = declarados - _apply_commands_on_disk()

        self.assertEqual(fantasmas, set(), f"Comandos declarados que ya no existen: {sorted(fantasmas)}")

    def test_ningun_comando_esta_en_las_dos_listas(self):
        repetidos = {name for name, _ in SCHEMA_COMMANDS} & set(EXCLUDED_SCHEMA_COMMANDS)

        self.assertEqual(repetidos, set(), f"Declarados a la vez como incluidos y excluidos: {sorted(repetidos)}")

    def test_la_lista_no_tiene_duplicados(self):
        nombres = [name for name, _ in SCHEMA_COMMANDS]

        self.assertEqual(len(nombres), len(set(nombres)))

    def test_los_comandos_a_correr_estan_registrados_en_django(self):
        # Un typo en la tupla se descubriria recien al arrancar el contenedor.
        registrados = set(get_commands())
        for name, _ in SCHEMA_COMMANDS:
            with self.subTest(command=name):
                self.assertIn(name, registrados)

    def test_cada_exclusion_tiene_motivo(self):
        for name, reason in EXCLUDED_SCHEMA_COMMANDS.items():
            with self.subTest(command=name):
                self.assertTrue(reason.strip(), f"{name} esta excluido sin explicar por que")

    def test_el_comando_que_pide_input_externo_nunca_se_corre(self):
        # apply_traced_device_corrections exige --input con un archivo de correcciones aprobadas:
        # si entrara en la lista de arranque, el contenedor no levantaria nunca.
        self.assertIn("apply_traced_device_corrections", EXCLUDED_SCHEMA_COMMANDS)


class PrepareProductionSchemaHandleTests(SimpleTestCase):
    def _run(self):
        command = Command()
        with (
            patch("service.management.commands.prepare_production_schema.connection"),
            patch("service.management.commands.prepare_production_schema.call_command") as call_command,
        ):
            command.handle()
        return call_command

    @override_settings(STATIC_ROOT="/app/staticfiles")
    def test_corre_migrate_los_esquemas_en_orden_y_collectstatic(self):
        call_command = self._run()

        esperado = [call("migrate", interactive=False)]
        esperado += [call(name, **kwargs) for name, kwargs in SCHEMA_COMMANDS]
        esperado += [call("collectstatic", interactive=False)]
        self.assertEqual(call_command.call_args_list, esperado)

    @override_settings(STATIC_ROOT="")
    def test_saltea_collectstatic_sin_static_root(self):
        # Es lo que permite usar el mismo comando en desarrollo: settings.py no define STATIC_ROOT
        # y collectstatic abortaria con ImproperlyConfigured.
        call_command = self._run()

        self.assertNotIn(call("collectstatic", interactive=False), call_command.call_args_list)
        self.assertEqual(call_command.call_args_list[0], call("migrate", interactive=False))


@override_settings(STATIC_ROOT="/app/staticfiles")
class PrepareProductionSchemaResilienceTests(SimpleTestCase):
    """Un esquema que no aplica no puede dejar la API sin arrancar.

    En `docker-compose.prod.yml` el comando esta encadenado con `&&` antes de gunicorn:
    un CommandError acá se traduce en produccion caida. Y hay comandos que fallan por
    datos preexistentes legales (`apply_derivation_workflow_schema` aborta si hay dos
    derivaciones activas para la misma OS), asi que la falla tiene que ser ruidosa,
    no fatal.
    """

    PRIMERO = SCHEMA_COMMANDS[0][0]

    def _run(self, failing=None, error="datos duplicados", **options):
        command = Command(stdout=StringIO(), stderr=StringIO(), no_color=True)

        def fake_call_command(name, *args, **kwargs):
            if name == failing:
                raise CommandError(error)

        with (
            patch("service.management.commands.prepare_production_schema.connection"),
            patch(
                "service.management.commands.prepare_production_schema.call_command",
                side_effect=fake_call_command,
            ) as call_command,
        ):
            command.handle(**options)
        return command, call_command

    def test_un_esquema_que_falla_no_corta_los_siguientes_ni_collectstatic(self):
        command, call_command = self._run(failing=self.PRIMERO)

        llamados = [llamada.args[0] for llamada in call_command.call_args_list]
        for name, _ in SCHEMA_COMMANDS:
            self.assertIn(name, llamados, f"{name} tendria que haberse intentado igual")
        self.assertIn(call("collectstatic", interactive=False), call_command.call_args_list)

    def test_el_resumen_nombra_los_esquemas_que_no_aplicaron(self):
        command, _ = self._run(failing=self.PRIMERO, error="derivaciones activas duplicadas")

        salida = command.stderr._out.getvalue()
        self.assertIn(self.PRIMERO, salida)
        self.assertIn("derivaciones activas duplicadas", salida)
        self.assertIn("NO estan en la base", salida.replace("á", "a").replace("á", "a"))

    def test_el_lock_se_libera_aunque_falle_un_esquema(self):
        with (
            patch("service.management.commands.prepare_production_schema.connection") as conn,
            patch(
                "service.management.commands.prepare_production_schema.call_command",
                side_effect=lambda name, *a, **k: (_ for _ in ()).throw(CommandError("x"))
                if name == self.PRIMERO
                else None,
            ),
        ):
            Command(stdout=StringIO(), stderr=StringIO(), no_color=True).handle()

        ejecutados = [
            llamada.args[0]
            for llamada in conn.cursor.return_value.__enter__.return_value.execute.call_args_list
        ]
        self.assertIn("SELECT pg_advisory_unlock(%s)", ejecutados)

    def test_strict_vuelve_a_abortar_en_el_primero(self):
        with self.assertRaises(CommandError):
            self._run(failing=self.PRIMERO, strict=True)

    def test_migrate_sigue_siendo_fatal(self):
        # Sin migraciones no hay base sobre la que aplicar esquemas: seguir seria a ciegas.
        with self.assertRaises(CommandError):
            self._run(failing="migrate")
