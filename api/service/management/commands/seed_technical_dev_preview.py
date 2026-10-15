from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service.models import User
from service import technical_interventions as technical
from service.views.helpers import q, exec_void, exec_returning


class Command(BaseCommand):
    help = "Crea usuarios y una orden ficticia exclusivamente en el desarrollo de carga técnica."

    def add_arguments(self,parser):
        parser.add_argument('--password',required=True)

    def handle(self,*args,**options):
        if connection.settings_dict['NAME'] != 'nexora_technical_dev' or settings.BEJERMAN_SYNC_ENABLED:
            raise CommandError('Este comando solo admite el entorno técnico aislado, con sincronización desactivada.')
        with transaction.atomic():
            people = {}
            for role,name,email in [('jefe','Revisión de presupuestos','presupuestos@tecnica.invalid'),('tecnico','Técnico de demostración','tecnico@tecnica.invalid')]:
                person,created = User.objects.get_or_create(email=email,defaults=dict(nombre=name,rol=role,activo=True,hash_pw=make_password(options['password'])))
                exec_void("UPDATE users SET bejerman_credentials_prompt_dismissed=TRUE WHERE id=%s", [person.id])
                people[role]=person
            existing=q("SELECT i.id FROM ingresos i JOIN devices d ON d.id=i.device_id WHERE d.numero_serie='DEMO-CARGA-TECNICA'",one=True)
            if existing:
                self.stdout.write(f"La orden ficticia ya existe: OS {existing['id']}.")
                return
            customer=exec_returning("INSERT INTO customers(razon_social) VALUES ('Demostración de carga técnica · desarrollo') RETURNING id")
            brand=exec_returning("INSERT INTO marcas(nombre) VALUES ('Demostración técnica') RETURNING id")
            model=exec_returning("INSERT INTO models(marca_id,nombre,tipo_equipo) VALUES (%s,'Equipo de demostración','Concentrador de oxígeno') RETURNING id",[brand])
            exec_void("INSERT INTO catalogo_tipos_equipo(nombre) VALUES ('Concentrador de oxígeno') ON CONFLICT DO NOTHING")
            device=exec_returning("INSERT INTO devices(customer_id,marca_id,model_id,numero_serie,tipo_equipo) VALUES (%s,%s,%s,'DEMO-CARGA-TECNICA','Concentrador de oxígeno') RETURNING id",[customer,brand,model])
            ingreso=exec_returning("""INSERT INTO ingresos(device_id,motivo,asignado_a,fecha_ingreso) VALUES
                       (%s,(SELECT unnest(enum_range(NULL::motivo_ingreso)) LIMIT 1),%s,now()) RETURNING id""",[device,people['tecnico'].id])
            repuesto=exec_returning("INSERT INTO catalogo_repuestos(codigo,nombre,stock_on_hand,costo_moneda) VALUES ('DEMO-TEC-001','Filtro de demostración',10,'ARS') RETURNING id")
            work=q("SELECT id FROM technical_work_catalog WHERE activo ORDER BY id LIMIT 1",one=True)['id']
            technical.save(ingreso,people['tecnico'],dict(revision=0,diagnostico='El equipo requiere revisión y cambio del filtro. Registro ficticio de desarrollo.',
                minutos_estimados=80,minutos_reales=0,items=[dict(tipo='trabajo',trabajo_id=work,cantidad_prevista=1),dict(tipo='repuesto',repuesto_id=repuesto,cantidad_prevista=1)]))
        self.stdout.write(self.style.SUCCESS(f"Orden ficticia OS {ingreso}. Usuarios: presupuestos@tecnica.invalid y tecnico@tecnica.invalid. Las tarifas quedan pendientes de configurar."))
