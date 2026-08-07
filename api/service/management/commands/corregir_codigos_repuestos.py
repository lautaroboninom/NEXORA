from django.core.management.base import BaseCommand, CommandError

from ...repuestos import corregir_codigos_repuestos, load_maestro_codigos_from_excel


class Command(BaseCommand):
    help = "Corrige codigos de catalogo_repuestos (codigo interno viejo -> codigo Bejerman) segun el maestro"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Ruta del Excel maestro de codigos Bejerman")
        parser.add_argument("--sheet", default="CODIGOS", help="Hoja a leer (default: CODIGOS)")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo mostrar que se haria, sin escribir en la base",
        )

    def handle(self, *args, **opts):
        file_path = opts["file"]
        sheet = opts["sheet"]
        dry_run = opts["dry_run"]

        items = load_maestro_codigos_from_excel(file_path, sheet=sheet)
        if not items:
            raise CommandError(f"No se encontraron filas validas en '{file_path}' hoja '{sheet}'")

        res = corregir_codigos_repuestos(items, dry_run=dry_run)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}renombrados={len(res['renombrados'])} "
            f"sin_cambio={len(res['sin_cambio'])} "
            f"pendientes(sin bejerman asignado)={len(res['pendientes'])} "
            f"no_encontrados_en_catalogo={len(res['no_encontrados'])} "
            f"colisiones={len(res['colisiones'])}"
        )
        if res["renombrados"]:
            self.stdout.write("Renombres:")
            for old, new in res["renombrados"]:
                self.stdout.write(f"  {old} -> {new}")
        if res["colisiones"]:
            self.stdout.write(self.style.WARNING("COLISIONES (no aplicadas, requieren revision manual):"))
            for c in res["colisiones"]:
                self.stdout.write(f"  {c['codigo_viejo']} -> {c['codigo_nuevo']} (ya existe otra fila con ese codigo)")
