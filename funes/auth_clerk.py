"""Sesion de Clerk (login con Google) para el panel nuevo.

Clerk.js en el navegador deja la cookie `__session`, un JWT firmado por Clerk.
Aca se verifica contra las claves publicas de la Frontend API de la instancia,
asi que no hace falta ninguna secret key en el servidor: el host sale de la
publishable key y de ahi cuelga /.well-known/jwks.json.
"""

import base64
from functools import lru_cache

import jwt
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from funes import db
from funes.config import CLERK_PUBLISHABLE_KEY

# Margen para el reloj del navegador frente al del servidor (nbf/iat).
_TOLERANCIA_RELOJ_S = 10


def habilitado() -> bool:
    return bool(CLERK_PUBLISHABLE_KEY)


@lru_cache(maxsize=1)
def host_frontend_api() -> str:
    """pk_test_<base64 del host>$ -> el host de la Frontend API."""
    cuerpo = CLERK_PUBLISHABLE_KEY.split("_", 2)[-1]
    cuerpo += "=" * (-len(cuerpo) % 4)
    return base64.b64decode(cuerpo).decode().rstrip("$")


@lru_cache(maxsize=1)
def _claves() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://{host_frontend_api()}/.well-known/jwks.json", cache_keys=True)


def _verificar(token: str) -> str | None:
    try:
        clave = _claves().get_signing_key_from_jwt(token).key
        datos = jwt.decode(
            token, clave, algorithms=["RS256"],
            issuer=f"https://{host_frontend_api()}",
            options={"verify_aud": False}, leeway=_TOLERANCIA_RELOJ_S,
        )
    except jwt.PyJWTError:
        return None
    return datos.get("sub") or None


async def usuario_de(request: Request) -> str | None:
    """El id de usuario de Clerk de este pedido, o None si no hay sesion valida."""
    if not habilitado():
        return None
    token = request.cookies.get("__session")
    if not token:
        return None
    return await run_in_threadpool(_verificar, token)


async def libreria_de(request: Request):
    """La libreria del usuario logueado, o None (sin sesion o sin libreria)."""
    usuario = await usuario_de(request)
    if usuario is None:
        return None
    return await db.pool().fetchrow(
        "SELECT * FROM librerias WHERE clerk_user_id = $1 AND activa", usuario
    )
