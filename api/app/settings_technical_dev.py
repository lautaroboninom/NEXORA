"""Entorno aislado de la rama de carga técnica; nunca usa la configuración productiva."""
from .settings import *  # noqa: F403

if DATABASES['default']['NAME'] != 'nexora_technical_dev':  # noqa: F405
    raise RuntimeError('El entorno técnico solo admite la base nexora_technical_dev.')

# Las tablas de negocio son SQL, no modelos gestionados por Django.
# El runner clona exclusivamente la base aislada para probar su esquema real.
DATABASES['default']['TEST'] = {'TEMPLATE': 'nexora_technical_dev'}  # noqa: F405
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
BEJERMAN_SYNC_ENABLED = False
BEJERMAN_PURCHASE_EMISSION_ENABLED = False
