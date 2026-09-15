"""Runner de integración con el esquema operativo, solo en bases descartables.

Uso: manage.py test --testrunner service.tests.runner.NexoraTestRunner
Requiere sql/schema.sql junto al directorio api de la copia de desarrollo.
"""

from django.test.runner import DiscoverRunner

from service.tests.schema_helpers import apply_application_schema


class NexoraTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        databases = super().setup_databases(**kwargs)
        if "default" in kwargs.get("aliases", {"default"}):
            apply_application_schema()
        return databases
