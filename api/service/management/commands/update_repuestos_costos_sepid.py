from django.core.management.base import BaseCommand, CommandError

from ...repuestos import actualizar_costos_masivos, load_costos_masivos_from_excel


class Command(BaseCommand):
    help = "Actualiza costos (USD) de catalogo_repuestos desde el Excel 'COSTOS VIGENTES SEPID'"

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Ruta del Excel de costos vigentes")
        parser.add_argument("--sheet", default="Hoja1", help="Hoja a leer (default: Hoja1)")
        parser.add_argument(
            "--no-insertar-faltantes",
            action="store_true",
            help="No crear repuestos nuevos para codigos que no existen en el catalogo",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo mostrar que se leyo del excel, sin escribir en la base",
        )

    def handle(self, *args, **opts):
        file_path = opts["file"]
        sheet = opts["sheet"]
        insertar_faltantes = not opts["no_insertar_faltantes"]

        items = load_costos_masivos_from_excel(file_path, sheet=sheet)
        if not items:
            raise CommandError(f"No se encontraron items validos en '{file_path}' hoja '{sheet}'")

        if opts["dry_run"]:
            self.stdout.write(f"[dry-run] {len(items)} items leidos de la hoja '{sheet}'")
            for it in items:
                self.stdout.write(f"  {it['codigo']}  USD {it['costo_usd']}  {it['nombre']}")
            return

        res = actualizar_costos_masivos(items, insertar_faltantes=insertar_faltantes)
        self.stdout.write(
            f"OK actualizados={len(res['actualizados'])} "
            f"insertados={len(res['insertados'])} "
            f"omitidos={len(res['omitidos'])}"
        )
        if res["omitidos"]:
            self.stdout.write("Omitidos (no existian en el catalogo, no se insertaron): " + ", ".join(res["omitidos"]))
