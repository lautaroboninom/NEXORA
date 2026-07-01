from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from service.airsep_serial_fix import (
    apply_local_corrections,
    bejerman_mappings_from_candidates,
    bejerman_remote_options_from_env,
    build_bejerman_sql,
    collect_candidate_backups,
    default_out_dir,
    execute_bejerman_sql_via_plink,
    load_airsep_candidates,
    write_reports,
)


class Command(BaseCommand):
    help = "Corrige numeros de serie AirSep agregando N y genera/aplica SQL Bejerman."

    def add_arguments(self, parser):
        parser.add_argument("--apply-local", action="store_true", help="Aplica cambios en NEXORA PostgreSQL.")
        parser.add_argument("--apply-bejerman", action="store_true", help="Intenta ejecutar el SQL contra Bejerman por SSH/plink.")
        parser.add_argument(
            "--include-history",
            action="store_true",
            default=True,
            help="Incluye tablas historicas Bejerman conocidas. Activo por defecto.",
        )
        parser.add_argument("--skip-history", action="store_true", help="No actualiza historicos Bejerman conocidos.")
        parser.add_argument("--out-dir", default="", help="Directorio de reportes. Default: informes/airsep_serial_fix_<timestamp>.")
        parser.add_argument("--bejerman-database", default="SBDASEP", help="Base SQL Server Bejerman.")
        parser.add_argument("--plink-path", default="", help="Ruta de plink.exe. Default: PLINK_PATH o plink.exe.")
        parser.add_argument("--ssh-host", default="", help="Host SSH Bejerman. Default: BEJERMAN_SSH_HOST o 45.173.2.155.")
        parser.add_argument("--ssh-user", default="", help="Usuario SSH Bejerman. Default: BEJERMAN_SSH_USER o administrator.")
        parser.add_argument(
            "--ssh-password-env",
            default="BEJERMAN_SSH_PASSWORD",
            help="Nombre de variable de entorno que contiene la clave SSH.",
        )
        parser.add_argument("--ssh-hostkey", default="", help="Huella hostkey para plink -hostkey.")
        parser.add_argument("--sql-server", default="", help="SQL Server interno. Default: BEJERMAN_SQL_SERVER o 10.0.200.155,1433.")

    def handle(self, *args, **options):
        include_history = bool(options.get("include_history")) and not bool(options.get("skip_history"))
        out_dir = options.get("out_dir") or default_out_dir()

        candidates = load_airsep_candidates()
        backups = collect_candidate_backups(candidates)
        local_result = apply_local_corrections(candidates, apply=bool(options.get("apply_local")))

        mappings = bejerman_mappings_from_candidates(candidates)
        bejerman_database = options.get("bejerman_database") or "SBDASEP"
        bejerman_sql = build_bejerman_sql(mappings, database=bejerman_database, include_history=include_history)

        remote_status = "not_requested"
        remote_stdout = ""
        remote_stderr = ""
        if options.get("apply_bejerman"):
            remote_options = bejerman_remote_options_from_env()
            if options.get("plink_path"):
                remote_options["plink_path"] = options["plink_path"]
            if options.get("ssh_host"):
                remote_options["ssh_host"] = options["ssh_host"]
            if options.get("ssh_user"):
                remote_options["ssh_user"] = options["ssh_user"]
            if options.get("ssh_hostkey"):
                remote_options["ssh_hostkey"] = options["ssh_hostkey"]
            if options.get("sql_server"):
                remote_options["sql_server"] = options["sql_server"]
            remote_options["sql_database"] = bejerman_database

            import os

            remote_options["ssh_password"] = os.getenv(options.get("ssh_password_env") or "BEJERMAN_SSH_PASSWORD", "")
            if not remote_options["ssh_password"]:
                remote_status = "sql_generated_password_missing"
            else:
                try:
                    completed = execute_bejerman_sql_via_plink(bejerman_sql, **remote_options)
                    remote_stdout = completed.stdout or ""
                    remote_stderr = completed.stderr or ""
                    remote_status = "executed" if completed.returncode == 0 else f"failed_rc_{completed.returncode}"
                except FileNotFoundError:
                    remote_status = "sql_generated_plink_missing"
                except Exception as exc:
                    remote_status = f"failed_{type(exc).__name__}"
                    remote_stderr = str(exc)

        out_path = write_reports(
            out_dir,
            candidates=candidates,
            local_result={
                **local_result,
                "bejerman_remote_status": remote_status,
                "bejerman_remote_stdout": remote_stdout[-4000:],
                "bejerman_remote_stderr": remote_stderr[-4000:],
            },
            backups=backups,
            bejerman_sql=bejerman_sql,
            bejerman_mappings=mappings,
        )

        self.stdout.write(f"Reportes: {Path(out_path)}")
        self.stdout.write(f"Candidatos AirSep: {len(candidates)}")
        self.stdout.write(f"Updates locales: {sum(1 for item in candidates if item.action == 'update')}")
        self.stdout.write(f"Merges locales: {sum(1 for item in candidates if item.action == 'merge')}")
        self.stdout.write(f"Bloqueados: {sum(1 for item in candidates if item.action == 'blocked')}")
        if options.get("apply_local"):
            self.stdout.write(
                "NEXORA aplicado: "
                f"updated={local_result.get('updated_devices', 0)} "
                f"merged={local_result.get('merged_devices', 0)} "
                f"deleted={local_result.get('deleted_devices', 0)} "
                f"blocked={local_result.get('blocked', 0)}"
            )
        else:
            self.stdout.write("NEXORA dry-run: no se escribieron cambios locales.")
        if options.get("apply_bejerman"):
            self.stdout.write(f"Bejerman: {remote_status}")
        else:
            self.stdout.write("Bejerman: SQL generado, no ejecutado.")
