"""Local development settings.

CORS stays env-driven here too (``CORS_ALLOWED_ORIGINS``). Set
``CORS_ALLOW_ALL_ORIGINS=True`` in the environment only when you need a
throwaway wildcard during local hacking.
"""

from .base import *  # noqa: F401,F403
from .base import config

DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = config("CORS_ALLOW_ALL_ORIGINS", default=False, cast=bool)
