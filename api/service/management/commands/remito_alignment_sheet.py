"""Genera la hoja de calibración para imprimir sobre el formulario preimpreso."""

from django.core.management.base import BaseCommand

from service.bejerman_remito_pdf import TEMPLATES, render_alignment_sheet


class Command(BaseCommand):
    help = "Genera una hoja de prueba para verificar el calce sobre el papel preimpreso"

    def add_arguments(self, parser):
        parser.add_argument("--empresa", default="SEPID", help=f"Empresa ({', '.join(TEMPLATES)})")
        parser.add_argument("--salida", required=True, help="Archivo PDF de salida")

    def handle(self, *args, **opts):
        empresa = str(opts["empresa"]).upper()
        if empresa not in TEMPLATES:
            self.stderr.write(self.style.ERROR(f"Empresa desconocida: {empresa}"))
            return
        with open(opts["salida"], "wb") as handle:
            handle.write(render_alignment_sheet(empresa))
        self.stdout.write(self.style.SUCCESS(f"Hoja de calibración {empresa} escrita en {opts['salida']}"))
        self.stdout.write(
            "Sólo hace falta si cambia el talonario: las posiciones vigentes salieron de medir\n"
            "remitos reales. Si se imprime, va sobre un formulario preimpreso: cada marca tiene\n"
            "que caer en su casillero. Si hay que correr todo, es un ajuste global; si se\n"
            "desfasa hacia abajo, es el paso de renglón."
        )
