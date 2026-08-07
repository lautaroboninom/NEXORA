# app/settings.py
import os
from pathlib import Path
from corsheaders.defaults import default_headers, default_methods

BASE_DIR = Path(__file__).resolve().parent.parent

def _csv(name: str, default: str = ""):
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int((os.getenv(name, "") or "").strip() or default)
    except (TypeError, ValueError):
        return int(default)


def _float_env(name: str, default: float = 0.0) -> float:
    try:
        return float((os.getenv(name, "") or "").strip() or default)
    except (TypeError, ValueError):
        return float(default)


# --- Núcleo / seguridad ---
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = _csv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# Orígenes del navegador (con esquema http/https)
CORS_ALLOWED_ORIGINS = _csv("ALLOWED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

# Auditoría
AUDIT_LOG_ENABLED = os.getenv("AUDIT_LOG_ENABLED", "0").lower() in ("1","true")
AUDIT_LOG_MAX_BODY = int(os.getenv("AUDIT_LOG_MAX_BODY", "4096"))
AUDIT_LOG_EXCLUDE_PREFIXES = _csv("AUDIT_LOG_EXCLUDE_PREFIXES", "")

# Integracion read-only con Portal Sepid.
# Guardar solo el hash SHA-256 del token, nunca el secreto plano.
PORTAL_INTEGRATION_TOKEN_SHA256 = os.getenv("PORTAL_INTEGRATION_TOKEN_SHA256", "")
PORTAL_INTEGRATION_TOKEN_SHA256_FALLBACKS = _csv("PORTAL_INTEGRATION_TOKEN_SHA256_FALLBACKS", "")
# Opcional: IPs o CIDRs permitidos, por ejemplo rangos Tailscale. Vacio no restringe por IP.
PORTAL_INTEGRATION_ALLOWED_IPS = _csv("PORTAL_INTEGRATION_ALLOWED_IPS", "")

# Branding / URLs públicas
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
PUBLIC_WEB_URL = os.getenv("PUBLIC_WEB_URL", FRONTEND_ORIGIN)
LOGO_PATH = os.getenv("LOGO_PATH", "/code/service/static/logo.png")  # usado por PDF

# Company header for PDFs (static across companies)
COMPANY_HEADER_L1 = os.getenv("COMPANY_HEADER_L1", "Valdenegro 4578 C.A.B.A (1430)")
COMPANY_HEADER_L2 = os.getenv("COMPANY_HEADER_L2", "IMPORTADORES DE EQUIPOS")
COMPANY_HEADER_L3 = os.getenv("COMPANY_HEADER_L3", "MEDICOS Y REPARACIONES")

# Datos de contacto para pie de página del presupuesto (pueden cambiar vía entorno)
COMPANY_FOOTER_EMAIL = os.getenv("COMPANY_FOOTER_EMAIL", "tecnica@sepid.com.ar")
COMPANY_FOOTER_CUIT = os.getenv("COMPANY_FOOTER_CUIT", "30-71006956-1")
COMPANY_FOOTER_WEB = os.getenv("COMPANY_FOOTER_WEB", "https://sepid.com.ar")
COMPANY_FOOTER_WHATSAPP = os.getenv("COMPANY_FOOTER_WHATSAPP", "+54 9 11 6675-4115")

COMPANY_FOOTER_EMAIL_2 = os.getenv("COMPANY_FOOTER_EMAIL_2", "serviciotecnicomgbio@gmail.com")
COMPANY_FOOTER_CUIT_2 = os.getenv("COMPANY_FOOTER_CUIT_2", "30-71227174-0")
COMPANY_FOOTER_WEB_2 = os.getenv("COMPANY_FOOTER_WEB_2", "https://sepid.com.ar")
COMPANY_FOOTER_WHATSAPP_2 = os.getenv("COMPANY_FOOTER_WHATSAPP_2", "+54 9 11 6675-4115")


# Directorio opcional donde guardar copias de PDFs de presupuestos
# Si existe, se escriben allí además de devolverse al cliente
QUOTES_SAVE_DIR = os.getenv(
    "QUOTES_SAVE_DIR",
    r"Z:\MG BIO\1 PRESUPUESTOS MGBIO SA\2025\Pendientes de envío"
)

# Email
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _bool_env("EMAIL_USE_TLS", "1")
EMAIL_USE_SSL = _bool_env("EMAIL_USE_SSL", "0")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))
EMAIL_INSECURE_SKIP_VERIFY = _bool_env("EMAIL_INSECURE_SKIP_VERIFY", "0")
# Los correos remotos no deben demorar las acciones de operación. Los backends
# locales de desarrollo y prueba se mantienen sincrónicos para facilitar su verificación.
EMAIL_DELIVERY_ASYNC = _bool_env("EMAIL_DELIVERY_ASYNC", "1")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@sepid.com.ar")
EMAIL_LEGAL_FOOTER = os.getenv(
    "EMAIL_LEGAL_FOOTER",
    (
        "La información de este correo es confidencial y concierne únicamente a la persona a la que está dirigida. "
        "Se niega el consentimiento para que pueda ser empleada como prueba por el destinatario en los términos que autoriza el art. 318 del CCyCN. "
        "Si este mensaje no está dirigido a usted, por favor tenga presente que no tiene autorización para leer el resto de este correo, copiarlo o derivarlo a cualquier otra persona que no sea aquella a la que está dirigido, como así tampoco valerse del mismo. "
        "Si recibe este correo por error, por favor, avise al remitente, luego de lo cual rogamos a usted destruya el mensaje original. "
        "No se puede responsabilizar al remitente de ninguna forma por/o en relación con alguna consecuencia y/o daño que resulte del apropiado y completo envío y recepción del contenido de este correo."
    ),
)

