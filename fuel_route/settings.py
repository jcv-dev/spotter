"""
Django settings for the fuel_route project.

Configuration is environment driven (12-factor style): every value can be
provided through environment variables or a local `.env` file.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    SECRET_KEY=(str, "django-insecure-dev-only-change-me"),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    DATABASE_URL=(str, "postgres://fuel:fuel@localhost:5432/fuel_route"),
    REDIS_URL=(str, ""),
    OPENROUTESERVICE_API_KEY=(str, ""),
    # The old api.openrouteservice.org URL is deprecated (reduced quota);
    # HeiGIT's current API is api.heigit.org.
    OPENROUTESERVICE_BASE_URL=(str, "https://api.heigit.org"),
    # Map providers: ors (default) | osrm/nominatim (free, keyless) | auto.
    ROUTING_PROVIDER=(str, "ors"),
    GEOCODING_PROVIDER=(str, "ors"),
    OSRM_BASE_URL=(str, "https://router.project-osrm.org"),
    NOMINATIM_BASE_URL=(str, "https://nominatim.openstreetmap.org"),
    NOMINATIM_REQUEST_INTERVAL=(float, 1.1),
    MAPS_USER_AGENT=(str, "fuel-route-planner/1.0 (assessment demo)"),
    ORS_TIMEOUT_SECONDS=(float, 20.0),
    ORS_REQUEST_INTERVAL=(float, 0.55),
    ROUTE_CACHE_TTL=(int, 3600),
    GEOCODE_CACHE_TTL=(int, 86400),
    GEOCODE_CITY_MAX_DISTANCE_MILES=(float, 15.0),
    TANK_CAPACITY_GALLONS=(float, 50.0),
    MILES_PER_GALLON=(float, 10.0),
    FUEL_STATION_RADIUS_MILES=(float, 10.0),
    FUEL_GRID_STEP_GALLONS=(float, 0.5),
)
# Read the local .env file if present (does not override real env vars).
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# Tell Django it is behind a TLS-terminating proxy (Dokploy/Traefik).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "fuel.middleware.ResponseTimeMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fuel_route.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "fuel_route.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Caching: Redis when REDIS_URL is set, otherwise per-process local memory.
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuel-route",
        }
    }

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# ---------------------------------------------------------------------------
# Fuel route domain settings
# ---------------------------------------------------------------------------
ORS_BASE_URL = env("OPENROUTESERVICE_BASE_URL")
OPENROUTESERVICE_API_KEY = env("OPENROUTESERVICE_API_KEY")
ROUTING_PROVIDER = env("ROUTING_PROVIDER")
GEOCODING_PROVIDER = env("GEOCODING_PROVIDER")
OSRM_BASE_URL = env("OSRM_BASE_URL")
NOMINATIM_BASE_URL = env("NOMINATIM_BASE_URL")
NOMINATIM_REQUEST_INTERVAL = env("NOMINATIM_REQUEST_INTERVAL")
MAPS_USER_AGENT = env("MAPS_USER_AGENT")
ORS_TIMEOUT_SECONDS = env("ORS_TIMEOUT_SECONDS")
ORS_REQUEST_INTERVAL = env("ORS_REQUEST_INTERVAL")
ROUTE_CACHE_TTL = env("ROUTE_CACHE_TTL")
GEOCODE_CACHE_TTL = env("GEOCODE_CACHE_TTL")
GEOCODE_CITY_MAX_DISTANCE_MILES = env("GEOCODE_CITY_MAX_DISTANCE_MILES")

TANK_CAPACITY_GALLONS = env("TANK_CAPACITY_GALLONS")
MILES_PER_GALLON = env("MILES_PER_GALLON")
FUEL_STATION_RADIUS_MILES = env("FUEL_STATION_RADIUS_MILES")
FUEL_GRID_STEP_GALLONS = env("FUEL_GRID_STEP_GALLONS")

# The task requires both endpoints to be in the continental USA.
USA_LAT_MIN = 24.0
USA_LAT_MAX = 49.0
USA_LON_MIN = -125.0
USA_LON_MAX = -66.0

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "fuel": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
