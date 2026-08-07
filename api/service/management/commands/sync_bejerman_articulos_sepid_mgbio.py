from django.core.management.base import BaseCommand

from ...bejerman_article_catalog_sync import sync_article_catalog


class Command(BaseCommand):
    help = (
        "Compara el catalogo de articulos de Bejerman SEPID (referencia) contra MGBIO y "
        "corrige las descripciones divergentes (siempre) y, con --include-sustantive, "
        "replica la fila completa para los casos donde el codigo apunta a un producto "
        "distinto en cada empresa. Dry-run por defecto; usar --apply para escribir."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin esto solo muestra).")
        parser.add_argument(
            "--include-sustantive",
            action="store_true",
            help="Ademas de las descripciones, replica la fila completa para los casos sustantivos.",
        )
        parser.add_argument(
            "--codes",
            default="",
            help="Lista de codigos separados por coma para restringir el alcance (default: todos los divergentes).",
        )

    def handle(self, *args, **opts):
        codes = [c.strip() for c in opts["codes"].split(",") if c.strip()] or None

        summary = sync_article_catalog(
            apply=opts["apply"],
            include_sustantive=opts["include_sustantive"],
            codes=codes,
        )

        prefix = "" if opts["apply"] else "[dry-run] "
        self.stdout.write(
            f"{prefix}{summary['reference']} -> {summary['target']}: "
            f"{summary['totalDiscrepancies']} discrepancias "
            f"(cosmeticas={summary['cosmetic']['total']} aplicadas={summary['cosmetic']['applied']}, "
            f"sustantivas={summary['sustantive']['total']} aplicadas={summary['sustantive']['appliedFull']} "
            f"pendientes_revision={summary['sustantive']['needsReview']})"
        )

        for row in summary["rows"]:
            marker = "OK" if row["fieldsChanged"] else "--"
            self.stdout.write(
                f"  [{marker}] {row['code']} ({row['classification']}, sim={row['similarity']}) "
                f"'{row['descriptionTarget']}' -> '{row['descriptionReference']}'"
            )
            for col, values in row["fieldsChanged"].items():
                self.stdout.write(f"        {col}: '{values['before']}' -> '{values['after']}'")
            if row["fkColumnsSkipped"]:
                self.stdout.write(
                    self.style.WARNING(f"        columnas omitidas por FK/compuesta: {', '.join(row['fkColumnsSkipped'])}")
                )
            if row["classification"] == "sustantive" and not row["eligible"]:
                self.stdout.write(
                    self.style.WARNING("        sustantivo: correr con --include-sustantive para replicar la fila completa")
                )

        if summary["errors"]:
            self.stdout.write(self.style.ERROR(f"Errores: {summary['errors']}"))
