"""Carga técnica única y cálculo comercial. Los DTO técnicos nunca contienen importes."""

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import connection, transaction
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from .permissions import user_has_permission
from .views.helpers import q, exec_void, exec_returning


class Conflict(APIException):
    status_code = 409
    default_detail = "La información cambió. Recargá la ficha antes de continuar."


def available():
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('technical_interventions') IS NOT NULL")
        return cur.fetchone()[0]


def decimal_value(value, label, *, positive=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result <= 0):
            raise ValueError
        if result > Decimal('99999999.99') or result != result.quantize(Decimal('.01')):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{label}: ingresá un número válido con hasta dos decimales.")


def minutes_value(value):
    if isinstance(value, bool):
        raise ValidationError("El tiempo debe expresarse en minutos enteros.")
    try:
        result = int(value)
        if str(result) != str(value) or not 0 <= result <= 525600:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValidationError("El tiempo debe expresarse en minutos enteros entre 0 y 525600.")


def catalog_identifier(value):
    if isinstance(value,bool):
        raise ValidationError("Identificador de catálogo inválido.")
    try:
        result = int(value)
        if result <= 0 or str(result) != str(value):
            raise ValueError
        return result
    except (ValueError,TypeError):
        raise ValidationError("Identificador de catálogo inválido.")


def calculate_labor(minutes, first_hour, additional_hour, seventh_hour=None):
    minutes = minutes_value(minutes)
    if minutes == 0:
        raise ValidationError("Ingresá el tiempo estimado antes de presupuestar.")
    first = decimal_value(first_hour, "Primera hora")
    additional = decimal_value(additional_hour, "Hora adicional")
    seventh = decimal_value(seventh_hour, "Hora desde la séptima") if seventh_hour is not None else additional
    return (first + Decimal(min(max(minutes - 60, 0), 300)) * additional / 60
            + Decimal(max(minutes - 360, 0)) * seventh / 60).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def can_manage(user):
    return user_has_permission(user, "action.presupuesto.manage")


def ingreso_data(ingreso_id, user=None, *, write=False, lock=False):
    row = q("""SELECT i.id, i.asignado_a, i.estado::text, i.resolucion, i.descripcion_problema,
               COALESCE(i.trabajos_realizados,'') AS trabajos_historicos,
               COALESCE(NULLIF(d.tipo_equipo,''), m.tipo_equipo,'') AS tipo_equipo, COALESCE(b.nombre,'') AS marca
          FROM ingresos i JOIN devices d ON d.id=i.device_id
          LEFT JOIN models m ON m.id=d.model_id
          LEFT JOIN marcas b ON b.id=COALESCE(d.marca_id,m.marca_id)
          WHERE i.id=%s""" + (" FOR UPDATE OF i" if lock else ""), [ingreso_id], one=True)
    if not row:
        raise NotFound("Orden de servicio inexistente.")
    if user is not None:
        manager = can_manage(user)
        tech = user_has_permission(user, "action.ingreso.edit_diagnosis")
        if not manager and not (tech and int(row['asignado_a'] or 0) == int(user.id)):
            raise PermissionDenied("Solo el técnico asignado o quien administra presupuestos puede acceder a la intervención.")
        if write and row['estado'] in {'entregado','liberado','baja','alquilado','vendido_pendiente_entrega','vendido_entregado'}:
            raise ValidationError("La orden está cerrada. No se puede modificar la intervención.")
    return row


def _ensure(ingreso_id):
    exec_void("INSERT INTO technical_interventions(ingreso_id) VALUES (%s) ON CONFLICT DO NOTHING", [ingreso_id])
    return q("SELECT * FROM technical_interventions WHERE ingreso_id=%s FOR UPDATE", [ingreso_id], one=True)


def _audit(user, ingreso_id):
    exec_void("SELECT set_config('app.user_id', %s, true)", [str(user.id)])
    exec_void("SELECT set_config('app.user_role', %s, true)", [str(user.rol)])
    exec_void("SELECT set_config('app.ingreso_id', %s, true)", [str(ingreso_id)])


