# service/serializers.py
from rest_framework import serializers
from .models import Ingreso, Quote, Customer, Device, Marca, Model as DeviceModel, IngresoMedia


# --- ModelSerializers base (lo que ya usabas) ---
class IngresoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingreso
        fields = "__all__"


class QuoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Quote
        fields = "__all__"


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = "__all__"


class DeviceListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Device
        # Usar nombres de campo Django (FKs), no los *_id internos
        fields = (
            "id", "customer", "marca", "model",
            "numero_serie", "numero_interno", "tipo_equipo", "variante",
            "garantia_vence", "alquilado", "alquiler_a", "ubicacion_id",
        )


class MarcaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Marca
        fields = "__all__"


class ModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceModel
        fields = "__all__"


# --- NUEVOS: útiles para endpoints de lista/detalle que devuelven dicts (SQL crudo) ---

class IngresoListItemSerializer(serializers.Serializer):
    """
    Útil para endpoints tipo:
      PendientesGeneral, PendientesPresupuesto, Presupuestados,
      AprobadosParaReparar, AprobadosYReparados, Liberados, GeneralEquipos, etc.
    Matchea las columnas que ya entrega el back y que espera el front.
    """
    id = serializers.IntegerField()
    estado = serializers.CharField()
    presupuesto_estado = serializers.CharField(allow_null=True, required=False)
    motivo = serializers.CharField(required=False, allow_blank=True)
    fecha_ingreso = serializers.DateTimeField(required=False, allow_null=True)
    razon_social = serializers.CharField(allow_blank=True, required=False)
    numero_serie = serializers.CharField(allow_blank=True, required=False)
    numero_interno = serializers.CharField(allow_blank=True, required=False)
    marca = serializers.CharField(allow_blank=True, required=False)
    modelo = serializers.CharField(allow_blank=True, required=False)
    tipo_equipo = serializers.CharField(allow_blank=True, required=False)
    equipo_variante = serializers.CharField(allow_blank=True, required=False, allow_null=True)
    fecha_servicio = serializers.DateTimeField(required=False, allow_null=True)
    fecha_entrega = serializers.DateTimeField(required=False, allow_null=True)
    fecha_aprobado = serializers.DateTimeField(required=False, allow_null=True)
    
    # Campos opcionales que algunas vistas ya traen
    fecha_reparado = serializers.DateTimeField(required=False, allow_null=True)
    fecha_listo = serializers.DateTimeField(required=False, allow_null=True)
    presupuesto_numero = serializers.CharField(required=False, allow_blank=True)
    presupuesto_monto = serializers.FloatField(required=False, allow_null=True)
    presupuesto_moneda = serializers.CharField(required=False, allow_blank=True)
    presupuesto_fecha_emision = serializers.DateTimeField(required=False, allow_null=True)
    presupuesto_fecha_envio = serializers.DateTimeField(required=False, allow_null=True)
    ubicacion_id = serializers.IntegerField(required=False, allow_null=True)
    ubicacion_nombre = serializers.CharField(required=False, allow_blank=True)

    # Campos opcionales para búsquedas por accesorios
    accesorio_nombre = serializers.CharField(required=False, allow_blank=True)
    referencia = serializers.CharField(required=False, allow_blank=True)

    # Flags opcionales
    derivado_devuelto = serializers.BooleanField(required=False)


