"""Formatter que sí imprime el `extra` de cada log.

Todo el circuito de Bejerman viene midiendo tiempos y mandándolos por `extra`
(`duration_ms`, `document_key`, `company_key`, `pdf_state`...). Con un formato
de `%(message)s` pelado esos datos se descartaban justo cuando hacían falta:
para explicar por qué una emisión tardó. Acá se agregan al final de la línea
como `clave=valor`, ordenados y en una sola línea, así siguen siendo grepeables.
"""

import logging


# Lo que trae cualquier LogRecord por su cuenta; el resto lo puso el llamador.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))
_RESERVED |= {"asctime", "message", "taskName"}

_MAX_VALUE_LEN = 300


def _render(value) -> str:
    # Un log por línea: un payload con saltos rompería cualquier grep posterior.
    text = " ".join(str(value).split())
    if len(text) > _MAX_VALUE_LEN:
        text = text[: _MAX_VALUE_LEN - 1] + "…"
    return text if text and " " not in text else f'"{text}"'


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if not extras:
            return base
        pairs = " ".join(f"{key}={_render(value)}" for key, value in sorted(extras.items()))
        return f"{base} | {pairs}"