def items(ingreso_id):
    return q("""SELECT t.id, t.tipo, t.trabajo_id, t.repuesto_id,
                 COALESCE(w.nombre,r.nombre) AS descripcion, r.codigo,
                 t.cantidad_prevista, t.cantidad_realizada, t.observaciones
          FROM technical_intervention_items t
          LEFT JOIN technical_work_catalog w ON w.id=t.trabajo_id
          LEFT JOIN catalogo_repuestos r ON r.id=t.repuesto_id
          WHERE t.ingreso_id=%s ORDER BY t.id""", [ingreso_id]) or []


def detail(ingreso_id, user=None):
    ingreso = ingreso_data(ingreso_id, user)
    intervention = q("""SELECT revision, scope_revision, submitted_revision, minutos_estimados, minutos_reales,
                     sin_falla, observaciones, submitted_at FROM technical_interventions WHERE ingreso_id=%s""", [ingreso_id], one=True)
    result = intervention or dict(revision=0, scope_revision=0, submitted_revision=None, minutos_estimados=0,
                                  minutos_reales=0, sin_falla=ingreso['estado']=='controlado_sin_defecto' or ingreso['resolucion']=='no_se_encontro_falla', observaciones='', submitted_at=None)
    result.update(ingreso_id=ingreso_id, diagnostico=ingreso['descripcion_problema'] or '',
                  trabajos_historicos=ingreso['trabajos_historicos'], tipo_equipo=ingreso['tipo_equipo'], items=items(ingreso_id))
    result['solicitudes'] = q("""SELECT id,tipo,nombre,cantidad,observaciones,estado,respuesta,created_at
             FROM technical_catalog_requests WHERE ingreso_id=%s ORDER BY id""", [ingreso_id]) or []
    pending = any(r['estado'] == 'pendiente' for r in result['solicitudes'])
    latest = q("SELECT estado::text FROM quotes WHERE ingreso_id=%s ORDER BY version_num DESC,id DESC LIMIT 1", [ingreso_id], one=True)
    ready = result['submitted_revision'] is not None and result['submitted_revision'] == result['scope_revision']
    result['estado_revision'] = ('alta_pendiente' if pending else 'listo_revision' if ready else
        'ampliacion_pendiente' if latest and latest['estado'] in {'presupuestado','emitido','aprobado'} and intervention else 'en_preparacion')
    return result


def _check_revision(intervention, expected):
    try:
        matches = int(expected) == intervention['revision']
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise Conflict()


