from django.db import transaction
from rest_framework import permissions
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import technical_interventions as technical
from .helpers import q, exec_void, exec_returning


def require_manager(request):
    if not technical.can_manage(request.user):
        raise PermissionDenied("Se requiere permiso para administrar presupuestos.")


class TechnicalInterventionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id):
        return Response(technical.detail(ingreso_id,request.user))

    def patch(self, request, ingreso_id):
        return Response(technical.save(ingreso_id,request.user,request.data))


class TechnicalSubmitView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id):
        return Response(technical.submit(ingreso_id,request.user,request.data.get('revision')))


class TechnicalPartsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id):
        from .repuestos_views import CatalogoRepuestosView
        technical.ingreso_data(ingreso_id,request.user)
        response = CatalogoRepuestosView().get(request)
        return Response([{key:row.get(key) for key in ('id','codigo','nombre','equipos_asociados')} for row in response.data])


class TechnicalWorkCatalogView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not technical.can_manage(request.user) and not technical.user_has_permission(request.user,'action.ingreso.edit_diagnosis'):
            raise PermissionDenied()
        all_items = technical.can_manage(request.user) and request.query_params.get('todos')=='1'
        return Response(q("SELECT id,nombre,activo FROM technical_work_catalog" + ('' if all_items else ' WHERE activo') + ' ORDER BY nombre'))

    def post(self, request):
        require_manager(request)
        name = str(request.data.get('nombre','')).strip()
        if not name or len(name)>250:
            raise ValidationError("Ingresá un nombre de hasta 250 caracteres.")
        existing = q("SELECT id FROM technical_work_catalog WHERE LOWER(BTRIM(nombre))=LOWER(%s)",[name],one=True)
        if existing:
            raise ValidationError("El trabajo ya existe en el catálogo.")
        ident = exec_returning("INSERT INTO technical_work_catalog(nombre) VALUES (%s) RETURNING id",[name])
        return Response(dict(id=ident,nombre=name,activo=True),status=201)


class TechnicalWorkCatalogDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self,request,item_id):
        require_manager(request)
        if not isinstance(request.data.get('activo'),bool):
            raise ValidationError("Indicá si el trabajo está activo.")
        exec_void("UPDATE technical_work_catalog SET activo=%s WHERE id=%s",[request.data['activo'],item_id])
        return Response({'ok':True})


class TechnicalRatesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self,request):
        require_manager(request)
        return Response(dict(tarifas=q("SELECT tipo_equipo,primera_hora,hora_adicional FROM technical_labor_rates ORDER BY tipo_equipo"),
            configuracion=q("SELECT diagnostico_limpieza FROM technical_billing_config WHERE id=1",one=True),
            tipos_equipo=q("SELECT nombre FROM catalogo_tipos_equipo WHERE activo ORDER BY nombre")))

    def put(self,request):
        require_manager(request)
        data = request.data
        with transaction.atomic():
            technical._audit(request.user,0)
            if 'diagnostico_limpieza' in data:
                value = technical.decimal_value(data['diagnostico_limpieza'],'Diagnóstico y limpieza')
                exec_void("UPDATE technical_billing_config SET diagnostico_limpieza=%s WHERE id=1",[value])
            if 'tipo_equipo' in data:
                kind = str(data['tipo_equipo']).strip().lower()
                if not kind or len(kind)>160:
                    raise ValidationError("Seleccioná un tipo de equipo.")
                first = technical.decimal_value(data.get('primera_hora'),'Primera hora')
                following = technical.decimal_value(data.get('hora_adicional'),'Hora adicional')
                exec_void("""INSERT INTO technical_labor_rates(tipo_equipo,primera_hora,hora_adicional) VALUES (%s,%s,%s)
                           ON CONFLICT (tipo_equipo) DO UPDATE SET primera_hora=EXCLUDED.primera_hora,
                           hora_adicional=EXCLUDED.hora_adicional,updated_at=now()""",[kind,first,following])
        return self.get(request)


class TechnicalLaborReviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self,request,ingreso_id):
        require_manager(request)
        technical.revise_labor(ingreso_id,request.user,request.data)
        from .quotes_views import _quote_response
        return _quote_response(request,ingreso_id)


class TechnicalCatalogRequestsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self,request):
        require_manager(request)
        return Response(q("SELECT * FROM technical_catalog_requests ORDER BY (estado='pendiente') DESC,id DESC LIMIT 500"))

    def post(self,request):
        data = request.data
        ingreso_id = technical.catalog_identifier(data.get('ingreso_id'))
        with transaction.atomic():
            technical.ingreso_data(ingreso_id,request.user,write=True,lock=True)
            technical._ensure(ingreso_id)
            technical._audit(request.user,ingreso_id)
            kind = data.get('tipo')
            name = str(data.get('nombre','')).strip()
            if kind not in ('trabajo','repuesto') or not name or len(name)>250:
                raise ValidationError("Indicá el tipo de artículo y un nombre de hasta 250 caracteres.")
            existing = q("""SELECT id FROM technical_catalog_requests WHERE ingreso_id=%s AND tipo=%s
                         AND LOWER(nombre)=LOWER(%s) AND estado='pendiente'""",[ingreso_id,kind,name],one=True)
            if existing:
                return Response(existing)
            ident = exec_returning("""INSERT INTO technical_catalog_requests(ingreso_id,tipo,nombre,cantidad,observaciones,created_by)
                  VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",[ingreso_id,kind,name,
                  technical.decimal_value(data.get('cantidad',1),'Cantidad',positive=True),str(data.get('observaciones','')).strip()[:2000],request.user.id])
            exec_void("UPDATE technical_interventions SET revision=revision+1,scope_revision=scope_revision+1 WHERE ingreso_id=%s",[ingreso_id])
        return Response({'id':ident},status=201)


class TechnicalCatalogRequestDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self,request,request_id):
        require_manager(request)
        with transaction.atomic():
            row = q("SELECT * FROM technical_catalog_requests WHERE id=%s",[request_id],one=True)
            if not row:
                raise NotFound("Solicitud inexistente.")
            technical.ingreso_data(row['ingreso_id'],request.user,write=True,lock=True)
            row = q("SELECT * FROM technical_catalog_requests WHERE id=%s FOR UPDATE",[request_id],one=True)
            if row['estado']!='pendiente':
                return Response({'ok':True})
            technical._audit(request.user,row['ingreso_id'])
            state = request.data.get('estado')
            if state not in ('aprobada','rechazada'):
                raise ValidationError("Estado de solicitud inválido.")
            catalog_id = request.data.get('catalog_id')
            response = str(request.data.get('respuesta','')).strip()[:2000]
            if state=='aprobada':
                catalog_id = technical.catalog_identifier(catalog_id)
                table = 'technical_work_catalog' if row['tipo']=='trabajo' else 'catalogo_repuestos'
                if not q(f"SELECT id FROM {table} WHERE id=%s AND activo",[catalog_id],one=True):
                    raise ValidationError("Seleccioná un artículo activo. Podés darlo de alta primero en el catálogo.")
                exec_void("""INSERT INTO technical_intervention_items(ingreso_id,tipo,trabajo_id,repuesto_id,cantidad_prevista,observaciones)
                       VALUES (%s,%s,%s,%s,%s,%s)""",[row['ingreso_id'],row['tipo'],catalog_id if row['tipo']=='trabajo' else None,
                       catalog_id if row['tipo']=='repuesto' else None,row['cantidad'],row['observaciones']])
            elif not response:
                raise ValidationError("Indicá el motivo del rechazo.")
            exec_void("""UPDATE technical_catalog_requests SET estado=%s,resolved_catalog_id=%s,respuesta=%s,
                      resolved_by=%s,resolved_at=now() WHERE id=%s""",[state,catalog_id if state=='aprobada' else None,response,request.user.id,request_id])
            exec_void("UPDATE technical_interventions SET revision=revision+1,scope_revision=scope_revision+1 WHERE ingreso_id=%s",[row['ingreso_id']])
        return Response({'ok':True})
