# roles.py
# Definiciones de roles visibles en la API/Front.

ROLE_CHOICES = [
    ("tecnico", "Técnico"),
    ("admin", "Administración"),
    ("ventas", "Ventas"),
    ("jefe", "Jefe"),
    ("jefe_veedor", "Jefe veedor"),
    ("recepcion", "Recepción"),
    ("cobranzas", "Cobranzas"),
]

ROLE_KEYS = [r for r, _ in ROLE_CHOICES]
