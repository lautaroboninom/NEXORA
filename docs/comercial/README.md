## NEXORA Comercial

Paquete editorial y visual para la presentación comercial de NEXORA.

Contenido:
- `content.py`: copy final, orden de páginas, metadata de capturas y layouts.
- `screenshots/`: capturas PNG tomadas del entorno DEV.
- `assets/fonts/`: fuentes embebidas usadas por el generador PDF.

Flujo recomendado:
1. `python scripts/capture_nexora_screens.py`
2. `python scripts/build_nexora_commercial_pdf.py`

Salida final:
- `output/pdf/nexora_presentacion_comercial.pdf`

QA visual:
- `tmp/pdfs/nexora_presentacion/`
