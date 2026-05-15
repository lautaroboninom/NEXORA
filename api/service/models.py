#service/models.py
from django.db import models

class User(models.Model):
    id = models.AutoField(primary_key=True)
    nombre = models.TextField()
    email = models.TextField(unique=True)
    hash_pw = models.TextField()
    rol = models.TextField()
    activo = models.BooleanField(default=True)

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    class Meta:
        managed = False
        db_table = "users"

class Customer(models.Model):
    id = models.AutoField(primary_key=True)
    cod_empresa = models.TextField(null=True)
    razon_social = models.TextField()
    cuit = models.TextField(null=True)
    contacto = models.TextField(null=True)
    telefono = models.TextField(null=True)
    telefono_2 = models.TextField(null=True)
    email = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "customers"

class Marca(models.Model):
    id = models.AutoField(primary_key=True)
    nombre = models.TextField(unique=True)
    class Meta:
        managed = False
        db_table = "marcas"

class Model(models.Model):
    id = models.AutoField(primary_key=True)
    marca = models.ForeignKey(Marca, on_delete=models.RESTRICT, db_column="marca_id")
    nombre = models.TextField()
    class Meta:
        managed = False
        db_table = "models"
        unique_together = (("marca","nombre"),)

class Device(models.Model):
    id = models.AutoField(primary_key=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, db_column="customer_id")
    marca = models.ForeignKey(Marca, null=True, on_delete=models.SET_NULL, db_column="marca_id")
    model = models.ForeignKey(Model, null=True, on_delete=models.SET_NULL, db_column="model_id")
    numero_serie = models.TextField(null=True)
    numero_interno = models.TextField(null=True)
    tipo_equipo = models.TextField(null=True)
    variante = models.TextField(null=True)
    garantia_vence = models.DateField(null=True)
    propietario = models.TextField(null=True)
    propietario_nombre = models.TextField(null=True)
    propietario_contacto = models.TextField(null=True)
    propietario_doc = models.TextField(null=True)
    n_de_control = models.TextField(null=True)
    alquilado = models.BooleanField(default=False)
    alquiler_a = models.TextField(null=True)
    ubicacion_id = models.IntegerField(null=True)
    class Meta:
        managed = False
        db_table = "devices"

class Ingreso(models.Model):
    id = models.AutoField(primary_key=True)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, db_column="device_id")
    estado = models.TextField()
    motivo = models.TextField()
    fecha_ingreso = models.DateTimeField(null=True, blank=True)
    fecha_creacion = models.DateTimeField()
    fecha_servicio = models.DateTimeField(null=True, blank=True)
    sala_origen = models.TextField(null=True)
    ubicacion_id = models.IntegerField(null=True)
    disposicion = models.TextField()
    informe_preliminar = models.TextField(null=True)
    accesorios = models.TextField(null=True)
    remito_ingreso = models.TextField(null=True)
    recibido_por = models.IntegerField(null=True)
    comentarios = models.TextField(null=True)
    presupuesto_estado = models.TextField()
    asignado_a = models.IntegerField(null=True)
    etiqueta_qr = models.TextField(null=True)
    etiq_garantia_ok = models.BooleanField(null=True)

    class Meta:
        managed = False
        db_table = "ingresos"

class Quote(models.Model):
    id = models.AutoField(primary_key=True)
    ingreso = models.OneToOneField(Ingreso, on_delete=models.CASCADE, db_column="ingreso_id")
    estado = models.TextField()
    moneda = models.TextField()
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    iva_21 = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    autorizado_por = models.TextField(null=True)
    forma_pago = models.TextField(null=True)
    plazo_entrega_txt = models.TextField(null=True)
    garantia_txt = models.TextField(null=True)
    mant_oferta_txt = models.TextField(null=True)
    fecha_emitido = models.DateTimeField(null=True)
    fecha_aprobado = models.DateTimeField(null=True)
    pdf_url = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "quotes"

class IngresoMedia(models.Model):
    id = models.AutoField(primary_key=True)
    ingreso = models.ForeignKey(Ingreso, on_delete=models.CASCADE, db_column="ingreso_id", related_name="media_items")
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, db_column="usuario_id")
    storage_path = models.TextField()
    thumbnail_path = models.TextField()
    original_name = models.TextField(null=True, blank=True)
    mime_type = models.CharField(max_length=80)
    size_bytes = models.BigIntegerField()
    width = models.IntegerField()
    height = models.IntegerField()
    comentario = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "ingreso_media"
        ordering = ("-created_at",)

    def __str__(self):
        return f"IngresoMedia({self.id})"
