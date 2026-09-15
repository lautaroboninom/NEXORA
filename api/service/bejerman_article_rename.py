"""Detecta cuando al artículo de Bejerman de un modelo le cambiaron el nombre por el de otro modelo.

Pasó con el 1115005: se compró como "Longfian JAY-5Q" y en noviembre de 2024 se lo renombró "JAY-5AW"
porque llegaron equipos de ese modelo. Desde entonces los 5Q que volvían entraban con el nombre de otro
equipo, y para "salir del paso" se abrió otro artículo, lo que partió la historia de cada serie en dos.
Se corrigió en Bejerman el 11/09/2026 (outputs/bejerman_jay5q_5aw_20260911).

El mapeo modelo -> artículo de NEXORA guarda la descripción que tenía el artículo cuando se confirmó.
Si esa descripción nombraba al modelo y la de hoy ya no, lo más probable es que el código se haya
reutilizado para otro equipo: emitir con ese artículo mezclaría la trazabilidad de los dos.

Sin imports de Django a propósito: la regla se prueba sola.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def _tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Un dígito suelto también identifica al modelo: el "5" de JAY-5 no es el "5aw" de JAY-5AW.
    return {part for part in re.sub(r"[^a-z0-9]+", " ", text).split() if len(part) >= 2 or part.isdigit()}


def article_renamed_away_from_model(model_name: Any, mapped_description: Any, live_description: Any) -> bool:
    """True si la descripción del mapeo nombraba al modelo y la descripción viva de Bejerman ya no.

    Un mapeo cuya descripción nunca nombró al modelo (p. ej. "CPAP G2" -> "CPAP RESmart BMC GII") no
    dice nada: sólo cuenta que haya dejado de nombrarlo.
    """
    model = _tokens(model_name)
    mapped = _tokens(mapped_description)
    live = _tokens(live_description)
    if not model or not mapped or not live:
        return False
    return model <= mapped and not model <= live