class IngresoDetailSerializer(serializers.Serializer):
    # Identificación / estados
    id = serializers.IntegerField()
    os = serializers.CharField()
    motivo = serializers.CharField()
    permite_reparacion = serializers.BooleanField(required=False)
    estado = serializers.CharField()
    presupuesto_estado = serializers.CharField(allow_null=True)
    fecha_ingreso = serializers.DateTimeField()
    fecha_servicio = serializers.DateTimeField(allow_null=True, required=False)
    fecha_entrega = serializers.DateTimeField(allow_null=True, required=False)
    remito_salida = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    factura_numero = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    garantia_reparacion = serializers.BooleanField(required=False)
    garantia_reparacion_trabajos = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    faja_garantia = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    etiq_garantia_ok = serializers.BooleanField(required=False)
    remito_ingreso = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    alquilado = serializers.BooleanField(required=False)
    alquiler_a = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    alquiler_remito = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    alquiler_fecha = serializers.DateField(allow_null=True, required=False)

    # Textos de ingreso + técnico
    informe_preliminar   = serializers.CharField(allow_null=True, allow_blank=True)
    descripcion_problema = serializers.CharField(allow_null=True, allow_blank=True)
    trabajos_realizados  = serializers.CharField(allow_null=True, allow_blank=True)
    accesorios           = serializers.CharField(allow_null=True, allow_blank=True)
    comentarios          = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    equipo_variante      = serializers.CharField(allow_null=True, allow_blank=True, required=False)

    # Ubicación
    ubicacion_id = serializers.IntegerField(allow_null=True)
    ubicacion_nombre = serializers.CharField(allow_blank=True)

    # Equipo
    device_id = serializers.IntegerField()
    numero_serie = serializers.CharField()
    # MG / número interno del equipo
    numero_interno = serializers.CharField(allow_blank=True, required=False)
    mg_estado = serializers.CharField(allow_blank=True, required=False)
    mg_activo = serializers.BooleanField(required=False)
    mg_inactivo_venta = serializers.BooleanField(required=False)
    mg_inactivo_desde = serializers.DateTimeField(allow_null=True, required=False)
    mg_venta_fecha = serializers.DateTimeField(allow_null=True, required=False)
    mg_venta_factura_numero = serializers.CharField(allow_blank=True, required=False)
    mg_venta_remito_numero = serializers.CharField(allow_blank=True, required=False)
    mg_venta_observaciones = serializers.CharField(allow_blank=True, required=False)
    mg_venta_usuario_id = serializers.IntegerField(allow_null=True, required=False)
    es_propietario_mg = serializers.BooleanField(required=False)
    vendido = serializers.BooleanField(required=False)
    garantia = serializers.BooleanField()
    marca_id = serializers.IntegerField()
    marca = serializers.CharField()
    model_id = serializers.IntegerField()
    modelo = serializers.CharField()
    # Tipo de equipo (opcional)
    tipo_equipo = serializers.CharField(allow_blank=True, required=False)
    tipo_equipo_nombre = serializers.CharField(allow_blank=True, required=False)

    # Cliente
    customer_id = serializers.IntegerField()
    razon_social = serializers.CharField()
    cod_empresa = serializers.CharField(allow_blank=True)
    telefono = serializers.CharField(allow_blank=True)

    # Asignación y propietario (opcionales)
    asignado_a = serializers.IntegerField(allow_null=True, required=False)
    asignado_a_nombre = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    ingresado_por_id = serializers.IntegerField(allow_null=True, required=False)
    ingresado_por_nombre = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    propietario_nombre = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    propietario_contacto = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    propietario_doc = serializers.CharField(allow_blank=True, allow_null=True, required=False)

    resolucion = serializers.CharField(allow_null=True, allow_blank=True)
    serial_cambio = serializers.CharField(allow_null=True, allow_blank=True, required=False)

    # Solicitud de asignación (opcional, calculado desde logs/tabla auxiliar)
    tecnico_solicitado_id = serializers.IntegerField(allow_null=True, required=False)
    tecnico_solicitado_nombre = serializers.CharField(allow_blank=True, required=False)
    baja_solicitada_id = serializers.IntegerField(allow_null=True, required=False)
    baja_solicitada_nombre = serializers.CharField(allow_blank=True, required=False)
    baja_solicitada_motivo = serializers.CharField(allow_blank=True, required=False)
    baja_solicitada_fecha = serializers.DateTimeField(allow_null=True, required=False)

class IngresoAccesorioItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    accesorio_id = serializers.IntegerField()
    accesorio_nombre = serializers.CharField()
    referencia = serializers.CharField(allow_null=True, allow_blank=True)
    descripcion = serializers.CharField(allow_null=True, allow_blank=True)


class IngresoDetailWithAccesoriosSerializer(IngresoDetailSerializer):
    accesorios_items = IngresoAccesorioItemSerializer(many=True, required=False)
    alquiler_accesorios_items = IngresoAccesorioItemSerializer(many=True, required=False)


class IngresoMediaItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    ingreso_id = serializers.IntegerField()
    usuario_id = serializers.IntegerField()
    usuario_nombre = serializers.CharField(allow_blank=True)
    comentario = serializers.CharField(allow_null=True, allow_blank=True)
    mime_type = serializers.CharField()
    size_bytes = serializers.IntegerField()
    width = serializers.IntegerField()
    height = serializers.IntegerField()
    original_name = serializers.CharField(allow_null=True, allow_blank=True, required=False)
    url = serializers.CharField()
    thumbnail_url = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

class QuoteItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    tipo = serializers.CharField()  # 'repuesto' | 'mano_obra' | 'servicio'
    repuesto_id = serializers.IntegerField(allow_null=True, required=False)
    repuesto_codigo = serializers.CharField(allow_null=True, required=False)
    descripcion = serializers.CharField()
    qty = serializers.DecimalField(max_digits=10, decimal_places=2)
    precio_u = serializers.DecimalField(max_digits=12, decimal_places=2)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    costo_u_neto = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True, required=False)
    costo_total_neto = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True, required=False)


class QuoteDetailSerializer(serializers.Serializer):
    ingreso_id = serializers.IntegerField()
    quote_id = serializers.IntegerField()
    estado = serializers.CharField()
    moneda = serializers.CharField()
    autorizado_por = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    forma_pago = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    plazo_entrega_txt = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    garantia_txt = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    mant_oferta_txt = serializers.CharField(allow_blank=True, allow_null=True, required=False)
    items = QuoteItemSerializer(many=True)
    pdf_url = serializers.CharField(required=False, allow_blank=True)
    
    tot_repuestos = serializers.DecimalField(max_digits=12, decimal_places=2)
    mano_obra = serializers.DecimalField(max_digits=12, decimal_places=2)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2)
    iva_21 = serializers.DecimalField(max_digits=12, decimal_places=2)
    total = serializers.DecimalField(max_digits=12, decimal_places=2)