def save(ingreso_id, user, data):
    if not user_has_permission(user, 'action.ingreso.edit_diagnosis'):
        raise PermissionDenied("No autorizado para modificar la intervención técnica.")
    allowed = {'revision','diagnostico','minutos_estimados','minutos_reales','sin_falla','observaciones','items'}
    if set(data) - allowed:
        raise ValidationError("La carga técnica solo admite campos técnicos.")
    with transaction.atomic():
        ingreso = ingreso_data(ingreso_id, user, write=True, lock=True)
        previous = q("SELECT * FROM technical_interventions WHERE ingreso_id=%s", [ingreso_id], one=True)
        if previous is None and data.get('revision') != 0:
            raise Conflict()
        intervention = _ensure(ingreso_id)
        _audit(user, ingreso_id)
        if previous:
            _check_revision(intervention, data.get('revision'))
        old_items = items(ingreso_id)
        new_items = old_items
        if 'items' in data:
            if not isinstance(data['items'], list) or len(data['items']) > 200:
                raise ValidationError("La lista admite hasta 200 artículos.")
            new_items = []
            old_by_id = {i['id']:i for i in old_items}
            seen = set()
            for item in data['items']:
                if not isinstance(item,dict):
                    raise ValidationError("Cada artículo debe ser un registro de la lista.")
                kind = item.get('tipo')
                key = 'trabajo_id' if kind == 'trabajo' else 'repuesto_id' if kind == 'repuesto' else None
                if not key:
                    raise ValidationError("Tipo de artículo inválido.")
                catalog_id = catalog_identifier(item.get(key))
                table = 'technical_work_catalog' if kind == 'trabajo' else 'catalogo_repuestos'
                item_id = item.get('id')
                if item_id is not None and (item_id not in old_by_id or item_id in seen):
                    raise ValidationError("Artículo ajeno a la orden o repetido.")
                retained = old_by_id.get(item_id)
                same_article = retained and retained['tipo'] == kind and retained[key] == catalog_id
                # Desactivar un artículo impide seleccionarlo nuevamente, pero permite
                # registrar la ejecución de uno que ya estaba incluido en la reparación.
                if not same_article and not q(f"SELECT id FROM {table} WHERE id=%s AND activo", [catalog_id], one=True):
                    raise ValidationError("El artículo debe existir y estar activo. Solicitá su alta si falta.")
                seen.add(item_id)
                new_items.append(dict(id=item_id,tipo=kind,trabajo_id=catalog_id if kind=='trabajo' else None,
                    repuesto_id=catalog_id if kind=='repuesto' else None,
                    cantidad_prevista=decimal_value(item.get('cantidad_prevista',1),'Cantidad prevista',positive=True),
                    cantidad_realizada=decimal_value(item.get('cantidad_realizada',0),'Cantidad realizada'),
                    observaciones=str(item.get('observaciones','')).strip()[:2000]))
        estimated = minutes_value(data.get('minutos_estimados',intervention['minutos_estimados']))
        actual = minutes_value(data.get('minutos_reales',intervention['minutos_reales']))
        no_fault = data.get('sin_falla',intervention['sin_falla'])
        if not isinstance(no_fault,bool):
            raise ValidationError("La indicación sin falla debe ser verdadera o falsa.")
        diagnosis = str(data.get('diagnostico',ingreso['descripcion_problema'] or '')).strip()
        notes = str(data.get('observaciones',intervention['observaciones'])).strip()[:4000]
        def scope(rows):
            # La ejecución hasta la cantidad prevista es avance; un excedente cambia el alcance.
            return [(r['tipo'],r['trabajo_id'],r['repuesto_id'],max(Decimal(str(r['cantidad_prevista'])),Decimal(str(r['cantidad_realizada']))),r['observaciones']) for r in rows]
        changed = (scope(old_items) != scope(new_items) or estimated != intervention['minutos_estimados']
                   or no_fault != intervention['sin_falla'] or notes != intervention['observaciones']
                   or diagnosis != (ingreso['descripcion_problema'] or ''))
        if 'items' in data:
            retained = [r['id'] for r in new_items if r['id'] is not None]
            exec_void("DELETE FROM technical_intervention_items WHERE ingreso_id=%s AND NOT (id=ANY(%s::int[]))", [ingreso_id,retained])
            for item in new_items:
                values = [item[k] for k in ('tipo','trabajo_id','repuesto_id','cantidad_prevista','cantidad_realizada','observaciones')]
                if item['id'] is None:
                    exec_void("""INSERT INTO technical_intervention_items(ingreso_id,tipo,trabajo_id,repuesto_id,cantidad_prevista,cantidad_realizada,observaciones)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)""", [ingreso_id,*values])
                else:
                    exec_void("""UPDATE technical_intervention_items SET tipo=%s,trabajo_id=%s,repuesto_id=%s,
                                cantidad_prevista=%s,cantidad_realizada=%s,observaciones=%s WHERE id=%s AND ingreso_id=%s""", [*values,item['id'],ingreso_id])
        exec_void("""UPDATE technical_interventions SET revision=revision+1,scope_revision=scope_revision+%s,
                   minutos_estimados=%s,minutos_reales=%s,sin_falla=%s,observaciones=%s,updated_at=now(),updated_by=%s WHERE ingreso_id=%s""",
                  [int(changed),estimated,actual,no_fault,notes,user.id,ingreso_id])
        exec_void('UPDATE ingresos SET descripcion_problema=%s WHERE id=%s',[diagnosis,ingreso_id])
    return detail(ingreso_id,user)


def technical_snapshot(ingreso_id):
    data = detail(ingreso_id)
    return {key:data[key] for key in ('diagnostico','observaciones','items','scope_revision','sin_falla','minutos_estimados')}


