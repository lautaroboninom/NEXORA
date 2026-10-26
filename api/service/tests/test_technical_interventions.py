from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase
from rest_framework.test import APIClient

from service.models import User
from service import technical_interventions as technical
from service.views.helpers import q, exec_returning, exec_void


class LaborCalculationTests(SimpleTestCase):
    def test_second_to_sixth_and_seventh_hour_boundaries(self):
        for minutes,expected in [(30,'100.00'),(60,'100.00'),(80,'120.00'),(360,'400.00'),
                                 (361,'400.50'),(390,'415.00'),(420,'430.00'),(480,'460.00')]:
            with self.subTest(minutes=minutes):
                self.assertEqual(technical.calculate_labor(minutes,100,60,30),Decimal(expected))
        self.assertEqual(technical.calculate_labor(420,100,60),Decimal('460.00'))

    def test_business_hourly_table_applies_the_increase_once(self):
        from service.management.commands.load_technical_hourly_rates import adjusted_rates
        rates={row[0]:row[1:4] for row in adjusted_rates()}
        self.assertEqual(rates['Aspiradores'],(Decimal('24909.21'),Decimal('10073.57'),Decimal('6959.92')))
        self.assertEqual(rates['Inogen'],(Decimal('80711.44'),Decimal('40355.72'),Decimal('32284.58')))

    def test_minimum_and_proportional_minutes(self):
        for minutes, expected in [(1,'100.00'),(30,'100.00'),(60,'100.00'),(80,'120.00'),(120,'160.00'),(150,'190.00')]:
            with self.subTest(minutes=minutes):
                self.assertEqual(technical.calculate_labor(minutes,100,60),Decimal(expected))

    def test_invalid_times_and_values(self):
        from rest_framework.exceptions import ValidationError
        for value in [-1,0,True,1.5,'NaN',525601]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                technical.calculate_labor(value,100,60)
        for value in ['NaN','Infinity',-1,'12.333']:
            with self.subTest(rate=value),self.assertRaises(ValidationError):
                technical.calculate_labor(60,value,60)


