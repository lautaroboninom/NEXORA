"""Tabla aportada por el negocio: julio de 2024, con el ajuste confirmado del 34 %."""
import json
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service import technical_interventions as technical
from service.models import User
from service.views.helpers import q, exec_void


RATES = [
    ('Aspiradores', '18588.96', '7517.59', '5193.97', ['Aspirador', 'Aspirador a baterías']),
    ('Bombas (todas)', '22044.32', '8529.05', '7216.89', ['Bomba de alimentación', 'Bomba de alimentacion', 'Bomba de aspiración de drenaje', 'Bomba de aspiracion de drenaje', 'Bomba de infusión', 'Cabezales de bomba']),
    ('BPAP', '28976.91', '12191.73', '9005.26', ['BPAP', 'Auto BPAP', 'BPAP c/AVAPS', 'BPAP c/BIFLEX', 'BPAP c/frec.', 'BPAP c/humidificador']),
    ('Concentradores', '22962.83', '9005.26', '7619.83', ['Concentrador de oxígeno', 'Concentrador portátil de oxígeno']),
    ('CPAP', '22962.83', '9005.26', '7619.83', ['CPAP', 'Auto CPAP', 'CPAP c/CFLEX', 'CPAP c/humidif']),
    ('Humidificadores', '22044.32', '8529.05', '7216.89', ['Calentador humidificador']),
    ('Incubadoras', '28976.91', '12191.73', '9005.26', ['Incubadora', 'Incubadora de transporte', 'Grupo motor de incubadora']),
    ('Monitores', '28976.91', '12191.73', '9005.26', ['Monitor auxiliar', 'Monitor de apnea', 'Monitor de presión no invasivo', 'Monitor multiparamétrico', 'Central de monitoreo']),
    ('Oxímetros', '28976.91', '12191.73', '9005.26', ['Oxímetro de pulso', 'Oxímetro de pulso c/curva']),
    ('Respiradores', '35748.55', '14302.69', '10722.93', ['Respirador']),
    ('Inogen', '60232.42', '30116.21', '24092.97', ['Inogen']),
]


def adjusted_rates():
    return [(name, *[(Decimal(value)*Decimal('1.34')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                    for value in (first, following, seventh)], aliases)
            for name, first, following, seventh, aliases in RATES]


class Command(BaseCommand):
    help = 'Vista previa o carga de la tabla de horas con aumento del 34 %, exclusivamente en desarrollo técnico.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--refresh-drafts', action='store_true')

    def handle(self, *args, **options):
        rows = adjusted_rates()
        self.stdout.write(json.dumps([dict(grupo=name,primera_hora=first,hora_adicional=following,
            hora_desde_septima=seventh,tipos=aliases,marca='Inogen' if name=='Inogen' else None)
            for name,first,following,seventh,aliases in rows], ensure_ascii=False, default=str))
        if not options['apply']:
            self.stdout.write('Vista previa. Usá --apply para cargar estos valores en desarrollo.')
            return
        if connection.settings_dict['NAME'] != 'nexora_technical_dev':
            raise CommandError('La carga de esta tabla solo está autorizada en nexora_technical_dev.')
        manager = User.objects.filter(email='presupuestos@tecnica.invalid').first()
        if manager is None:
            raise CommandError('Creá primero el usuario de demostración para registrar la auditoría.')
        with transaction.atomic():
            technical._audit(manager, 0)
            # Retirar únicamente los valores conocidos del seed que no figuran
            # en la tabla del negocio; no confundirlos con tarifas confirmadas.
            configured = q('SELECT id FROM technical_labor_rates WHERE hora_desde_septima IS NOT NULL LIMIT 1',one=True)
            if not configured:
                exec_void('''DELETE FROM technical_labor_rates WHERE tipo_equipo='nebulizador'
                    AND primera_hora=12000 AND hora_adicional=8000 AND hora_desde_septima IS NULL AND marca IS NULL''')
                exec_void('UPDATE technical_billing_config SET diagnostico_limpieza=NULL WHERE id=1 AND diagnostico_limpieza=6000')
            for name, first, following, seventh, aliases in rows:
                for alias in aliases:
                    if name!='Inogen':
                        exec_void('INSERT INTO catalogo_tipos_equipo(nombre) VALUES (%s) ON CONFLICT DO NOTHING',[alias])
                    exec_void('''INSERT INTO technical_labor_rates(tipo_equipo,marca,primera_hora,hora_adicional,hora_desde_septima)
                        VALUES (LOWER(%s),%s,%s,%s,%s) ON CONFLICT (tipo_equipo) DO UPDATE
                        SET marca=EXCLUDED.marca,primera_hora=EXCLUDED.primera_hora,hora_adicional=EXCLUDED.hora_adicional,
                            hora_desde_septima=EXCLUDED.hora_desde_septima,updated_at=now()''',
                        [alias,'Inogen' if name=='Inogen' else None,first,following,seventh])
            refreshed = []
            if options['refresh_drafts']:
                pending = q('''SELECT q.ingreso_id FROM quotes q JOIN technical_interventions t ON t.ingreso_id=q.ingreso_id
                    JOIN ingresos i ON i.id=q.ingreso_id
                    WHERE q.estado='pendiente' AND q.technical_revision=t.scope_revision AND t.submitted_revision=t.scope_revision
                    AND i.estado NOT IN ('entregado','liberado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
                    AND q.id=(SELECT id FROM quotes WHERE ingreso_id=q.ingreso_id ORDER BY version_num DESC,id DESC LIMIT 1)''')
                for item in pending:
                    technical.revise_labor(item['ingreso_id'],manager,{})
                    refreshed.append(item['ingreso_id'])
        self.stdout.write(self.style.SUCCESS(f'Tarifas cargadas. Borradores actualizados: {refreshed}. Las versiones emitidas se conservaron.'))
        self.stdout.write('Los tipos sin tarifa bloquean la emisión. Los importes ajenos a la tabla de horas se gestionan en configuración.')