def work_text(snapshot, *, actual=False):
    lines = []
    for item in snapshot.get('items',[]):
        quantity = Decimal(str(item['cantidad_realizada'] if actual else max(Decimal(str(item['cantidad_prevista'])),Decimal(str(item['cantidad_realizada'])))))
        if quantity <= 0:
            continue
        label = ('Reemplazo de ' if item['tipo']=='repuesto' else '') + item['descripcion']
        lines.append(f"• {label} (cantidad: {quantity.normalize():f})" + (f". {item['observaciones']}" if item.get('observaciones') else ''))
    if snapshot.get('observaciones'):
        lines.append(snapshot['observaciones'])
    return '\n'.join(lines)


def _json(value):
    return json.dumps(value,ensure_ascii=False,default=str)


def json_object(value):
    # Django deja los JSON de consultas SQL directas como texto para que los decodifique el campo.
    return json.loads(value) if isinstance(value,str) else (value or {})


def stored_document(quote_id):
    # El helper general normaliza bytes como texto; un PDF debe conservar sus bytes originales.
    with connection.cursor() as cursor:
        cursor.execute("SELECT filename,pdf FROM technical_quote_documents WHERE quote_id=%s",[quote_id])
        row = cursor.fetchone()
    return (row[0],bytes(row[1])) if row and row[1] else None


def _fixed_price_choice(ingreso_id):
    candidates = q("""SELECT DISTINCT w.id FROM technical_intervention_items t
        JOIN technical_work_catalog w ON w.id=t.trabajo_id
        WHERE t.ingreso_id=%s AND w.precio_final IS NOT NULL AND w.activo""",[ingreso_id])
    return candidates[0]['id'] if len(candidates)==1 else None, len(candidates)>1


