"""Compara span a span el PDF que genera NEXORA contra el que emitió Bejerman.

Es la forma objetiva de verificar que el remito reproducido es idéntico: no compara
bytes (dos PDF nunca son binariamente iguales) sino lo que se ve — cada texto, con su
posición de línea base, su tamaño y su columna.
"""

import json

from django.core.management.base import BaseCommand

from service.bejerman_remito_pdf import render_remito_pdf


TOLERANCIA_PT = 0.5


def _spans(pdf_bytes: bytes) -> list[tuple[float, float, float, str]]:
    import fitz

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    spans: list[tuple[float, float, float, str]] = []
    for page in document:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span["text"].rstrip()
                    if not text.strip():
                        continue
                    spans.append(
                        (round(span["origin"][1], 2), round(span["origin"][0], 2), round(span["size"], 1), text)
                    )
    spans.sort()
    return spans


class Command(BaseCommand):
    help = "Compara el PDF de remito generado por NEXORA contra el original de Bejerman"

    def add_arguments(self, parser):
        parser.add_argument("--comprobante-json", required=True, help="Archivo con el DatosJSON de Bejerman")
        parser.add_argument("--original", required=True, help="Archivo PDF original de Bejerman")
        parser.add_argument("--salida", default="", help="Dónde guardar el PDF generado (opcional)")

    def handle(self, *args, **opts):
        with open(opts["comprobante_json"], "r", encoding="utf-8") as handle:
            comprobante = json.load(handle)
        with open(opts["original"], "rb") as handle:
            original_bytes = handle.read()

        generado_bytes = render_remito_pdf(comprobante)
        if opts["salida"]:
            with open(opts["salida"], "wb") as handle:
                handle.write(generado_bytes)

        original = _spans(original_bytes)
        generado = _spans(generado_bytes)

        self.stdout.write(f"spans original: {len(original)} | generado: {len(generado)}")

        textos_original = {t for *_rest, t in original}
        textos_generado = {t for *_rest, t in generado}
        faltantes = sorted(textos_original - textos_generado)
        sobrantes = sorted(textos_generado - textos_original)

        emparejados = 0
        desalineados = []
        for y, x, size, text in original:
            match = next((g for g in generado if g[3] == text), None)
            if match is None:
                continue
            emparejados += 1
            dy, dx, dsize = abs(match[0] - y), abs(match[1] - x), abs(match[2] - size)
            if dy > TOLERANCIA_PT or dx > TOLERANCIA_PT or dsize > TOLERANCIA_PT:
                desalineados.append((text, (y, x, size), (match[0], match[1], match[2])))

        if faltantes:
            self.stdout.write(self.style.ERROR(f"\nTextos del original que NO se generaron ({len(faltantes)}):"))
            for t in faltantes[:20]:
                self.stdout.write(f"   {t!r}")
        if sobrantes:
            self.stdout.write(self.style.WARNING(f"\nTextos generados que NO están en el original ({len(sobrantes)}):"))
            for t in sobrantes[:20]:
                self.stdout.write(f"   {t!r}")
        if desalineados:
            self.stdout.write(self.style.ERROR(f"\nTextos mal posicionados ({len(desalineados)}):"))
            for text, esperado, obtenido in desalineados[:20]:
                self.stdout.write(f"   {text!r}\n      esperado y/x/sz={esperado}\n      obtenido y/x/sz={obtenido}")

        if not faltantes and not sobrantes and not desalineados:
            self.stdout.write(
                self.style.SUCCESS(f"\nIDÉNTICOS: {emparejados} textos coinciden en contenido, posición y tamaño.")
            )
        else:
            self.stdout.write(
                f"\nCoinciden {emparejados - len(desalineados)} de {len(original)} textos del original."
            )