class TechnicalInterventionAPITests(TestCase):
    """Ejecutar con settings_technical_dev: clona únicamente la base aislada."""

    def setUp(self):
        self.manager = User.objects.create(nombre='Jefatura técnica',email='jefatura-tecnica-test@example.invalid',rol='jefe')
        self.tech = User.objects.create(nombre='Técnico de prueba',email='tecnico-carga-test@example.invalid',rol='tecnico')
        self.other = User.objects.create(nombre='Otro técnico',email='otro-carga-test@example.invalid',rol='tecnico')
        customer = exec_returning("INSERT INTO customers(razon_social) VALUES ('Cliente de prueba técnica') RETURNING id")
        device = exec_returning("INSERT INTO devices(customer_id,numero_serie,tipo_equipo) VALUES (%s,'TECNICA-PRUEBA','Concentrador') RETURNING id",[customer])
        self.ingreso = exec_returning("""INSERT INTO ingresos(device_id,motivo,asignado_a,fecha_ingreso)
                         VALUES (%s,(SELECT unnest(enum_range(NULL::motivo_ingreso)) LIMIT 1),%s,now()) RETURNING id""",[device,self.tech.id])
        self.part = exec_returning("""INSERT INTO catalogo_repuestos(codigo,nombre,costo_neto,costo_moneda,stock_on_hand,stock_min)
                         VALUES ('TEC-TEST','Filtro técnico de prueba',10,'ARS',20,0) RETURNING id""")
        self.work = q("SELECT id FROM technical_work_catalog ORDER BY id LIMIT 1",one=True)['id']
        exec_void("""INSERT INTO technical_labor_rates(tipo_equipo,primera_hora,hora_adicional) VALUES ('concentrador',100,60)
                     ON CONFLICT (tipo_equipo) DO UPDATE SET primera_hora=100,hora_adicional=60""")
        exec_void("UPDATE technical_billing_config SET diagnostico_limpieza=35 WHERE id=1")
        self.client = APIClient()
        self.client.force_authenticate(self.tech)
        self.base = f'/api/ingresos/{self.ingreso}/intervencion/'
        self.quote_base = f'/api/quotes/{self.ingreso}/'

    def save(self,**changes):
        data = self.client.get(self.base).json()
        payload = dict(revision=data['revision'],diagnostico='No concentra correctamente.',minutos_estimados=80,
                       minutos_reales=0,sin_falla=False,items=[dict(tipo='trabajo',trabajo_id=self.work,cantidad_prevista=1,cantidad_realizada=0),
                       dict(tipo='repuesto',repuesto_id=self.part,cantidad_prevista=1,cantidad_realizada=0)])
        if data['items']:
            payload['items']=data['items']
        payload.update(changes)
        response = self.client.patch(self.base,payload,format='json')
        self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def send(self):
        current = self.client.get(self.base).json()
        response = self.client.post(self.base+'enviar/',{'revision':current['revision']},format='json')
        self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def quote(self):
        self.client.force_authenticate(self.manager)
        response=self.client.get(self.quote_base)
        self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def issue(self):
        quote=self.quote()
        with patch('service.views.quotes_views.render_quote_pdf',return_value=('Presupuesto.pdf',b'%PDF-documento-original')):
            response=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def approve(self):
        self.client.force_authenticate(self.manager)
        with patch('service.views.quotes_views.notify_presupuesto_aprobado'),patch('service.views.quotes_views.notify_quote_mg_approved'),patch('service.views.quotes_views.notify_repair_ready_for_remito'):
            response=self.client.post(self.quote_base+'aprobar/',{},format='json')
        self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def test_single_load_submit_and_idempotent_draft(self):
        saved=self.save()
        self.assertEqual(saved['estado_revision'],'en_preparacion')
        self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                         dict(estado='ingresado',fecha_servicio=None))
        self.send()
        confirmed=q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True)
        self.assertEqual(confirmed['estado'],'diagnosticado')
        self.assertIsNotNone(confirmed['fecha_servicio'])
        self.send()
        self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),confirmed)
        quote=self.quote()
        self.assertEqual(len(quote['items']),3)
        self.assertEqual(Decimal(quote['mano_obra']),Decimal('120.00'))
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],20)

    def test_incomplete_submission_keeps_diagnosis_in_preparation(self):
        for changes in [dict(diagnostico=''),dict(items=[]),dict(minutos_estimados=0)]:
            with self.subTest(changes=changes):
                saved=self.save(**changes)
                response=self.client.post(self.base+'enviar/',{'revision':saved['revision']},format='json')
                self.assertEqual(response.status_code,400,response.content)
                self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                                 dict(estado='ingresado',fecha_servicio=None))
                self.assertIsNone(self.client.get(self.base).json()['submitted_revision'])

    def test_failed_draft_generation_does_not_complete_diagnosis(self):
        from rest_framework.exceptions import ValidationError
        saved=self.save()
        with patch('service.technical_interventions.sync_draft',side_effect=ValidationError('No se pudo preparar el borrador.')):
            response=self.client.post(self.base+'enviar/',{'revision':saved['revision']},format='json')
        self.assertEqual(response.status_code,400,response.content)
        self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                         dict(estado='ingresado',fecha_servicio=None))
        self.assertIsNone(self.client.get(self.base).json()['submitted_revision'])

    def test_general_sheet_fields_do_not_bypass_explicit_submission(self):
        endpoint=f'/api/ingresos/{self.ingreso}/'
        with patch('service.views.ingresos_views.sync_plan_from_ingreso_fecha_servicio',return_value={'status':'skipped'}):
            response=self.client.patch(endpoint,{'descripcion_problema':'Diagnóstico en preparación.'},format='json')
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                             dict(estado='ingresado',fecha_servicio=None))
            response=self.client.patch(endpoint,{'fecha_servicio':'2026-10-25T12:00:00Z'},format='json')
            self.assertEqual(response.status_code,200,response.content)
            before=q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True)
            self.assertEqual(before['estado'],'ingresado')
            self.save()
            response=self.client.patch(endpoint,{'descripcion_problema':'Diagnóstico corregido, todavía sin enviar.'},format='json')
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),before)
        self.send()
        self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                         dict(estado='diagnosticado',fecha_servicio=before['fecha_servicio']))

    def test_legacy_sheet_without_technical_schema_keeps_its_transition(self):
        with patch('service.technical_interventions.available',return_value=False), \
                patch('service.views.ingresos_views.sync_plan_from_ingreso_fecha_servicio',return_value={'status':'skipped'}):
            response=self.client.patch(f'/api/ingresos/{self.ingreso}/',{'descripcion_problema':'Diagnóstico por el circuito anterior.'},format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(q('SELECT estado::text FROM ingresos WHERE id=%s',[self.ingreso],one=True)['estado'],'diagnosticado')

    def test_submission_preserves_existing_service_date_and_advanced_states(self):
        exec_void("UPDATE ingresos SET fecha_servicio=now()-interval '3 days' WHERE id=%s",[self.ingreso])
        date=q('SELECT fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True)['fecha_servicio']
        self.save();self.send()
        self.assertEqual(q('SELECT fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True)['fecha_servicio'],date)
        for state in ['presupuestado','reparar','reparado','controlado_sin_defecto','no_se_repara']:
            with self.subTest(state=state):
                exec_void('UPDATE ingresos SET estado=%s WHERE id=%s',[state,self.ingreso])
                self.save(observaciones='Actualización técnica en estado '+state+'.');self.send()
                self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                                 dict(estado=state,fecha_servicio=date))

    def configure_fixed(self, amount='35000.00'):
        self.client.force_authenticate(self.manager)
        response=self.client.put('/api/tarifas-mano-obra/',{'trabajo_id':self.work,'precio_final':amount},format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.client.force_authenticate(self.tech)

    def test_complete_fixed_price_is_charged_once_without_hourly_or_part_extras(self):
        self.configure_fixed();self.save(minutos_estimados=180);self.send();self.send()
        quote=self.quote()
        self.assertEqual(Decimal(quote['subtotal']),35000)
        self.assertEqual(Decimal(quote['mano_obra']),0)
        self.assertEqual(Decimal(quote['tot_repuestos']),0)
        part=next(row for row in quote['items'] if row['tipo']=='repuesto')
        self.assertTrue(part['incluido_precio_final'])
        self.assertEqual(Decimal(part['costo_u_neto']),10)
        self.assertEqual(quote['technical']['labor_snapshot']['modo_cobro'],'precio_final')
        for payload in [{'precio_u':999},{'costo_u_neto':99}]:
            self.assertEqual(self.client.patch(self.quote_base+f"items/{part['id']}/",payload,format='json').status_code,400)
        self.issue();self.approve();self.approve()
        self.assertEqual(q('SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s',[self.part],one=True)['stock_on_hand'],19)

    def test_fixed_price_review_restores_separate_prices_when_returning_to_hours(self):
        self.save();self.send();quote=self.quote()
        part=next(row for row in quote['items'] if row['tipo']=='repuesto')
        self.assertEqual(self.client.patch(self.quote_base+f"items/{part['id']}/",{'precio_u':99},format='json').status_code,200)
        self.configure_fixed()
        self.client.force_authenticate(self.manager)
        result=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'precio_final_trabajo_id':self.work,'importe_precio_final':'34000.00'},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(Decimal(result.json()['subtotal']),34000)
        result=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'precio_final_trabajo_id':None,'minutos_presupuestados':80},format='json')
        self.assertEqual(result.status_code,200,result.content)
        quote=result.json()
        self.assertEqual(Decimal(quote['subtotal']),219)
        self.assertFalse(next(row for row in quote['items'] if row['tipo']=='repuesto')['incluido_precio_final'])
        self.assertFalse(q('SELECT id FROM quote_items WHERE quote_id=%s AND technical_final_price',[quote['quote_id']],one=True))

    def test_fixed_prices_and_configuration_are_never_exposed_to_technicians(self):
        self.configure_fixed()
        works=self.client.get('/api/catalogo-trabajos/?todos=1').json()
        self.assertTrue(all(set(row)=={'id','nombre','activo'} for row in works))
        self.assertEqual(self.client.get('/api/tarifas-mano-obra/').status_code,403)
        self.assertEqual(self.client.put('/api/tarifas-mano-obra/',{'trabajo_id':self.work,'precio_final':1},format='json').status_code,403)
        data=self.save();self.send()
        self.assertNotIn('precio_final',str(self.client.get(self.base).json()))
        self.assertEqual(self.client.patch(self.base,{'revision':data['revision'],'precio_final':1},format='json').status_code,400)

    def test_fixed_price_works_without_hourly_tariff_and_is_preserved_after_issue(self):
        self.configure_fixed()
        exec_void("DELETE FROM technical_labor_rates WHERE tipo_equipo='concentrador'")
        self.save();self.send();issued=self.issue()
        self.configure_fixed('40000.00')
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json()
        for item in data['items']:item['cantidad_realizada']=item['cantidad_prevista']
        self.save(items=data['items'],minutos_reales=180)
        unchanged=self.quote()
        self.assertEqual(unchanged['subtotal'],issued['subtotal'])
        self.assertFalse(unchanged['technical']['requiere_actualizacion'])

    def test_fixed_price_extension_preserves_pdf_and_applies_only_stock_difference(self):
        self.configure_fixed();self.save();self.send();original=self.issue();self.approve()
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json();data['items'][1]['cantidad_prevista']=3
        self.save(items=data['items']);self.send()
        self.client.force_authenticate(self.manager)
        version=self.client.post(self.quote_base+'versiones/',{},format='json')
        self.assertEqual(version.status_code,200,version.content)
        self.assertEqual(Decimal(version.json()['subtotal']),35000)
        self.issue();self.approve();self.approve()
        self.assertEqual(q('SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s',[self.part],one=True)['stock_on_hand'],17)
        document=self.client.get(self.quote_base+f"pdf/?quote_id={original['quote_id']}")
        self.assertEqual(document.content,b'%PDF-documento-original')

    def test_multiple_fixed_jobs_require_explicit_review_of_the_whole_repair(self):
        self.configure_fixed()
        second=exec_returning("INSERT INTO technical_work_catalog(nombre,precio_final) VALUES ('Otro trabajo completo',140000) RETURNING id")
        saved=self.save()
        self.save(items=[*saved['items'],dict(tipo='trabajo',trabajo_id=second,cantidad_prevista=1,cantidad_realizada=0)])
        self.send();quote=self.quote()
        self.assertTrue(quote['technical']['labor_snapshot']['requiere_seleccion_precio_final'])
        result=self.client.patch(self.quote_base+'mano-obra-tecnica/',{},format='json')
        self.assertTrue(result.json()['technical']['labor_snapshot']['requiere_seleccion_precio_final'])
        result=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(result.status_code,400)
        result=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'precio_final_trabajo_id':second},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(Decimal(result.json()['subtotal']),140000)
        self.issue()

    def test_full_price_pdf_explains_that_parts_and_labor_are_included(self):
        import fitz
        self.configure_fixed();self.save();self.send();quote=self.quote()
        response=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(response.status_code,200,response.content)
        pdf=self.client.get(self.quote_base+'pdf/')
        with fitz.open(stream=pdf.content,filetype='pdf') as document:
            text='\n'.join(page.get_text() for page in document)
        self.assertIn('mano de obra y repuestos incluidos',text)

    def test_configured_fixed_prices_keep_confirmed_amounts(self):
        from service.management.commands.load_technical_fixed_prices import FIXED_PRICES
        self.assertEqual([Decimal(amount) for _,amount in FIXED_PRICES],list(map(Decimal,['35000','30000','40000','140000'])))

    def test_rate_administration_and_brand_override_are_frozen_at_issue(self):
        self.client.force_authenticate(self.manager)
        response=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Inogen',marca='Inogen',
            primera_hora=500,hora_adicional=250,hora_desde_septima=100),format='json')
        self.assertEqual(response.status_code,200,response.content)
        duplicate=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Otra referencia',marca=' inogen ',
            primera_hora=500,hora_adicional=250,hora_desde_septima=100),format='json')
        self.assertEqual(duplicate.status_code,400,duplicate.content)
        existing=q("SELECT id FROM marcas WHERE LOWER(nombre)='inogen' ORDER BY id LIMIT 1",one=True)
        brand=existing['id'] if existing else exec_returning("INSERT INTO marcas(nombre) VALUES ('Inogen') RETURNING id")
        exec_void('UPDATE devices SET marca_id=%s WHERE id=(SELECT device_id FROM ingresos WHERE id=%s)',[brand,self.ingreso])
        self.client.force_authenticate(self.tech)
        self.save(minutos_estimados=420);self.send()
        issued=self.issue()
        self.assertEqual(Decimal(issued['mano_obra']),Decimal('1850.00'))
        self.assertEqual(issued['technical']['labor_snapshot']['marca_tarifa'],'Inogen')
        self.assertEqual(Decimal(issued['technical']['labor_snapshot']['hora_desde_septima']),100)
        updated=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Inogen',marca='Inogen',
            primera_hora=900,hora_adicional=500,hora_desde_septima=250),format='json')
        self.assertEqual(updated.status_code,200,updated.content)
        self.assertEqual(self.quote()['mano_obra'],issued['mano_obra'])

    def test_rate_api_keeps_legacy_additional_rate_when_third_band_is_omitted(self):
        self.client.force_authenticate(self.manager)
        response=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Concentrador',
            primera_hora=100,hora_adicional=60,hora_desde_septima=30),format='json')
        self.assertEqual(response.status_code,200,response.content)
        response=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Concentrador',primera_hora=100,hora_adicional=60),format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.client.force_authenticate(self.tech)
        self.save(minutos_estimados=390);self.send()
        self.assertEqual(Decimal(self.quote()['mano_obra']),Decimal('415.00'))

    def test_single_labor_time_preserves_history_and_calculates_decimal_hours(self):
        self.save(minutos_estimados=90,minutos_reales=40);self.send()
        quote=self.quote()
        self.assertEqual(Decimal(quote['mano_obra']),Decimal('130.00'))
        self.assertEqual(next(row['descripcion'] for row in quote['items'] if row['tipo']=='mano_obra'),'Mano de obra (1,5 h)')
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json()
        saved=self.client.patch(self.base,{'revision':data['revision'],'minutos_estimados':120},format='json')
        self.assertEqual(saved.status_code,200,saved.content)
        self.assertEqual(saved.json()['minutos_reales'],40)
        self.send()
        self.assertEqual(Decimal(self.quote()['mano_obra']),Decimal('160.00'))

    def test_technical_responses_and_commercial_permissions(self):
        self.save();self.send()
        for suffix in ['', 'repuestos/?q=TEC']:
            response=self.client.get(self.base+suffix)
            self.assertEqual(response.status_code,200,response.content)
            text=response.content.decode('utf-8')
            for secret in ['costo','precio','multiplicador','subtotal','importe']:
                self.assertNotIn(secret,text)
        self.assertEqual(self.client.get(self.quote_base).status_code,403)
        self.assertEqual(self.client.get('/api/tarifas-mano-obra/').status_code,403)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.base).status_code,403)

    def test_stale_save_and_submission(self):
        data=self.save()
        response=self.client.patch(self.base,{'revision':data['revision']-1,'minutos_reales':50},format='json')
        self.assertEqual(response.status_code,409)
        response=self.client.post(self.base+'enviar/',{'revision':data['revision']-1},format='json')
        self.assertEqual(response.status_code,409)

    def test_technical_parts_include_configured_equipment_without_commercial_data(self):
        equipment = ['Longfian | JAY-5', 'Longfian | JAY-10 | Doble flujo']
        exec_void("UPDATE catalogo_repuestos SET estado=%s WHERE id=%s",['\n'.join(equipment),self.part])
        response=self.client.get(self.base+'repuestos/?q=TEC-TEST')
        self.assertEqual(response.status_code,200,response.content)
        part=next(row for row in response.json() if row['id']==self.part)
        self.assertEqual(set(part),{'id','codigo','nombre','equipos_asociados'})
        self.assertEqual(part['equipos_asociados'],equipment)
        exec_void("UPDATE catalogo_repuestos SET estado=NULL WHERE id=%s",[self.part])
        response=self.client.get(self.base+'repuestos/?q=TEC-TEST')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(next(row for row in response.json() if row['id']==self.part)['equipos_asociados'],[])

    def test_progress_keeps_scope_and_quoted_price(self):
        self.save();self.send();issued=self.issue()
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json()
        for item in data['items']:item['cantidad_realizada']=item['cantidad_prevista']
        result=self.save(items=data['items'],minutos_reales=150)
        self.assertEqual(result['scope_revision'],issued['technical']['technical_revision'])
        quote=self.quote()
        self.assertEqual(quote['mano_obra'],issued['mano_obra'])
        self.assertFalse(quote['technical']['requiere_actualizacion'])

    def test_changes_during_review_prevent_issue(self):
        self.save();self.send();quote=self.quote()
        self.client.force_authenticate(self.tech);self.save(diagnostico='Diagnóstico corregido.')
        self.client.force_authenticate(self.manager)
        response=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(response.status_code,409)

    def test_execution_of_previously_selected_inactive_articles(self):
        self.save();self.send();issued=self.issue()
        exec_void("UPDATE technical_work_catalog SET activo=FALSE WHERE id=%s",[self.work])
        exec_void("UPDATE catalogo_repuestos SET activo=FALSE WHERE id=%s",[self.part])
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json()
        for item in data['items']:item['cantidad_realizada']=item['cantidad_prevista']
        saved=self.save(items=data['items'],minutos_reales=100)
        self.assertEqual(saved['scope_revision'],issued['technical']['technical_revision'])
        extra=dict(tipo='trabajo',trabajo_id=self.work,cantidad_prevista=1,cantidad_realizada=0)
        result=self.client.patch(self.base,{'revision':saved['revision'],'items':[*saved['items'],extra]},format='json')
        self.assertEqual(result.status_code,400)

    def test_missing_rate_blocks_issue_until_review(self):
        exec_void("DELETE FROM technical_labor_rates WHERE tipo_equipo='concentrador'")
        self.save();self.send();quote=self.quote()
        self.assertTrue(quote['technical']['labor_snapshot']['tarifa_pendiente'])
        response=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(response.status_code,400)
        self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Concentrador',primera_hora=100,hora_adicional=60),format='json')
        response=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'minutos_presupuestados':80},format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.issue()

    def test_no_fault_global_fee_and_bonus(self):
        self.save(sin_falla=True,minutos_estimados=0);self.send()
        quote=self.quote();self.assertEqual(Decimal(quote['mano_obra']),35)
        response=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'bonificado':True},format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(Decimal(response.json()['mano_obra']),0)
        self.issue()

    def test_pending_request_requires_resolution(self):
        self.save()
        response=self.client.post('/api/solicitudes-catalogo/',dict(ingreso_id=self.ingreso,tipo='trabajo',nombre='Calibración solicitada'),format='json')
        self.assertEqual(response.status_code,201,response.content)
        data=self.client.get(self.base).json()
        self.assertEqual(data['estado_revision'],'alta_pendiente')
        self.assertEqual(self.client.post(self.base+'enviar/',{'revision':data['revision']},format='json').status_code,400)
        self.assertEqual(q('SELECT estado::text,fecha_servicio FROM ingresos WHERE id=%s',[self.ingreso],one=True),
                         dict(estado='ingresado',fecha_servicio=None))
        self.client.force_authenticate(self.manager)
        created=self.client.post('/api/catalogo-trabajos/',{'nombre':'Calibración solicitada'},format='json')
        result=self.client.patch(f"/api/solicitudes-catalogo/{response.json()['id']}/",{'estado':'aprobada','catalog_id':created.json()['id']},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.client.force_authenticate(self.tech);self.send()
        self.assertEqual(len(self.client.get(self.base).json()['items']),3)

    def test_approved_extension_consumes_only_difference_and_preserves_document(self):
        self.save();self.send();original=self.issue();self.approve();self.approve()
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],19)
        self.client.force_authenticate(self.tech)
        current=self.client.get(self.base).json()
        current['items'][1]['cantidad_prevista']=3
        self.save(items=current['items']);self.send()
        self.client.force_authenticate(self.manager)
        version=self.client.post(self.quote_base+'versiones/',{},format='json')
        self.assertEqual(version.status_code,200,version.content)
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],19)
        self.issue();self.approve();self.approve()
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],17)
        old=q("SELECT estado::text FROM quotes WHERE id=%s",[original['quote_id']],one=True)
        self.assertEqual(old['estado'],'sustituido')
        response=self.client.get(self.quote_base+f"pdf/?quote_id={original['quote_id']}")
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.content,b'%PDF-documento-original')
        self.assertEqual(q("SELECT COUNT(*) AS n FROM quotes WHERE ingreso_id=%s AND estado='aprobado'",[self.ingreso],one=True)['n'],1)

    def test_emitted_items_are_immutable(self):
        self.save();self.send();quote=self.issue()
        item=quote['items'][0]
        response=self.client.patch(self.quote_base+f"items/{item['id']}/",{'precio_u':999},format='json')
        self.assertEqual(response.status_code,400)

    def test_individual_permission_override(self):
        exec_void("INSERT INTO user_permission_overrides(user_id,permission_code,effect) VALUES (%s,'action.presupuesto.manage','allow')",[self.other.id])
        self.client.force_authenticate(self.other)
        response=self.client.put('/api/tarifas-mano-obra/',dict(tipo_equipo='Otro equipo',primera_hora=50,hora_adicional=20),format='json')
        self.assertEqual(response.status_code,200,response.content)

    def test_repeated_submission_preserves_reviewed_hours_and_part_price(self):
        self.save();self.send();quote=self.quote()
        part=next(row for row in quote['items'] if row['tipo']=='repuesto')
        result=self.client.patch(self.quote_base+f"items/{part['id']}/",{'precio_u':99},format='json')
        self.assertEqual(result.status_code,200,result.content)
        result=self.client.patch(self.quote_base+'mano-obra-tecnica/',{'minutos_presupuestados':120},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.client.force_authenticate(self.tech);self.send()
        quote=self.quote()
        self.assertEqual(Decimal(quote['mano_obra']),160)
        self.assertEqual(Decimal(next(row for row in quote['items'] if row['tipo']=='repuesto')['precio_u']),99)

    def test_scope_has_one_source_and_malformed_catalog_items_are_rejected(self):
        data=self.save();self.send();quote=self.quote()
        part=next(row for row in quote['items'] if row['tipo']=='repuesto')
        self.assertEqual(self.client.patch(self.quote_base+f"items/{part['id']}/",{'qty':5},format='json').status_code,400)
        self.assertEqual(self.client.post(self.quote_base+'items/',{'tipo':'repuesto','qty':1,'descripcion':'Duplicado','precio_u':20},format='json').status_code,400)
        self.client.force_authenticate(self.tech)
        response=self.client.patch(self.base,{'revision':data['revision'],'items':[{'tipo':'repuesto','repuesto_id':'inválido'}]},format='json')
        self.assertEqual(response.status_code,400)

    def test_history_preserves_legacy_text_and_hides_commercial_changes(self):
        exec_void("UPDATE ingresos SET trabajos_realizados='Texto histórico conservado.' WHERE id=%s",[self.ingreso])
        data=self.save()
        self.assertEqual(data['trabajos_historicos'],'Texto histórico conservado.')
        self.send();self.issue()
        self.client.force_authenticate(self.tech)
        response=self.client.get(f'/api/ingresos/{self.ingreso}/historial/')
        self.assertEqual(response.status_code,200,response.content)
        for row in response.json():
            self.assertNotIn(row['table_name'],['quotes','quote_items','technical_labor_rates','technical_billing_config'])

    def test_portal_extension_uses_same_stock_delta(self):
        from service.views.portal_integration_views import _approve_budget_from_portal
        self.save();self.send();self.issue();self.approve()
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json();data['items'][1]['cantidad_prevista']=3
        self.save(items=data['items']);self.send()
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(self.quote_base+'versiones/',{},format='json').status_code,200)
        issued=self.issue()
        with patch('service.views.portal_integration_views._save_budget_pdf_copy'):
            self.assertTrue(_approve_budget_from_portal({'id':self.ingreso,'quote_id':issued['quote_id']})[0])
            self.assertFalse(_approve_budget_from_portal({'id':self.ingreso,'quote_id':issued['quote_id']})[0])
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],17)

    def test_real_pdf_renders_structured_diagnosis_and_work(self):
        import fitz
        self.save();self.send();quote=self.quote()
        result=self.client.post(self.quote_base+'emitir/',{'technical_revision':quote['technical']['technical_revision']},format='json')
        self.assertEqual(result.status_code,200,result.content)
        response=self.client.get(self.quote_base+'pdf/')
        self.assertEqual(response.status_code,200,response.content)
        with fitz.open(stream=response.content,filetype='pdf') as document:
            text='\n'.join(page.get_text() for page in document)
        self.assertIn('No concentra correctamente.',text)
        self.assertIn('Filtro técnico de prueba',text)

    def test_complete_repair_keeps_real_time(self):
        self.save();self.send();self.issue();self.approve()
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json()
        for item in data['items']:item['cantidad_realizada']=item['cantidad_prevista']
        self.save(items=data['items'],minutos_reales=110)
        response=self.client.post(f'/api/ingresos/{self.ingreso}/reparado/',{},format='json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(q("SELECT estado::text FROM ingresos WHERE id=%s",[self.ingreso],one=True)['estado'],'reparado')
        self.assertEqual(self.client.get(self.base).json()['minutos_reales'],110)

    def test_technician_must_request_catalog_creation(self):
        result=self.client.post('/api/repuestos/',{'subrubro_codigo':'1208','nombre':'Alta sin revisión'},format='json')
        self.assertEqual(result.status_code,403)
        result=self.client.post('/api/catalogo-trabajos/',{'nombre':'Alta sin revisión'},format='json')
        self.assertEqual(result.status_code,403)

    def test_legacy_budget_can_create_an_extension_without_duplicate_stock(self):
        initial=self.quote()
        exec_void("INSERT INTO quote_items(quote_id,tipo,repuesto_id,descripcion,qty,precio_u) VALUES (%s,'repuesto',%s,'Filtro anterior',1,20)",[initial['quote_id'],self.part])
        with patch('service.views.quotes_views.render_quote_pdf',return_value=('Anterior.pdf',b'%PDF-anterior')):
            issued=self.client.post(self.quote_base+'emitir/',{},format='json')
        self.assertEqual(issued.status_code,200,issued.content)
        self.approve()
        response=self.client.post(self.quote_base+'versiones/',{},format='json')
        self.assertEqual(response.status_code,200,response.content)
        draft=response.json()
        self.assertTrue(draft['is_editable'])
        self.assertIsNone(draft['technical'])
        self.assertEqual(draft['version_num'],2)
        result=self.client.patch(self.quote_base+f"items/{draft['items'][0]['id']}/",{'qty':3},format='json')
        self.assertEqual(result.status_code,200,result.content)
        with patch('service.views.quotes_views.render_quote_pdf',return_value=('Ampliado.pdf',b'%PDF-ampliado')):
            self.assertEqual(self.client.post(self.quote_base+'emitir/',{},format='json').status_code,200)
        cancelled=self.client.post(self.quote_base+'anular/',{},format='json')
        self.assertEqual(cancelled.status_code,200,cancelled.content)
        self.assertEqual(cancelled.json()['version_num'],3)
        with patch('service.views.quotes_views.render_quote_pdf',return_value=('Ampliado.pdf',b'%PDF-ampliado')):
            self.assertEqual(self.client.post(self.quote_base+'emitir/',{},format='json').status_code,200)
        self.approve();self.approve()
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],17)

    def test_existing_no_fault_action_selects_the_global_diagnostic_fee(self):
        self.save()
        result=self.client.post(f'/api/ingresos/{self.ingreso}/controlado-sin-defecto/',{},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.assertTrue(self.client.get(self.base).json()['sin_falla'])
        self.send()
        self.assertEqual(Decimal(self.quote()['mano_obra']),35)

    def test_diagnosis_deny_is_effective_even_with_budget_permission(self):
        exec_void("INSERT INTO user_permission_overrides(user_id,permission_code,effect) VALUES (%s,'action.presupuesto.manage','allow'),(%s,'action.ingreso.edit_diagnosis','deny')",[self.tech.id,self.tech.id])
        result=self.client.patch(self.base,{'revision':0,'diagnostico':'No permitido'},format='json')
        self.assertEqual(result.status_code,403)

    def test_cancelling_unapproved_extension_keeps_consumed_baseline(self):
        self.save();self.send();self.issue();self.approve()
        self.client.force_authenticate(self.tech)
        data=self.client.get(self.base).json();data['items'][1]['cantidad_prevista']=3
        self.save(items=data['items']);self.send()
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(self.quote_base+'versiones/',{},format='json').status_code,200)
        self.issue()
        result=self.client.post(self.quote_base+'anular/',{},format='json')
        self.assertEqual(result.status_code,200,result.content)
        self.issue();self.approve()
        self.assertEqual(q("SELECT stock_on_hand FROM catalogo_repuestos WHERE id=%s",[self.part],one=True)['stock_on_hand'],17)
