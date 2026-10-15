"""Datos ficticios para recorrer la funcionalidad completa sin usar producción."""
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from rest_framework.test import APIClient

from service.models import User
from service import technical_interventions as technical
from service.views.helpers import q, exec_void, exec_returning
from service.views.quotes_views import _append_equipo_assoc_to_repuesto, _build_ingreso_equipo_assoc_label


PARTS = [
    ('FILTRO', 'Filtro de entrada para concentrador', 3200, 6400, 40, 'Concentrador de oxígeno'),
    ('HEPA', 'Filtro HEPA para concentrador', 8500, 17000, 30, 'Concentrador de oxígeno'),
    ('VALVULA', 'Válvula de salida para concentrador', 12500, 25000, 15, 'Concentrador de oxígeno'),
    ('TURBINA', 'Turbina para CPAP', 95000, 152000, 8, 'CPAP'),
    ('MANGUERA', 'Manguera para CPAP', 7500, 15000, 20, 'CPAP'),
    ('BATERIA', 'Batería para aspirador', 28000, 49000, 12, 'Aspirador'),
    ('FRASCO', 'Frasco recolector para aspirador', 11000, 22000, 18, 'Aspirador'),
    ('SENSOR', 'Sensor de saturación para monitor', 24000, 42000, 10, 'Monitor multiparamétrico'),
    ('CABLE', 'Cable de alimentación', 4500, 9000, 25, 'Monitor multiparamétrico'),
    ('NEBULIZADOR', 'Kit de nebulización', 3800, 7600, 30, 'Nebulizador'),
]

# Clave estable, caso, tipo de equipo, minutos, repuestos y etapa inicial.
SCENARIOS = [
    ('INGRESADO', 'Ingreso nuevo para diagnosticar', 'Concentrador de oxígeno', 0, [], 'nuevo'),
    ('SIN-ASIGNAR', 'Ingreso sin técnico asignado', 'Aspirador', 0, [], 'sin_asignar'),
    ('PREPARACION', 'Carga técnica en preparación', 'CPAP', 60, ['MANGUERA'], 'preparacion'),
    ('REVISION-30', 'Revisión de media hora: comprobar mínimo facturable', 'Nebulizador', 30, ['NEBULIZADOR'], 'revision'),
    ('REVISION-80', 'Revisión de una hora y veinte minutos', 'Concentrador de oxígeno', 80, ['FILTRO', 'HEPA'], 'revision'),
    ('REVISION-150', 'Revisión de dos horas y media', 'CPAP', 150, ['TURBINA', 'MANGUERA'], 'revision'),
    ('ALTA-REPUESTO', 'Solicitud de alta de repuesto pendiente', 'Monitor multiparamétrico', 90, ['CABLE'], 'alta_repuesto'),
    ('ALTA-TRABAJO', 'Solicitud de alta de trabajo pendiente', 'Aspirador', 75, ['FRASCO'], 'alta_trabajo'),
    ('SIN-FALLA', 'Sin falla: diagnóstico y limpieza', 'Concentrador de oxígeno', 0, [], 'sin_falla'),
    ('BONIFICADO', 'Sin falla con diagnóstico y limpieza bonificados', 'Nebulizador', 0, [], 'bonificado'),
    ('SIN-TARIFA', 'Revisión bloqueada por tarifa faltante', 'Equipo de demostración sin tarifa', 80, [], 'revision'),
    ('EMITIDO', 'Presupuesto emitido para aprobar', 'Monitor multiparamétrico', 120, ['SENSOR', 'CABLE'], 'emitido'),
    ('EJECUCION', 'Presupuesto aprobado: registrar ejecución y tiempo real', 'Aspirador', 90, ['BATERIA'], 'aprobado'),
    ('REPARADO', 'Reparación terminada con tiempo real registrado', 'CPAP', 80, ['MANGUERA'], 'reparado'),
    ('AMPLIACION', 'Adicional enviado sobre un presupuesto aprobado', 'Concentrador de oxígeno', 80, ['FILTRO'], 'ampliacion'),
]