def _labor(ingreso_id, quote_id, overrides=None):
    data = detail(ingreso_id)
    ingreso = ingreso_data(ingreso_id)
    previous = json_object(q("SELECT labor_snapshot FROM quotes WHERE id=%s",[quote_id],one=True)['labor_snapshot'])
    overrides = overrides or {}
    minutes = minutes_value(overrides.get('minutos_presupuestados',previous.get('minutos_presupuestados',data['minutos_estimados'])))
    bonified = overrides.get('bonificado',previous.get('bonificado',False) if data['sin_falla'] else False)
    if not isinstance(bonified,bool):
        raise ValidationError("Bonificado debe ser verdadero o falso.")
    suggested, ambiguous = _fixed_price_choice(ingreso_id)
    selected = overrides.get('precio_final_trabajo_id',previous.get('precio_final_trabajo_id',suggested))
    if data['sin_falla']:
        selected = None
    result = dict(minutos_presupuestados=minutes,bonificado=bonified,sin_falla=data['sin_falla'],tipo_equipo=ingreso['tipo_equipo'],
                  modo_cobro='precio_final' if selected is not None else 'horas',precio_final_trabajo_id=selected,
                  requiere_seleccion_precio_final=(previous.get('requiere_seleccion_precio_final',ambiguous)
                      if 'precio_final_trabajo_id' not in overrides else False))
    if selected is not None:
        selected = catalog_identifier(selected)
        work = q('SELECT id,nombre,precio_final FROM technical_work_catalog WHERE id=%s AND activo',[selected],one=True)
        if not work or work['precio_final'] is None:
            raise ValidationError('Seleccioná un trabajo activo con precio final configurado.')
        default = previous.get('importe') if previous.get('precio_final_trabajo_id')==selected else work['precio_final']
        amount = decimal_value(overrides.get('importe_precio_final',default),'Precio final de la reparación')
        result.update(precio_final_trabajo_id=selected,nombre_precio_final=work['nombre'],tarifa_precio_final=str(work['precio_final']),
                      tarifa_pendiente=False,importe=str(amount))
    elif data['sin_falla']:
        value = q("SELECT diagnostico_limpieza FROM technical_billing_config WHERE id=1",one=True)['diagnostico_limpieza']
        if value is None:
            result.update(tarifa_pendiente=True,importe=None)
        else:
            result.update(tarifa_pendiente=False,tarifa_diagnostico=str(value),importe=str(Decimal('0.00') if bonified else value))
    else:
        rate = q("""SELECT tipo_equipo,marca,primera_hora,hora_adicional,hora_desde_septima FROM technical_labor_rates
                    WHERE (marca IS NOT NULL AND LOWER(BTRIM(marca))=LOWER(BTRIM(%s)))
                       OR (marca IS NULL AND tipo_equipo=LOWER(BTRIM(%s)))
                    ORDER BY (marca IS NOT NULL) DESC LIMIT 1""",[ingreso['marca'],ingreso['tipo_equipo']],one=True)
        if rate is None:
            result.update(tarifa_pendiente=True,importe=None)
        else:
            result.update(tarifa_pendiente=False,primera_hora=str(rate['primera_hora']),hora_adicional=str(rate['hora_adicional']),
                          hora_desde_septima=str(rate['hora_desde_septima'] if rate['hora_desde_septima'] is not None else rate['hora_adicional']),
                          tarifa_tipo_equipo=rate['tipo_equipo'],marca_tarifa=rate['marca'],
                          importe=str(calculate_labor(minutes,rate['primera_hora'],rate['hora_adicional'],rate['hora_desde_septima'])))
    if bonified and not data['sin_falla']:
        raise ValidationError("La bonificación prevista corresponde al diagnóstico y limpieza sin falla.")
    exec_void("UPDATE quotes SET labor_snapshot=%s::jsonb WHERE id=%s",[_json(result),quote_id])
    exec_void("DELETE FROM quote_items WHERE quote_id=%s AND tipo='mano_obra'",[quote_id])
    exec_void('DELETE FROM quote_items WHERE quote_id=%s AND technical_final_price',[quote_id])
    if selected is not None:
        # Conservar cantidades, referencias y costos para stock; el precio acordado
        # cubre toda la reparación y no debe volver a sumar sus repuestos.
        exec_void('''UPDATE quote_items SET precio_u_fuera_precio_final=COALESCE(precio_u_fuera_precio_final,precio_u),precio_u=0
                     WHERE quote_id=%s AND tipo IN ('repuesto','servicio')''',[quote_id])
        exec_void("""INSERT INTO quote_items(quote_id,tipo,descripcion,qty,precio_u,technical_final_price)
                     VALUES (%s,'servicio',%s,1,%s,TRUE)""",
                  [quote_id,work['nombre']+' · Precio final de la reparación (mano de obra y repuestos incluidos)',amount])
        return result
    exec_void('''UPDATE quote_items SET precio_u=precio_u_fuera_precio_final,precio_u_fuera_precio_final=NULL
                 WHERE quote_id=%s AND precio_u_fuera_precio_final IS NOT NULL''',[quote_id])
    hours = format(Decimal(minutes)/60, '.2f').rstrip('0').rstrip('.').replace('.',',')
    label = 'Diagnóstico y limpieza' + (' (bonificado)' if bonified else '') if data['sin_falla'] else f"Mano de obra ({hours} h)"
    exec_void("INSERT INTO quote_items(quote_id,tipo,descripcion,qty,precio_u) VALUES (%s,'mano_obra',%s,1,%s)",[quote_id,label,result['importe'] or 0])
    return result


