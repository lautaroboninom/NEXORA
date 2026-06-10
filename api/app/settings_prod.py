from .settings import *  # noqa
import os

# Ajustes para despliegue estricto por dominio
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "nexora.tail7bb880.ts.net").split(",") if h.strip()]

_default_origin = "https://nexora.tail7bb880.ts.net"
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origin).split(",") if o.strip()]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

STATIC_ROOT = os.getenv("STATIC_ROOT", "/app/staticfiles")
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/app/media")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
