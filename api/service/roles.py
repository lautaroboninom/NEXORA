# roles.py
# Definiciones de roles visibles en la API/Front.

ROLE_CHOICES = [
    ("tecnico", "Técnico"),
    ("admin", "Administración"),
    ("supervisor", "Supervisor"),
    ("ventas", "Ventas"),
    ("jefe", "Jefe"),
    ("jefe_veedor", "Jefe veedor"),
    # Reemplazo temporal de jefatura. No es un puesto: se asigna con
    # `manage.py suplencia` y se saca al volver el titular.
    ("jefe_suplente", "Jefe suplente"),
    ("recepcion", "Recepción"),
    ("cobranzas", "Cobranzas"),
    ("logistica", "Logística"),
]

ROLE_KEYS = [r for r, _ in ROLE_CHOICES]