def sync_draft(ingreso_id, quote_id):
    from .views.quotes_views import _resolve_repuesto_data, _refresh_quote_totals
    snapshot = technical_snapshot(ingreso_id)
    current_ids = [item['id'] for item in snapshot['items']]
    exec_void("DELETE FROM quote_items WHERE quote_id=%s AND technical_item_id IS NOT NULL AND NOT (technical_item_id=ANY(%s::int[]))",[quote_id,current_ids])
    for item in snapshot['items']:
        quantity = max(Decimal(str(item['cantidad_prevista'])),Decimal(str(item['cantidad_realizada'])))
        existing = q("SELECT id,repuesto_id FROM quote_items WHERE quote_id=%s AND technical_item_id=%s",[quote_id,item['id']],one=True)
        if existing and existing['repuesto_id'] == item['repuesto_id']:
            exec_void("UPDATE quote_items SET qty=%s,descripcion=%s WHERE id=%s",[quantity,item['descripcion'],existing['id']])
            continue
        if existing:
            exec_void("DELETE FROM quote_items WHERE id=%s",[existing['id']])
        if item['tipo']=='repuesto':
            rid,code,name,cost,price,_mult = _resolve_repuesto_data(item['repuesto_id'],item['codigo'])
            exec_void("""INSERT INTO quote_items(quote_id,tipo,descripcion,qty,precio_u,repuesto_id,repuesto_codigo,costo_u_neto,technical_item_id)
                         VALUES (%s,'repuesto',%s,%s,%s,%s,%s,%s,%s)""",[quote_id,name,quantity,price or 0,rid,code,cost,item['id']])
        else:
            exec_void("""INSERT INTO quote_items(quote_id,tipo,descripcion,qty,precio_u,technical_item_id)
                         VALUES (%s,'servicio',%s,%s,0,%s)""",[quote_id,item['descripcion'],quantity,item['id']])
    # Una nueva estimación técnica reemplaza el tiempo sugerido, que administración vuelve a revisar.
    suggested, ambiguous = _fixed_price_choice(ingreso_id)
    result = _labor(ingreso_id,quote_id,{'minutos_presupuestados':snapshot['minutos_estimados'],
                                       'precio_final_trabajo_id':suggested})
    if ambiguous and not snapshot['sin_falla']:
        result['requiere_seleccion_precio_final'] = True
        exec_void('UPDATE quotes SET labor_snapshot=%s::jsonb WHERE id=%s',[_json(result),quote_id])
    exec_void("UPDATE quotes SET technical_revision=%s,technical_snapshot=%s::jsonb WHERE id=%s",[snapshot['scope_revision'],_json(snapshot),quote_id])
    _refresh_quote_totals(quote_id)


def submit(ingreso_id,user,expected):
    from .views.quotes_views import _ensure_current_quote, _get_current_quote_row
    with transaction.atomic():
        ingreso_data(ingreso_id,user,write=True,lock=True)
        intervention = _ensure(ingreso_id)
        _check_revision(intervention,expected)
        _audit(user,ingreso_id)
        data = detail(ingreso_id,user)
        if not data['diagnostico'].strip() or not data['items']:
            raise ValidationError("Completá el diagnóstico y agregá los trabajos o repuestos.")
        if data['estado_revision']=='alta_pendiente':
            raise ValidationError("Hay solicitudes de alta pendientes de resolver.")
        if not data['sin_falla'] and data['minutos_estimados']<=0:
            raise ValidationError("Ingresá el tiempo estimado de trabajo.")
        _ensure_current_quote(ingreso_id)
        current = _get_current_quote_row(ingreso_id,for_update=True)
        if current['estado'] == 'pendiente':
            existing_revision = q('SELECT technical_revision FROM quotes WHERE id=%s', [current['id']], one=True)['technical_revision']
            if intervention['submitted_revision'] == intervention['scope_revision'] and existing_revision == intervention['scope_revision']:
                return detail(ingreso_id, user)
            sync_draft(ingreso_id,current['id'])
        elif current['estado'] not in {'presupuestado','emitido','aprobado'}:
            raise ValidationError("Administración debe abrir una nueva versión para esta orden.")
        exec_void("UPDATE technical_interventions SET submitted_revision=scope_revision,submitted_at=now() WHERE ingreso_id=%s",[ingreso_id])
        # Guardar es preparar el diagnóstico. Solo un envío confirmado completa
        # esta etapa, sin retroceder estados de reparación o resoluciones especiales.
        exec_void("""UPDATE ingresos SET estado='diagnosticado',fecha_servicio=COALESCE(fecha_servicio,now())
                     WHERE id=%s AND estado::text IN ('ingresado','asignado')""",[ingreso_id])
    return detail(ingreso_id,user)