class Command(BaseCommand):
    help = 'Carga usuarios, clientes, repuestos y escenarios ficticios en el desarrollo técnico aislado.'

    def add_arguments(self, parser):
        parser.add_argument('--password', required=True)

    def handle(self, *args, **options):
        if (connection.settings_dict['NAME'] != 'nexora_technical_dev'
                or settings.BEJERMAN_SYNC_ENABLED
                or settings.EMAIL_BACKEND != 'django.core.mail.backends.locmem.EmailBackend'):
            raise CommandError('Este comando solo admite la base técnica aislada, sin sincronización ni correo externo.')
        with transaction.atomic():
            exec_void('SELECT pg_advisory_xact_lock(71620261015)')
            people = {}
            for role, name, email in [
                ('jefe', 'Admin demo', 'presupuestos@tecnica.invalid'),
                ('tecnico', 'Técnico demo', 'tecnico@tecnica.invalid'),
            ]:
                person, _ = User.objects.get_or_create(email=email, defaults=dict(
                    nombre=name, rol=role, activo=True, hash_pw=make_password(options['password'])))
                exec_void('UPDATE users SET bejerman_credentials_prompt_dismissed=TRUE WHERE id=%s', [person.id])
                if person.nombre in {'Revisión de presupuestos', 'Técnico de demostración'}:
                    exec_void('UPDATE users SET nombre=%s WHERE id=%s', [name, person.id])
                people[role] = person
            self.tech, self.manager = people['tecnico'], people['jefe']
            self.client = APIClient()
            self.client.force_authenticate(self.manager)
            brand = q("SELECT id FROM marcas WHERE nombre='Demostración técnica' ORDER BY id LIMIT 1", one=True)
            self.brand = brand['id'] if brand else exec_returning("INSERT INTO marcas(nombre) VALUES ('Demostración técnica') RETURNING id")
            self.customers = []
            for name in ['Clínica del Parque · DEMO', 'Centro Respiratorio del Sur · DEMO', 'Consultorios del Centro · DEMO']:
                row = q('SELECT id FROM customers WHERE razon_social=%s ORDER BY id LIMIT 1', [name], one=True)
                self.customers.append(row['id'] if row else exec_returning('INSERT INTO customers(razon_social) VALUES (%s) RETURNING id', [name]))
            self.parts = {}
            for code, name, cost, price, stock, kind in PARTS:
                row = q('SELECT id FROM catalogo_repuestos WHERE codigo=%s', ['DEMO-'+code], one=True)
                self.parts[code] = row['id'] if row else exec_returning('''INSERT INTO catalogo_repuestos
                    (codigo,nombre,costo_neto,costo_moneda,precio_venta,multiplicador,stock_on_hand,stock_min,notas,estado)
                    VALUES (%s,%s,%s,'ARS',%s,%s,%s,2,%s,%s) RETURNING id''',
                    ['DEMO-'+code, name, cost, price, price/cost, stock,
                     'Artículo ficticio. Importes exclusivos de prueba.', kind])
            for kind, first, additional in [('Concentrador de oxígeno', 18000, 12000), ('CPAP', 22000, 15000),
                                            ('Aspirador', 16000, 10000), ('Monitor multiparamétrico', 24000, 16000),
                                            ('Nebulizador', 12000, 8000)]:
                exec_void('INSERT INTO catalogo_tipos_equipo(nombre) VALUES (%s) ON CONFLICT DO NOTHING', [kind])
                exec_void('''INSERT INTO technical_labor_rates(tipo_equipo,primera_hora,hora_adicional)
                    VALUES (LOWER(%s),%s,%s) ON CONFLICT DO NOTHING''', [kind, first, additional])
            exec_void('UPDATE technical_billing_config SET diagnostico_limpieza=6000 WHERE id=1 AND diagnostico_limpieza IS NULL')
            self.works = q('SELECT id FROM technical_work_catalog WHERE activo ORDER BY id LIMIT 2')
            if len(self.works) < 2:
                raise CommandError('Se necesitan los dos trabajos iniciales activos en el catálogo.')
            for index, scenario in enumerate(SCENARIOS):
                self.seed_scenario(index, *scenario)
        self.stdout.write(self.style.SUCCESS('Escenarios listos. Los importes son ficticios; no se reemplazaron tarifas ni órdenes existentes.'))

    def post(self, path, data=None):
        response = self.client.post(path, data or {}, format='json')
        if response.status_code not in (200, 201):
            raise CommandError(f'No se pudo preparar {path}: {response.status_code} {response.data}')
        return response.data

    def issue(self, ingreso):
        revision = technical.detail(ingreso)['scope_revision']
        self.post(f'/api/quotes/{ingreso}/emitir/', {'technical_revision': revision})

    def seed_scenario(self, index, code, label, kind, minutes, part_codes, stage):
        serial = 'DEMO-TEC-'+code
        existing = q('SELECT i.id FROM ingresos i JOIN devices d ON d.id=i.device_id WHERE d.numero_serie=%s', [serial], one=True)
        if existing:
            self.stdout.write(f"OS {existing['id']}: {label} (ya existente, se conserva).")
            return
        model = q('SELECT id FROM models WHERE marca_id=%s AND nombre=%s ORDER BY id LIMIT 1', [self.brand, kind+' demo'], one=True)
        model_id = model['id'] if model else exec_returning('INSERT INTO models(marca_id,nombre,tipo_equipo) VALUES (%s,%s,%s) RETURNING id', [self.brand, kind+' demo', kind])
        exec_void('INSERT INTO catalogo_tipos_equipo(nombre) VALUES (%s) ON CONFLICT DO NOTHING', [kind])
        device = exec_returning('''INSERT INTO devices(customer_id,marca_id,model_id,numero_serie,tipo_equipo)
            VALUES (%s,%s,%s,%s,%s) RETURNING id''', [self.customers[index % len(self.customers)], self.brand, model_id, serial, kind])
        ingreso = exec_returning('''INSERT INTO ingresos(device_id,motivo,asignado_a,fecha_ingreso,comentarios,informe_preliminar)
            VALUES (%s,'reparación',%s,now()-(%s * interval '1 day'),%s,%s) RETURNING id''',
            [device, None if stage=='sin_asignar' else self.tech.id, index % 10,
             'DEMO · '+label+' · Datos e importes ficticios de desarrollo.', label])
        if stage not in {'nuevo', 'sin_asignar'}:
            rows = [dict(tipo='trabajo', trabajo_id=w['id'], cantidad_prevista=1, cantidad_realizada=0,
                         observaciones='Realizar y verificar el funcionamiento antes de la entrega.') for w in self.works]
            rows += [dict(tipo='repuesto', repuesto_id=self.parts[c], cantidad_prevista=1, cantidad_realizada=0,
                          observaciones='Reemplazo previsto según el diagnóstico.') for c in part_codes]
            saved = technical.save(ingreso, self.tech, dict(revision=0,
                diagnostico=('No se detectaron fallas durante las pruebas de funcionamiento.' if stage in {'sin_falla','bonificado'}
                             else 'Se detectó una falla de funcionamiento. '+label+'.'),
                minutos_estimados=minutes, minutos_reales=0, sin_falla=stage in {'sin_falla','bonificado'},
                observaciones='Caso de demostración: '+label+'.', items=rows))
            equipment_label = _build_ingreso_equipo_assoc_label(ingreso)
            for part_code in part_codes:
                _append_equipo_assoc_to_repuesto(self.parts[part_code], equipment_label)
            if stage in {'alta_repuesto', 'alta_trabajo'}:
                self.post('/api/solicitudes-catalogo/', dict(ingreso_id=ingreso,
                    tipo='repuesto' if stage=='alta_repuesto' else 'trabajo',
                    nombre='Conector de sensor DEMO' if stage=='alta_repuesto' else 'Calibración y verificación DEMO',
                    cantidad=1, observaciones='Resolver vinculando un artículo existente o creando uno nuevo.'))
            elif stage != 'preparacion':
                technical.submit(ingreso, self.tech, saved['revision'])
                if stage=='bonificado':
                    technical.revise_labor(ingreso, self.manager, {'bonificado': True})
                if stage in {'emitido','aprobado','reparado','ampliacion'}:
                    self.issue(ingreso)
                if stage in {'aprobado','reparado','ampliacion'}:
                    self.post(f'/api/quotes/{ingreso}/aprobar/')
                if stage in {'aprobado','reparado'}:
                    live = technical.detail(ingreso)
                    for item in live['items']:
                        item['cantidad_realizada'] = item['cantidad_prevista'] if stage=='reparado' else 0
                    technical.save(ingreso, self.tech, dict(revision=live['revision'], items=live['items'],
                                   minutos_reales=100 if stage=='reparado' else 35))
                    if stage=='reparado':
                        self.post(f'/api/ingresos/{ingreso}/reparado/')
                if stage=='ampliacion':
                    live = technical.detail(ingreso)
                    live['items'].append(dict(tipo='repuesto', repuesto_id=self.parts['VALVULA'],
                        cantidad_prevista=1, cantidad_realizada=0, observaciones='Adicional detectado durante la reparación.'))
                    changed = technical.save(ingreso, self.tech, dict(revision=live['revision'], items=live['items'], minutos_estimados=120))
                    technical.submit(ingreso, self.tech, changed['revision'])
        self.stdout.write(f'OS {ingreso}: {label}.')
