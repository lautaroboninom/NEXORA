"""Precios completos confirmados por el negocio; no reciben el aumento de horas."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service import technical_interventions as technical
from service.models import User
from service.views.helpers import q, exec_void


FIXED_PRICES = [
    ('Polywatch: cambio de cánula nasal', '35000.00'),
    ('Polywatch: cambio de pila', '30000.00'),
    ('Polywatch: cambio de pila y actualización de software', '40000.00'),
    ('Reparación de compresores en general', '140000.00'),
]


class Command(BaseCommand):
    help = 'Configura precios finales completos de Polywatch y compresores, exclusivamente en desarrollo técnico.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        for name, amount in FIXED_PRICES:
            self.stdout.write(f'{name}: $ {amount} (mano de obra y repuestos incluidos)')
        if not options['apply']:
            self.stdout.write('Vista previa. Usá --apply para cargar los precios en desarrollo.')
            return
        if connection.settings_dict['NAME'] != 'nexora_technical_dev':
            raise CommandError('La carga solo está autorizada en nexora_technical_dev.')
        manager = User.objects.filter(email='presupuestos@tecnica.invalid').first()
        if manager is None:
            raise CommandError('Creá primero el usuario de demostración para registrar la auditoría.')
        with transaction.atomic():
            technical._audit(manager,0)
            for name, amount in FIXED_PRICES:
                existing = q('SELECT id FROM technical_work_catalog WHERE LOWER(BTRIM(nombre))=LOWER(%s)',[name],one=True)
                if existing:
                    exec_void('UPDATE technical_work_catalog SET precio_final=%s WHERE id=%s',[amount,existing['id']])
                else:
                    exec_void('INSERT INTO technical_work_catalog(nombre,precio_final) VALUES (%s,%s)',[name,amount])
        self.stdout.write(self.style.SUCCESS('Precios finales configurados. No se modificaron órdenes, repuestos ni documentos emitidos.'))