def quote_info(ingreso_id,quote_id):
    if not available():
        return None
    row = q("SELECT technical_revision,labor_snapshot,technical_snapshot,supersedes_quote_id FROM quotes WHERE id=%s",[quote_id],one=True)
    data = q("SELECT scope_revision,submitted_revision,minutos_reales FROM technical_interventions WHERE ingreso_id=%s",[ingreso_id],one=True)
    if not data:
        return None
    for key in ('labor_snapshot','technical_snapshot'):
        row[key] = json_object(row[key])
    row.update(scope_revision=data['scope_revision'],submitted_revision=data['submitted_revision'],minutos_reales=data['minutos_reales'])
    row['requiere_actualizacion'] = row['technical_revision'] != data['scope_revision']
    return row


def validate_issue(ingreso_id,quote_id,expected_revision):
    info = quote_info(ingreso_id,quote_id)
    if info is None:
        return
    if info['technical_revision'] != expected_revision or info['requiere_actualizacion'] or info['submitted_revision'] != info['scope_revision']:
        raise Conflict("La carga técnica cambió o aún no fue enviada. Revisá la versión vigente antes de emitir.")
    snapshot = info.get('technical_snapshot') or {}
    if snapshot.get('diagnostico') != (ingreso_data(ingreso_id)['descripcion_problema'] or ''):
        raise Conflict("El diagnóstico cambió después del envío a presupuestar.")
    if not info['labor_snapshot'] or info['labor_snapshot'].get('tarifa_pendiente'):
        raise ValidationError("Falta configurar la tarifa de mano de obra. Revisala antes de emitir.")
    labor = info['labor_snapshot']
    if labor.get('requiere_seleccion_precio_final'):
        raise ValidationError('Hay varios trabajos con precio final. Seleccioná el precio que cubre toda la reparación o revisá el cobro por horas.')
    if labor.get('modo_cobro')=='precio_final':
        totals = q('''SELECT COUNT(*) FILTER (WHERE technical_final_price) AS fixed_count,
            COALESCE(SUM(qty*precio_u),0) AS total,
            COUNT(*) FILTER (WHERE tipo='mano_obra' OR (tipo='repuesto' AND precio_u<>0)) AS extras
            FROM quote_items WHERE quote_id=%s''',[quote_id],one=True)
        if totals['fixed_count']!=1 or totals['extras'] or Decimal(str(totals['total']))!=Decimal(labor['importe']):
            raise ValidationError('El precio final debe incluir todos los repuestos y la mano de obra. Actualizá el cálculo antes de emitir.')
    if q("SELECT id FROM technical_catalog_requests WHERE ingreso_id=%s AND estado='pendiente' LIMIT 1",[ingreso_id],one=True):
        raise ValidationError("Hay solicitudes de alta pendientes.")


def revise_labor(ingreso_id,user,data):
    from .views.quotes_views import _get_current_quote_row,_refresh_quote_totals
    if not can_manage(user):
        raise PermissionDenied("No autorizado para revisar importes.")
    with transaction.atomic():
        ingreso_data(ingreso_id,user,write=True,lock=True)
        current = _get_current_quote_row(ingreso_id,for_update=True)
        if not current or current['estado']!='pendiente':
            raise ValidationError("La revisión de importes requiere un borrador pendiente.")
        _audit(user,ingreso_id)
        _labor(ingreso_id,current['id'],data)
        _refresh_quote_totals(current['id'])


