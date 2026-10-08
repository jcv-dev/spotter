"""Shared HTTP session with connection pooling for the map providers.

A cold route request makes up to three provider calls (geocoding the start and
finish, then the directions call). Reusing a single :class:`requests.Session`
keeps the TCP/TLS connection alive between them: a fresh TLS handshake to the
ORS host measured ~0.36 s, which would otherwise be paid on every call.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

#: urllib3 connection-pool size; enough for gunicorn's threaded workers.
_POOL_SIZE = 16

session = requests.Session()
_adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
session.mount("http://", _adapter)
session.mount("https://", _adapter)