# Notificaciones: solicitudes de asignación de técnico
ASSIGNMENT_REQUEST_RECIPIENTS = _csv("ASSIGNMENT_REQUEST_RECIPIENTS", "")
# Notificaciones: bajas de equipos (otros sistemas)
BAJA_NOTIFY_RECIPIENTS = _csv("BAJA_NOTIFY_RECIPIENTS", "eduardo@sepid.com.ar")
# Avisos patrimoniales (baja, alta, alta MG y venta MG). Si queda vacío se usan
# los mismos destinatarios que las bajas (resuelto en tiempo de envío).
PATRIMONIAL_NOTIFY_RECIPIENTS = _csv("PATRIMONIAL_NOTIFY_RECIPIENTS", "")
# Notificaciones: presupuestos pendientes (solo rol jefe)
PRESUPUESTO_ALERT_ENABLED = os.getenv("PRESUPUESTO_ALERT_ENABLED", "1").lower() in ("1", "true", "yes")
PRESUPUESTO_ALERT_FIRST_DAYS = int(os.getenv("PRESUPUESTO_ALERT_FIRST_DAYS", "7"))
PRESUPUESTO_ALERT_REPEAT_DAYS = int(os.getenv("PRESUPUESTO_ALERT_REPEAT_DAYS", "3"))
PRESUPUESTO_ALERT_LOCATION = os.getenv("PRESUPUESTO_ALERT_LOCATION", "taller")
# Notificaciones: mantenimientos preventivos (solo rol jefe)
PREVENTIVO_ALERT_ENABLED = os.getenv("PREVENTIVO_ALERT_ENABLED", "1").lower() in ("1", "true", "yes")
PREVENTIVO_DEFAULT_LEAD_DAYS = int(os.getenv("PREVENTIVO_DEFAULT_LEAD_DAYS", "30"))

# Notificaciones Web Push. Requiere claves VAPID configuradas en el entorno.
WEB_PUSH_VAPID_PUBLIC_KEY = os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "")
WEB_PUSH_VAPID_PRIVATE_KEY = os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "")
WEB_PUSH_VAPID_SUBJECT = os.getenv("WEB_PUSH_VAPID_SUBJECT", "")

# Zona horaria
TIME_ZONE = os.getenv("TZ", "America/Argentina/Buenos_Aires")
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "service",
]

MIDDLEWARE = [
    "service.middleware.RequestTimingMiddleware",     # primero: mide el request completo
    "corsheaders.middleware.CorsMiddleware",          # debe ir arriba de CommonMiddleware
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "service.middleware.AuditUserMiddleware",         # set app.user_id/app.user_role por request
    "service.middleware.RLSMiddleware",               # RLS por-request
    "service.middleware.ActivityLogMiddleware",       # auditoría (con exclusiones por prefijo)
]

ROOT_URLCONF = "app.urls"
WSGI_APPLICATION = "app.wsgi.application"

# Templates (habilita carga de plantillas por app_dir)
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [],
        },
    }
]

# --- Base de datos: solo PostgreSQL ---
# Variables esperadas: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", os.getenv("PGDATABASE", "servicio_tecnico")),
        "USER": os.getenv("POSTGRES_USER", os.getenv("PGUSER", "sepid")),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", os.getenv("PGPASSWORD", "supersegura")),
        "HOST": os.getenv("POSTGRES_HOST", os.getenv("PGHOST", "postgres")),
        "PORT": os.getenv("POSTGRES_PORT", os.getenv("PGPORT", "5432")),
        "ATOMIC_REQUESTS": True,
        # Reutilización de conexiones
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
    }
}

# Umbral en ms a partir del cual RequestTimingMiddleware loguea un request.
# 0 lo desactiva.
REQUEST_TIMING_LOG_THRESHOLD_MS = int(os.getenv("REQUEST_TIMING_LOG_THRESHOLD_MS", "1000"))

CACHES = {
    "default": {
        "BACKEND": os.getenv("DJANGO_CACHE_BACKEND", "django.core.cache.backends.locmem.LocMemCache"),
        "LOCATION": os.getenv("DJANGO_CACHE_LOCATION", "default"),
    }
}


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "service.auth.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "service.permissions.MappedPermissionGuard",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "UNAUTHENTICATED_TOKEN": None,
    # Normaliza 401/403 y agrega WWW-Authenticate en 401
    "EXCEPTION_HANDLER": "service.exceptions.handler",
}