def new_version(ingreso_id,user):
    from .views.quotes_views import _get_current_quote_row,_clone_quote_version
    if not can_manage(user):
        raise PermissionDenied("No autorizado para crear versiones.")
    with transaction.atomic():
        ingreso_data(ingreso_id,user,write=True,lock=True)
        current = _get_current_quote_row(ingreso_id,for_update=True)
        if not current or current['estado'] not in {'presupuestado','emitido','aprobado','rechazado'}:
            raise ValidationError("La versión actual debe estar emitida, aprobada o rechazada.")
        data = detail(ingreso_id,user)
        if data['submitted_revision'] != data['scope_revision']:
            raise ValidationError("El técnico debe enviar la carga actualizada a presupuestar.")
        baseline = q("SELECT id FROM quotes WHERE ingreso_id=%s AND estado='aprobado' ORDER BY version_num DESC,id DESC LIMIT 1",[ingreso_id],one=True)
        _audit(user,ingreso_id)
        new_id = _clone_quote_version(current['id'])
        # Evitar copiar además los mismos artículos como manuales.
        exec_void("DELETE FROM quote_items WHERE quote_id=%s",[new_id])
        exec_void("""INSERT INTO quote_items(quote_id,tipo,descripcion,qty,precio_u,repuesto_id,repuesto_codigo,costo_u_neto,technical_item_id,precio_u_fuera_precio_final)
                    SELECT %s,tipo,descripcion,qty,precio_u,repuesto_id,repuesto_codigo,costo_u_neto,technical_item_id,precio_u_fuera_precio_final
                    FROM quote_items WHERE quote_id=%s AND tipo<>'mano_obra' AND NOT technical_final_price""",[new_id,current['id']])
        exec_void("UPDATE quotes SET supersedes_quote_id=%s WHERE id=%s",[baseline['id'] if baseline else None,new_id])
        sync_draft(ingreso_id,new_id)
        exec_void("UPDATE ingresos SET presupuesto_estado='pendiente', resolucion=CASE WHEN resolucion='presupuesto_rechazado' THEN NULL ELSE resolucion END WHERE id=%s",[ingreso_id])
        return new_id


def stock_quantities(quote_id):
    return {row['repuesto_id']:Decimal(str(row['qty'])) for row in (q("""SELECT repuesto_id,SUM(qty) AS qty FROM quote_items
             WHERE quote_id=%s AND tipo='repuesto' AND repuesto_id IS NOT NULL GROUP BY repuesto_id""",[quote_id]) or [])}


def mark_no_fault(ingreso_id,user):
    """Reutiliza el resultado técnico existente, sin pedir cargar dos veces el hallazgo."""
    if available():
        _audit(user,ingreso_id)
        exec_void("""UPDATE technical_interventions SET sin_falla=TRUE,revision=revision+1,
                      scope_revision=scope_revision+1,updated_at=now(),updated_by=%s
                      WHERE ingreso_id=%s AND NOT sin_falla""",[user.id,ingreso_id])


def approval_stock_delta(ingreso_id,quote_id):
    current = q("SELECT supersedes_quote_id,stock_applied FROM quotes WHERE id=%s",[quote_id],one=True)
    if current['stock_applied']:
        return []
    baseline = stock_quantities(current['supersedes_quote_id']) if current['supersedes_quote_id'] else {}
    final = stock_quantities(quote_id)
    return [dict(repuesto_id=key,qty=final.get(key,Decimal(0))-baseline.get(key,Decimal(0))) for key in sorted(final.keys()|baseline.keys())]


def finish_approval(quote_id):
    exec_void("UPDATE quotes SET stock_applied=TRUE WHERE id=%s",[quote_id])
    exec_void("UPDATE quotes SET estado='sustituido' WHERE id=(SELECT supersedes_quote_id FROM quotes WHERE id=%s) AND estado='aprobado'",[quote_id])


def queue_status(ingreso_ids):
    if not ingreso_ids or not available():
        return {}
    rows = q("""SELECT i.ingreso_id,
                CASE WHEN EXISTS (SELECT 1 FROM technical_catalog_requests r WHERE r.ingreso_id=i.ingreso_id AND r.estado='pendiente') THEN 'alta_pendiente'
                     WHEN i.submitted_revision IS DISTINCT FROM i.scope_revision THEN 'en_preparacion'
                     WHEN i.scope_revision IS DISTINCT FROM (SELECT q.technical_revision FROM quotes q WHERE q.ingreso_id=i.ingreso_id ORDER BY q.version_num DESC,q.id DESC LIMIT 1) THEN 'ampliacion_pendiente'
                     ELSE 'listo_revision' END AS estado
          FROM technical_interventions i WHERE i.ingreso_id=ANY(%s::int[])""",[ingreso_ids]) or []
    return {r['ingreso_id']:r['estado'] for r in rows}
