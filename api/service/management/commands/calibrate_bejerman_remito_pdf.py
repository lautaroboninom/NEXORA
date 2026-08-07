"""Deduce la geometría del remito de Bejerman a partir de los PDF reales.

El PDF se imprime sobre papel preimpreso, así que las posiciones tienen que caer
exactamente en las columnas y renglones del formulario. En vez de ajustarlas a ojo,
este comando mide muchos remitos ya emitidos y reporta el consenso por tipo de
comprobante, que es lo que después se carga en `TEMPLATES`.

Mide sobre la línea base (`origin`) y no sobre el bbox, que varía según qué letras
tenga el texto.
"""

import posixpath
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from service.bejerman_pdf_remote import (
    _connect,
    _open_sftp,
    _sftp_path,
    remote_windows_path,
)


# Los overlays de Bejerman son chicos; los archivos grandes son otra cosa
# (presupuestos de NEXORA que quedaron con el nombre colisionado).
MAX_OVERLAY_BYTES = 80_000
MIN_OVERLAY_BYTES = 5_000


def _consenso(valores: list[float], decimales: int = 2) -> tuple[float | None, int, int]:
    """Devuelve (valor más frecuente, veces que aparece, total)."""

    if not valores:
        return None, 0, 0
    contador = Counter(round(v, decimales) for v in valores)
    valor, veces = contador.most_common(1)[0]
    return valor, veces, len(valores)


class Command(BaseCommand):
    help = "Mide la geometría de los remitos reales de Bejerman y reporta el consenso por tipo"

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=r"samtronic:C:\2. SEPID\PDF\REMITOS")
        parser.add_argument("--por-tipo", type=int, default=12, help="Cuántos PDF medir por tipo")
        parser.add_argument("--tipos", default="RIS,RT,RDA,RDN,RSS,RTA,RTN")

    def handle(self, *args, **opts):
        import fitz

        tipos = [t.strip().upper() for t in opts["tipos"].split(",") if t.strip()]
        cliente = _connect()
        try:
            sftp = _open_sftp(cliente)
            directorio = _sftp_path(remote_windows_path(opts["dir"])).rstrip("/")
            entradas = [
                (f.st_mtime, f.filename)
                for f in sftp.listdir_attr(directorio)
                if MIN_OVERLAY_BYTES < f.st_size < MAX_OVERLAY_BYTES and "_0" not in f.filename
            ]
            entradas.sort(reverse=True)  # los más recientes primero: el diseño vigente

            por_tipo: dict[str, list[str]] = defaultdict(list)
            for _mtime, nombre in entradas:
                for tipo in tipos:
                    # `RTR...` empieza con RT, así que hay que descartar los más largos
                    if nombre.upper().startswith(tipo + "R") and len(por_tipo[tipo]) < opts["por_tipo"]:
                        por_tipo[tipo].append(nombre)
                        break

            for tipo in tipos:
                nombres = por_tipo.get(tipo) or []
                if not nombres:
                    self.stdout.write(self.style.WARNING(f"\n{tipo}: sin muestras"))
                    continue

                fechas_x, fechas_y, fechas_sz = [], [], []
                cab_y0, cab_paso, cab_sz = [], [], []
                item_y0, item_paso, item_sz = [], [], []
                columnas: list[float] = []

                for nombre in nombres:
                    try:
                        with sftp.open(posixpath.join(directorio, nombre), "rb") as handle:
                            crudo = handle.read()
                        pagina = fitz.open(stream=crudo, filetype="pdf")[0]
                    except Exception:
                        continue

                    spans = [
                        (s["origin"][1], s["origin"][0], s["size"], s["text"])
                        for bloque in pagina.get_text("dict")["blocks"]
                        for linea in bloque.get("lines", [])
                        for s in linea.get("spans", [])
                        if s["text"].strip()
                    ]
                    if not spans:
                        continue
                    spans.sort()

                    cabecera = [s for s in spans if 60 < s[0] < 180]
                    items = [s for s in spans if s[0] >= 180]

                    arriba = [s for s in spans if s[0] <= 60]
                    if arriba:
                        fechas_y.append(arriba[0][0]); fechas_x.append(arriba[0][1]); fechas_sz.append(arriba[0][2])

                    ys_cab = sorted({round(s[0], 2) for s in cabecera})
                    if ys_cab:
                        cab_y0.append(ys_cab[0]); cab_sz += [s[2] for s in cabecera]
                        cab_paso += [round(ys_cab[i + 1] - ys_cab[i], 2) for i in range(len(ys_cab) - 1)]

                    ys_item = sorted({round(s[0], 2) for s in items})
                    if ys_item:
                        item_y0.append(ys_item[0]); item_sz += [s[2] for s in items]
                        item_paso += [round(ys_item[i + 1] - ys_item[i], 2) for i in range(len(ys_item) - 1)]
                        columnas += [s[1] for s in items]

                self.stdout.write(self.style.SUCCESS(f"\n=== {tipo} ({len(nombres)} muestras)"))
                for etiqueta, datos in (
                    ("fecha  x", fechas_x), ("fecha  y", fechas_y), ("fecha  sz", fechas_sz),
                    ("cabecera y0", cab_y0), ("cabecera paso", cab_paso), ("cabecera sz", cab_sz),
                    ("items y0", item_y0), ("items sz", item_sz),
                ):
                    valor, veces, total = _consenso(datos)
                    if valor is not None:
                        self.stdout.write(f"  {etiqueta:<16} = {valor:<10} ({veces}/{total})")

                pasos = Counter(p for p in item_paso if 5 < p < 40)
                if pasos:
                    self.stdout.write(f"  items paso       = {pasos.most_common(4)}")
                cols = Counter(round(c, 2) for c in columnas)
                frecuentes = [c for c, n in cols.most_common(10) if n >= max(2, len(nombres) // 3)]
                self.stdout.write(f"  columnas x       = {sorted(frecuentes)}")
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            cliente.close()