# Feature flag para permisos granulares por usuario.
PERMISSIONS_V2_ENABLED = os.getenv("PERMISSIONS_V2_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# CORS
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = list(default_headers) + ["authorization"]
CORS_ALLOW_METHODS = list(default_methods)
# Solo útil en dev/LAN; no afecta prod si no se usa
CORS_ALLOW_PRIVATE_NETWORK = True

# Static
STATIC_URL = "/static/"

# Password hashing: priorizar Argon2 (tenés argon2-cffi en requirements)
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

# Endurecimiento cuando DEBUG=False
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Media para fotos de ingresos
INGRESO_MEDIA_MAX_SIZE_MB = int(os.getenv('INGRESO_MEDIA_MAX_SIZE_MB', '10'))
INGRESO_MEDIA_MAX_FILES = int(os.getenv('INGRESO_MEDIA_MAX_FILES', '50'))
INGRESO_MEDIA_THUMB_MAX = int(os.getenv('INGRESO_MEDIA_THUMB_MAX', '512'))
INGRESO_MEDIA_ALLOWED_MIME = [
    m.strip()
    for m in os.getenv(
        'INGRESO_MEDIA_ALLOWED_MIME',
        # permitir imágenes + PDF + MP4 por defecto
        'image/jpeg,image/png,application/pdf,video/mp4'
    ).split(',') if m.strip()
]

# Ruta al repositorio de trazabilidad (Excels con ventas)
# Se puede sobreescribir con la variable de entorno TRAZABILIDAD_ROOT
TRAZABILIDAD_ROOT = os.getenv(
    "TRAZABILIDAD_ROOT",
    r"\\SERVERDATA\Datos\MG BIO\TRAZABILIDAD"
)
"""
Ruta absoluta del Excel principal de trazabilidad (GENERAL).
Se puede sobreescribir con la variable TRAZABILIDAD_GENERAL_FILE.
Si no se define, se intenta resolver a partir de TRAZABILIDAD_ROOT.
"""
TRAZABILIDAD_GENERAL_FILE = os.getenv(
    "TRAZABILIDAD_GENERAL_FILE",
    os.path.join(TRAZABILIDAD_ROOT, "@GENERAL.xlsx")
)

# Catalogo de repuestos (costos)
REPUESTOS_COSTOS_ROOT = os.getenv(
    "REPUESTOS_COSTOS_ROOT",
    r"\\SERVERDATA\Datos\Servicio Tecnico"
)

REPUESTOS_COSTOS_FILE = os.getenv(
    "REPUESTOS_COSTOS_FILE",
    os.path.join(REPUESTOS_COSTOS_ROOT, "repuestos_unificados.xlsx")
)

# Catalogo de repuestos (listado unificado: codigo/descripcion/proveedor)
REPUESTOS_UNIFICADOS_FILE = os.getenv(
    "REPUESTOS_UNIFICADOS_FILE",
    os.path.join(REPUESTOS_COSTOS_ROOT, "repuestos_unificados.xlsx")
)

INGRESO_MEDIA_STORAGE_PREFIX = os.getenv('INGRESO_MEDIA_STORAGE_PREFIX', 'ingresos')

# Catalogo de repuestos (listado unificado: codigo/descripcion/proveedor)
REPUESTOS_UNIFICADOS_FILE = os.getenv(
    "REPUESTOS_UNIFICADOS_FILE",
    os.path.join(REPUESTOS_COSTOS_ROOT, "repuestos_unificados.xlsx")
)

# Sincronización con Bejerman SDK
BEJERMAN_SYNC_ENABLED = _bool_env("BEJERMAN_SYNC_ENABLED", "0")
BEJERMAN_WSDL_URL = os.getenv("BEJERMAN_WSDL_URL", "")
BEJERMAN_USER = os.getenv("BEJERMAN_USER", "")
BEJERMAN_PASSWORD = os.getenv("BEJERMAN_PASSWORD", "")
BEJERMAN_COMPANY = os.getenv("BEJERMAN_COMPANY", "")
BEJERMAN_COMPANY_SEPID = os.getenv("BEJERMAN_COMPANY_SEPID", "SEP")
BEJERMAN_COMPANY_MGBIO = os.getenv("BEJERMAN_COMPANY_MGBIO", "MGBI")
BEJERMAN_COMPANY_TEST = os.getenv("BEJERMAN_COMPANY_TEST", "SEP")
BEJERMAN_WORKSTATION = os.getenv("BEJERMAN_WORKSTATION", "")
BEJERMAN_SERVICE_WORKSTATION = os.getenv("BEJERMAN_SERVICE_WORKSTATION", BEJERMAN_WORKSTATION or "STEC")
BEJERMAN_ADMIN_WORKSTATION = os.getenv("BEJERMAN_ADMIN_WORKSTATION", BEJERMAN_WORKSTATION or "ADMV")
BEJERMAN_BRANCH = os.getenv("BEJERMAN_BRANCH", "")
BEJERMAN_SOURCE_DEPOSIT = os.getenv("BEJERMAN_SOURCE_DEPOSIT", "STR")
BEJERMAN_TARGET_DEPOSIT = os.getenv("BEJERMAN_TARGET_DEPOSIT", "STL")
BEJERMAN_CLIENT_TARGET_DEPOSIT = os.getenv("BEJERMAN_CLIENT_TARGET_DEPOSIT", "STC")
BEJERMAN_NUMERA_FLEX = os.getenv("BEJERMAN_NUMERA_FLEX", "S")
BEJERMAN_STOCK_NUMERA_FLEX = os.getenv("BEJERMAN_STOCK_NUMERA_FLEX", "N")
BEJERMAN_STOCK_ENTRY_COMPROBANTE = os.getenv("BEJERMAN_STOCK_ENTRY_COMPROBANTE", "ENT")
if BEJERMAN_STOCK_ENTRY_COMPROBANTE.strip().upper() == "RIS":
    BEJERMAN_STOCK_ENTRY_COMPROBANTE = "ENT"
BEJERMAN_STOCK_EXIT_COMPROBANTE = os.getenv("BEJERMAN_STOCK_EXIT_COMPROBANTE", "SAL")
if BEJERMAN_STOCK_EXIT_COMPROBANTE.strip().upper() == "RSS":
    BEJERMAN_STOCK_EXIT_COMPROBANTE = "SAL"
BEJERMAN_STOCK_TRANSFER_COMPROBANTE = os.getenv(
    "BEJERMAN_STOCK_TRANSFER_COMPROBANTE",
    os.getenv("BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION", "TRA"),
)
BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE = os.getenv("BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE", "SAL")
BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE = os.getenv("BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE", "ENT")
BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION = os.getenv("BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION", "")
BEJERMAN_STOCK_BAJA_TARGET_DEPOSIT = os.getenv("BEJERMAN_STOCK_BAJA_TARGET_DEPOSIT", "DES")
BEJERMAN_STOCK_BAJA_CAUSA_EMISION = os.getenv("BEJERMAN_STOCK_BAJA_CAUSA_EMISION", "DES")
BEJERMAN_STOCK_BAJA_OUT_COMPROBANTE = os.getenv("BEJERMAN_STOCK_BAJA_OUT_COMPROBANTE", "SAL")
BEJERMAN_STOCK_BAJA_IN_COMPROBANTE = os.getenv("BEJERMAN_STOCK_BAJA_IN_COMPROBANTE", "ENT")
BEJERMAN_STOCK_ALTA_SOURCE_DEPOSIT = os.getenv("BEJERMAN_STOCK_ALTA_SOURCE_DEPOSIT", "DES")
BEJERMAN_STOCK_ALTA_TARGET_DEPOSIT = os.getenv("BEJERMAN_STOCK_ALTA_TARGET_DEPOSIT", "STR")
BEJERMAN_STOCK_ALTA_CAUSA_EMISION = os.getenv("BEJERMAN_STOCK_ALTA_CAUSA_EMISION", "ALQ")
BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE = os.getenv("BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE", "SAL")
BEJERMAN_STOCK_ALTA_IN_COMPROBANTE = os.getenv("BEJERMAN_STOCK_ALTA_IN_COMPROBANTE", "ENT")
BEJERMAN_STOCK_CORRECTION_COMPROBANTE = os.getenv("BEJERMAN_STOCK_CORRECTION_COMPROBANTE", "SAL")
BEJERMAN_STOCK_CORRECTION_CAUSA_EMISION = os.getenv("BEJERMAN_STOCK_CORRECTION_CAUSA_EMISION", "ERR")
BEJERMAN_ARTICLE_AUTO_MATCH = _bool_env("BEJERMAN_ARTICLE_AUTO_MATCH", "1")
BEJERMAN_MAX_ATTEMPTS = int(os.getenv("BEJERMAN_MAX_ATTEMPTS", "8"))
BEJERMAN_REQUEST_TIMEOUT = int(os.getenv("BEJERMAN_REQUEST_TIMEOUT", "30"))
# Timeout más corto para los endpoints interactivos (buscador de artículos, stock
# de una partida): ahí el usuario está esperando la respuesta y colgarse 30s
# retiene un thread de gunicorn y una transacción de Postgres abiertos al pedo.
# Con el cache de abajo la descarga completa pasa a ser esporádica, así que se
# deja margen para que la primera (fría) igual llegue a completarse.
BEJERMAN_INTERACTIVE_REQUEST_TIMEOUT = int(os.getenv("BEJERMAN_INTERACTIVE_REQUEST_TIMEOUT", "20"))
# TTL de los volcados completos que el SDK devuelve sin filtrar (ver
# service/bejerman_catalog_cache.py). 0 desactiva el cache.
BEJERMAN_ARTICLES_CACHE_SECONDS = int(os.getenv("BEJERMAN_ARTICLES_CACHE_SECONDS", "300"))
BEJERMAN_PARTIDAS_CACHE_SECONDS = int(os.getenv("BEJERMAN_PARTIDAS_CACHE_SECONDS", "60"))
BEJERMAN_STOCK_CACHE_SECONDS = int(os.getenv("BEJERMAN_STOCK_CACHE_SECONDS", "30"))
BEJERMAN_SYNC_TRANSIENT_MAX_ATTEMPTS = int(
    os.getenv("BEJERMAN_SYNC_TRANSIENT_MAX_ATTEMPTS", "12")
)
BEJERMAN_SYNC_TRANSIENT_RETRY_BASE_SECONDS = int(
    os.getenv("BEJERMAN_SYNC_TRANSIENT_RETRY_BASE_SECONDS", "300")
)
BEJERMAN_SYNC_TRANSIENT_REOPEN_INTERVAL_SECONDS = int(
    os.getenv("BEJERMAN_SYNC_TRANSIENT_REOPEN_INTERVAL_SECONDS", "300")
)
BEJERMAN_SYNC_TRANSIENT_REOPEN_COOLDOWN_SECONDS = int(
    os.getenv("BEJERMAN_SYNC_TRANSIENT_REOPEN_COOLDOWN_SECONDS", "1800")
)
BEJERMAN_SYNC_TRANSIENT_REOPEN_MAX_CYCLES = int(
    os.getenv("BEJERMAN_SYNC_TRANSIENT_REOPEN_MAX_CYCLES", "2")
)
BEJERMAN_SYNC_STOCK_VERIFY_ATTEMPTS = int(
    os.getenv("BEJERMAN_SYNC_STOCK_VERIFY_ATTEMPTS", "3")
)
BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS = int(
    os.getenv("BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS", "1500")
)
BEJERMAN_SYNC_BLOCKED_ALERT_ENABLED = _bool_env("BEJERMAN_SYNC_BLOCKED_ALERT_ENABLED", "1")
BEJERMAN_SYNC_BLOCKED_ALERT_AFTER_HOURS = int(
    os.getenv("BEJERMAN_SYNC_BLOCKED_ALERT_AFTER_HOURS", "2")
)
BEJERMAN_SYNC_BLOCKED_ALERT_INTERVAL_SECONDS = int(
    os.getenv("BEJERMAN_SYNC_BLOCKED_ALERT_INTERVAL_SECONDS", "900")
)
BEJERMAN_CUSTOMER_RESOLUTION_RETRY_DELAY_SECONDS = float(
    os.getenv("BEJERMAN_CUSTOMER_RESOLUTION_RETRY_DELAY_SECONDS", "2")
)
# Habilita/deshabilita la ficha del particular en el maestro de Bejerman segun tenga o no equipos
# abiertos. Escribe en un ERP productivo y no hay sandbox: apagado por defecto, se prende por env.
BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED = _bool_env(
    "BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED", "0"
)
BEJERMAN_SALE_ITEMS_LOOP_INTERVAL_SECONDS = int(
    os.getenv("BEJERMAN_SALE_ITEMS_LOOP_INTERVAL_SECONDS", "21600")
)
BEJERMAN_SALE_ITEMS_STARTUP_DELAY_SECONDS = int(
    os.getenv("BEJERMAN_SALE_ITEMS_STARTUP_DELAY_SECONDS", "300")
)
BEJERMAN_SALE_ITEMS_SYNC_DAYS = int(os.getenv("BEJERMAN_SALE_ITEMS_SYNC_DAYS", "2"))
BEJERMAN_SALE_ITEMS_SYNC_MAX_COMPROBANTES = int(
    os.getenv("BEJERMAN_SALE_ITEMS_SYNC_MAX_COMPROBANTES", "100")
)
BEJERMAN_SALE_ITEMS_LIST_MAX_COMPROBANTES = int(
    os.getenv("BEJERMAN_SALE_ITEMS_LIST_MAX_COMPROBANTES", "5000")
)
BEJERMAN_SALE_ITEMS_SYNC_CHUNK_DAYS = int(
    os.getenv("BEJERMAN_SALE_ITEMS_SYNC_CHUNK_DAYS", "31")
)
BEJERMAN_SALE_ITEMS_REFRESH_DAYS = int(os.getenv("BEJERMAN_SALE_ITEMS_REFRESH_DAYS", "2"))
BEJERMAN_SALE_MAX_ARTICLE_LINES = int(os.getenv("BEJERMAN_SALE_MAX_ARTICLE_LINES", "10"))
BEJERMAN_SALE_MAX_PRINT_LINES = int(os.getenv("BEJERMAN_SALE_MAX_PRINT_LINES", "22"))
BEJERMAN_RSS_MAX_PRINT_LINES = int(os.getenv("BEJERMAN_RSS_MAX_PRINT_LINES", "22"))
BEJERMAN_RSS_PRINT_LINES_PER_ITEM = int(os.getenv("BEJERMAN_RSS_PRINT_LINES_PER_ITEM", "3"))
BEJERMAN_REMITO_MAX_PRINT_LINES = int(os.getenv("BEJERMAN_REMITO_MAX_PRINT_LINES", "21"))
BEJERMAN_SDK_RACE_RETRY_ATTEMPTS = int(os.getenv("BEJERMAN_SDK_RACE_RETRY_ATTEMPTS", "2"))
BEJERMAN_SDK_RACE_RETRY_DELAY_MS = int(os.getenv("BEJERMAN_SDK_RACE_RETRY_DELAY_MS", "900"))
BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT = int(os.getenv("BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT", "4"))
BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS = int(os.getenv("BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS", "1"))
BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS = int(os.getenv("BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS", "0"))
BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS = int(os.getenv("BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS", "1200"))
BEJERMAN_PDF_GENERATION_CONNECT_TIMEOUT = int(os.getenv("BEJERMAN_PDF_GENERATION_CONNECT_TIMEOUT", "10"))
BEJERMAN_PDF_GENERATION_READ_TIMEOUT = int(os.getenv("BEJERMAN_PDF_GENERATION_READ_TIMEOUT", "90"))
BEJERMAN_PDF_SDK_READ_TIMEOUT = int(os.getenv("BEJERMAN_PDF_SDK_READ_TIMEOUT", "30"))
BEJERMAN_PDF_SDK_CHECK_SECONDS = int(os.getenv("BEJERMAN_PDF_SDK_CHECK_SECONDS", "5"))
BEJERMAN_PDF_WAIT_DEADLINE_SECONDS = int(os.getenv("BEJERMAN_PDF_WAIT_DEADLINE_SECONDS", "600"))
BEJERMAN_PDF_FOLDER_CHECK_SECONDS = int(os.getenv("BEJERMAN_PDF_FOLDER_CHECK_SECONDS", "5"))
BEJERMAN_PDF_COPY_RETRY_SECONDS = int(os.getenv("BEJERMAN_PDF_COPY_RETRY_SECONDS", "10"))
# Ciclos de generación adicionales que el worker puede abrir solo cuando un PDF no
# llegó. Sin esto una sola generación fallida dejaba el comprobante sin PDF para
# siempre, porque no había ningún camino de reintento.
BEJERMAN_PDF_MAX_REGENERATIONS = int(os.getenv("BEJERMAN_PDF_MAX_REGENERATIONS", "2"))
# Que el PDF de los remitos lo genere NEXORA replicando el del ERP. A Bejerman se le
# pide el número (es el maestro de la numeración) y, con ese número, NEXORA arma el
# papel: el servicio de PDF del ERP no lo entrega desde el 20/07. Sólo aplica a
# remitos; las facturas se siguen pidiendo a Bejerman.
# Arranca apagado a propósito — encenderlo después de comprobar el calce sobre el
# papel preimpreso de cada empresa, que usan formularios distintos.
BEJERMAN_REMITO_PDF_ENABLED = _bool_env("BEJERMAN_REMITO_PDF_ENABLED", "0")
# Nombre anterior, de cuando esto era sólo un último recurso. Se sigue leyendo para no
# romper los entornos que ya lo tienen configurado.
BEJERMAN_REMITO_PDF_FALLBACK_ENABLED = _bool_env("BEJERMAN_REMITO_PDF_FALLBACK_ENABLED", "0")
# Directorio con cour.ttf/courbd.ttf para embeber la Courier New real. Sin esto se
# usa la Courier de ReportLab, que tiene el mismo avance (0.6 em) y cae en la misma
# posición; sólo cambian los trazos de los glifos.
BEJERMAN_REMITO_PDF_FONT_DIR = os.getenv("BEJERMAN_REMITO_PDF_FONT_DIR", "")
# El worker declara su propia ventana de validez: el peor tick legítimo ronda los
# 220s (SSH 20 + register 100 + generación 100), así que la API no puede adivinar
# un umbral propio sin dar falsos "worker muerto".
BEJERMAN_PDF_WORKER_HEARTBEAT_TTL_SECONDS = int(os.getenv("BEJERMAN_PDF_WORKER_HEARTBEAT_TTL_SECONDS", "300"))
BEJERMAN_PDF_WORKER_STALL_GRACE_SECONDS = int(os.getenv("BEJERMAN_PDF_WORKER_STALL_GRACE_SECONDS", "90"))
BEJERMAN_PDF_DEADLINE_GRACE_SECONDS = int(os.getenv("BEJERMAN_PDF_DEADLINE_GRACE_SECONDS", "120"))
# Reserva al reclamar una fila. Los estados baratos vuelven a estar disponibles
# enseguida; solo `queued` (que puede disparar una generación larga) paga los 2min.
BEJERMAN_PDF_CLAIM_LEASE_SECONDS = int(os.getenv("BEJERMAN_PDF_CLAIM_LEASE_SECONDS", "30"))
BEJERMAN_PDF_CLAIM_LEASE_QUEUED_SECONDS = int(os.getenv("BEJERMAN_PDF_CLAIM_LEASE_QUEUED_SECONDS", "120"))
BEJERMAN_PDF_FOLDER_LISTING_CACHE_SECONDS = int(os.getenv("BEJERMAN_PDF_FOLDER_LISTING_CACHE_SECONDS", "30"))
# Timeout de canal SFTP: sin esto, `timeout` de paramiko solo cubre el connect y una
# lectura colgada inmoviliza al worker con el lock global de generación tomado.
BEJERMAN_PDF_SFTP_CHANNEL_TIMEOUT = int(os.getenv("BEJERMAN_PDF_SFTP_CHANNEL_TIMEOUT", "60"))
BEJERMAN_PDF_FACTURAS_DIR = os.getenv("BEJERMAN_PDF_FACTURAS_DIR", "")
BEJERMAN_PDF_REMITOS_DIR = os.getenv("BEJERMAN_PDF_REMITOS_DIR", "")
BEJERMAN_PDF_SEPID_FACTURAS_DIR = os.getenv("BEJERMAN_PDF_SEPID_FACTURAS_DIR", "")
BEJERMAN_PDF_SEPID_REMITOS_DIR = os.getenv("BEJERMAN_PDF_SEPID_REMITOS_DIR", "")
BEJERMAN_PDF_MGBIO_FACTURAS_DIR = os.getenv("BEJERMAN_PDF_MGBIO_FACTURAS_DIR", "")
BEJERMAN_PDF_MGBIO_REMITOS_DIR = os.getenv("BEJERMAN_PDF_MGBIO_REMITOS_DIR", "")
BEJERMAN_PDF_WINDOWS_PATH_MOUNTS = os.getenv(
    "BEJERMAN_PDF_WINDOWS_PATH_MOUNTS",
    r"C=samtronic:C:\,Z=/mnt/nexora-z",
)
BEJERMAN_PDF_SAMTRONIC_SSH_HOST = os.getenv("BEJERMAN_PDF_SAMTRONIC_SSH_HOST", os.getenv("BEJERMAN_SSH_HOST", "45.173.2.155"))
BEJERMAN_PDF_SAMTRONIC_SSH_PORT = os.getenv("BEJERMAN_PDF_SAMTRONIC_SSH_PORT", os.getenv("BEJERMAN_SSH_PORT", "22"))
BEJERMAN_PDF_SAMTRONIC_SSH_USER = os.getenv("BEJERMAN_PDF_SAMTRONIC_SSH_USER", os.getenv("BEJERMAN_SSH_USER", "administrator"))
BEJERMAN_PDF_SAMTRONIC_SSH_PASSWORD = os.getenv("BEJERMAN_PDF_SAMTRONIC_SSH_PASSWORD", os.getenv("BEJERMAN_SSH_PASSWORD", ""))
BEJERMAN_PDF_SAMTRONIC_FACTURAS_DIR = os.getenv("BEJERMAN_PDF_SAMTRONIC_FACTURAS_DIR", r"C:\2. SEPID\PDF\FACTURAS")
BEJERMAN_PDF_CLEAN_REMOTE_REMITOS_FROM_FACTURAS = _bool_env("BEJERMAN_PDF_CLEAN_REMOTE_REMITOS_FROM_FACTURAS", "1")
BEJERMAN_REMITO_CURRENCY = os.getenv("BEJERMAN_REMITO_CURRENCY", "")
BEJERMAN_REMITO_CURRENCY_ARS = os.getenv("BEJERMAN_REMITO_CURRENCY_ARS", BEJERMAN_REMITO_CURRENCY)
BEJERMAN_REMITO_CURRENCY_USD = os.getenv("BEJERMAN_REMITO_CURRENCY_USD", "2")
BEJERMAN_REMITO_EXCHANGE_TYPE = os.getenv("BEJERMAN_REMITO_EXCHANGE_TYPE", "")
BEJERMAN_REMITO_EXCHANGE_TYPE_ARS = os.getenv("BEJERMAN_REMITO_EXCHANGE_TYPE_ARS", BEJERMAN_REMITO_EXCHANGE_TYPE)
BEJERMAN_REMITO_EXCHANGE_TYPE_USD = os.getenv(
    "BEJERMAN_REMITO_EXCHANGE_TYPE_USD",
    BEJERMAN_REMITO_EXCHANGE_TYPE or "DVE",
)
BEJERMAN_REMITO_EXCHANGE_RATE = float(os.getenv("BEJERMAN_REMITO_EXCHANGE_RATE", "0") or "0")
BEJERMAN_PURCHASE_NUMERA_FLEX = (os.getenv("BEJERMAN_PURCHASE_NUMERA_FLEX", "N") or "N").strip().upper() or "N"
BEJERMAN_PURCHASE_EMITE_REG = (os.getenv("BEJERMAN_PURCHASE_EMITE_REG", "R") or "R").strip().upper() or "R"
BEJERMAN_PURCHASE_CURRENCY = (os.getenv("BEJERMAN_PURCHASE_CURRENCY", "1") or "1").strip() or "1"
BEJERMAN_PURCHASE_EXCHANGE_TYPE = (
    os.getenv("BEJERMAN_PURCHASE_EXCHANGE_TYPE", "UNI") or "UNI"
).strip().upper() or "UNI"
BEJERMAN_PURCHASE_EXCHANGE_RATE = _float_env("BEJERMAN_PURCHASE_EXCHANGE_RATE", 1)
BEJERMAN_PURCHASE_VAT_RATE_CODES = (
    os.getenv("BEJERMAN_PURCHASE_VAT_RATE_CODES", "01:21,03:10.5,04:0") or ""
).strip()
BEJERMAN_PURCHASE_EMISSION_ENABLED = _bool_env("BEJERMAN_PURCHASE_EMISSION_ENABLED", "0")
BEJERMAN_PURCHASE_CATALOG_CACHE_SECONDS = _int_env("BEJERMAN_PURCHASE_CATALOG_CACHE_SECONDS", 30)
BEJERMAN_PURCHASE_PROVIDER_CACHE_SECONDS = _int_env("BEJERMAN_PURCHASE_PROVIDER_CACHE_SECONDS", 60)
BEJERMAN_PURCHASE_DEPOSIT_CACHE_SECONDS = _int_env("BEJERMAN_PURCHASE_DEPOSIT_CACHE_SECONDS", 60)
BEJERMAN_PURCHASE_PARTIDA_CACHE_SECONDS = _int_env("BEJERMAN_PURCHASE_PARTIDA_CACHE_SECONDS", 60)
BEJERMAN_PURCHASE_MAX_BULK_SCANS = _int_env("BEJERMAN_PURCHASE_MAX_BULK_SCANS", 5000)
BEJERMAN_PURCHASE_RUNNING_GRACE_SECONDS = _int_env("BEJERMAN_PURCHASE_RUNNING_GRACE_SECONDS", 120)
BEJERMAN_PURCHASE_QUANTITY_MISMATCH_THRESHOLD = os.getenv("BEJERMAN_PURCHASE_QUANTITY_MISMATCH_THRESHOLD", "0.2")
BEJERMAN_RIS_TYPE = os.getenv("BEJERMAN_RIS_TYPE", "RIS")
BEJERMAN_RIS_LETTER = os.getenv("BEJERMAN_RIS_LETTER", "R")
BEJERMAN_RIS_POINT_OF_SALE = os.getenv("BEJERMAN_RIS_POINT_OF_SALE", "00004")
BEJERMAN_RIS_OPERATION = os.getenv("BEJERMAN_RIS_OPERATION", "IngresarComprobanteJSON")
BEJERMAN_RIS_SERVICE_OPERATION = os.getenv("BEJERMAN_RIS_SERVICE_OPERATION", "REP")
BEJERMAN_RIS_DEPOSIT = os.getenv("BEJERMAN_RIS_DEPOSIT", "STR")
BEJERMAN_RIS_NUMERA_FLEX = os.getenv("BEJERMAN_RIS_NUMERA_FLEX", "S")
BEJERMAN_RIS_EMITE_REG = os.getenv("BEJERMAN_RIS_EMITE_REG", "E")
BEJERMAN_RIS_PRICE_LIST = os.getenv("BEJERMAN_RIS_PRICE_LIST", "GN")
BEJERMAN_RIS_CURRENCY = os.getenv("BEJERMAN_RIS_CURRENCY", "")
BEJERMAN_RIS_EXCHANGE_TYPE = os.getenv("BEJERMAN_RIS_EXCHANGE_TYPE", "")
BEJERMAN_RIS_EXCHANGE_RATE = float(os.getenv("BEJERMAN_RIS_EXCHANGE_RATE", "0") or "0")
BEJERMAN_RIS_UPDATE_STOCK = _bool_env("BEJERMAN_RIS_UPDATE_STOCK", "0")
BEJERMAN_RIS_RENTAL_RETURN_TYPE = os.getenv("BEJERMAN_RIS_RENTAL_RETURN_TYPE", "RDA")
BEJERMAN_RIS_RENTAL_RETURN_POINT_OF_SALE = os.getenv("BEJERMAN_RIS_RENTAL_RETURN_POINT_OF_SALE", "00004")
BEJERMAN_RIS_RENTAL_RETURN_OPERATION = os.getenv("BEJERMAN_RIS_RENTAL_RETURN_OPERATION", "ALQ")
BEJERMAN_RIS_RENTAL_RETURN_DEPOSIT = os.getenv("BEJERMAN_RIS_RENTAL_RETURN_DEPOSIT", "STR")
BEJERMAN_RIS_RENTAL_RETURN_UPDATE_STOCK = _bool_env("BEJERMAN_RIS_RENTAL_RETURN_UPDATE_STOCK", "1")
BEJERMAN_RIS_DEMO_RETURN_TYPE = os.getenv("BEJERMAN_RIS_DEMO_RETURN_TYPE", "RDN")
BEJERMAN_RIS_DEMO_RETURN_POINT_OF_SALE = os.getenv("BEJERMAN_RIS_DEMO_RETURN_POINT_OF_SALE", "00004")
BEJERMAN_RIS_DEMO_RETURN_OPERATION = os.getenv("BEJERMAN_RIS_DEMO_RETURN_OPERATION", "DEMO")
BEJERMAN_RIS_DEMO_RETURN_DEPOSIT = os.getenv("BEJERMAN_RIS_DEMO_RETURN_DEPOSIT", "STR")
BEJERMAN_RIS_DEMO_RETURN_UPDATE_STOCK = _bool_env("BEJERMAN_RIS_DEMO_RETURN_UPDATE_STOCK", "1")
BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES = os.getenv("BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES", "")
BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES = os.getenv("BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES", "")
BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES = os.getenv("BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES", "")
BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES = os.getenv("BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES", "RD,RDN,RDA")
BEJERMAN_DEMO_TARGET_DEPOSIT = os.getenv("BEJERMAN_DEMO_TARGET_DEPOSIT", "VAL")
BEJERMAN_RIS_SELLER_CODE = os.getenv("BEJERMAN_RIS_SELLER_CODE", "ADM")
BEJERMAN_RIS_PAYMENT_TERM = os.getenv("BEJERMAN_RIS_PAYMENT_TERM", "30")
BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE = _bool_env("BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE", "0")
BEJERMAN_RIS_GENERIC_ARTICLE_CODE = os.getenv("BEJERMAN_RIS_GENERIC_ARTICLE_CODE", "SERVICIO")
BEJERMAN_RIS_GENERIC_ARTICLE_NAME = os.getenv(
    "BEJERMAN_RIS_GENERIC_ARTICLE_NAME",
    "Equipo recibido para servicio técnico",
)


# --- Seguridad / Autenticación (vistas) ---
# TTL de tokens de restablecimiento (minutos)
TOKEN_TTL_MIN = int(os.getenv("TOKEN_TTL_MIN", "30"))

# Cooldown para envío de correos repetidos (minutos)
EMAIL_COOLDOWN_MIN = int(os.getenv("EMAIL_COOLDOWN_MIN", "1"))

# Intentos máximos de login y bloqueo temporal
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "5"))
LOGIN_LOCKOUT_SECONDS = max(1, LOGIN_LOCKOUT_MINUTES) * 60

# Requisito mínimo local de longitud de contraseña (además de validators si aplica)
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))

# Cookies de autenticación (para JWT en cookie)
# Por default el login devuelve token en el body y TAMBIÉN lo setea en cookie
# Si el front consume por cross-origin, usar: AUTH_COOKIE_SAMESITE=None y AUTH_COOKIE_SECURE=True (requiere HTTPS)
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "auth_token")
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "Lax")  # Lax | Strict | None
# Si no se define, se toma según DEBUG
_cookie_secure_env = os.getenv("AUTH_COOKIE_SECURE", "")
AUTH_COOKIE_SECURE = (not DEBUG) if _cookie_secure_env == "" else (_cookie_secure_env.lower() in ("1","true","yes"))
AUTH_COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN", "") or None
