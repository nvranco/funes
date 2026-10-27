"""Alta propia de una libreria: datos -> Google (Clerk) -> elegir Funes -> panel.

El paso 1 (/registro) es solo cliente: guarda el borrador en sessionStorage y
manda a Google. Al volver, /registro/continuar crea la libreria con ese
borrador, que el servidor vuelve a validar entero.
"""

import asyncpg
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from funes import auth_clerk, db
from funes.tokens import nuevo_token_panel, slugify

router = APIRouter()
templates = Jinja2Templates(directory="funes/templates")

# Primeros segmentos de URL que ya son de la app: una libreria con uno de estos
# slugs taparia (o seria tapada por) una ruta propia.
_SLUGS_RESERVADOS = {
    "app", "api", "admin", "funes", "static", "health", "registro", "ingresar",
    "sso-callback", "qr", "panel", "login", "salir",
}


def _contexto() -> dict:
    return {
        "clerk_clave": auth_clerk.CLERK_PUBLISHABLE_KEY,
        "clerk_host": auth_clerk.host_frontend_api(),
    }


def _exigir_clerk() -> None:
    if not auth_clerk.habilitado():
        raise HTTPException(status_code=404)


async def _slug_libre(nombre: str) -> str:
    base = slugify(nombre)
    if base in _SLUGS_RESERVADOS:
        base = f"{base}-libreria"
    candidato, n = base, 1
    while await db.pool().fetchval("SELECT 1 FROM librerias WHERE slug = $1", candidato):
        n += 1
        candidato = f"{base}-{n}"
    return candidato


@router.get("/registro", response_class=HTMLResponse)
async def registro_datos(request: Request):
    _exigir_clerk()
    propia = await auth_clerk.libreria_de(request)
    if propia is not None:
        return RedirectResponse(f"/app/{propia['slug']}", status_code=303)
    return templates.TemplateResponse(request, "registro.html", _contexto())


@router.get("/ingresar", response_class=HTMLResponse)
async def ingresar(request: Request):
    _exigir_clerk()
    propia = await auth_clerk.libreria_de(request)
    if propia is not None:
        return RedirectResponse(f"/app/{propia['slug']}", status_code=303)
    return templates.TemplateResponse(request, "ingresar.html", _contexto())


@router.get("/ingresar/destino")
async def ingresar_destino(request: Request):
    """Adonde cae alguien recien logueado: su panel, o el registro si todavia no tiene libreria."""
    _exigir_clerk()
    propia = await auth_clerk.libreria_de(request)
    if propia is not None:
        return RedirectResponse(f"/app/{propia['slug']}", status_code=303)
    if await auth_clerk.usuario_de(request) is None:
        return RedirectResponse("/ingresar", status_code=303)
    return RedirectResponse("/registro", status_code=303)


@router.get("/sso-callback", response_class=HTMLResponse)
async def sso_callback(request: Request):
    _exigir_clerk()
    return templates.TemplateResponse(request, "sso_callback.html", _contexto())


@router.get("/registro/continuar", response_class=HTMLResponse)
async def registro_continuar(request: Request):
    _exigir_clerk()
    if await auth_clerk.usuario_de(request) is None:
        return RedirectResponse("/registro", status_code=303)
    propia = await auth_clerk.libreria_de(request)
    if propia is not None:
        return RedirectResponse(f"/app/{propia['slug']}", status_code=303)
    return templates.TemplateResponse(request, "registro_continuar.html", {**_contexto(), "error": None})


@router.post("/registro/continuar", response_class=HTMLResponse)
async def registro_crear(
    request: Request,
    nombre: str = Form(""),
    whatsapp: str = Form(""),
    funes: str = Form("no"),
):
    _exigir_clerk()
    usuario = await auth_clerk.usuario_de(request)
    if usuario is None:
        return RedirectResponse("/registro", status_code=303)
    propia = await auth_clerk.libreria_de(request)
    if propia is not None:
        return RedirectResponse(f"/app/{propia['slug']}", status_code=303)

    nombre = " ".join(nombre.split())
    whatsapp = whatsapp.strip()
    error = None
    if not (2 <= len(nombre) <= 80):
        error = "Falta el nombre de tu librería."
    elif not whatsapp.isdigit() or not (10 <= len(whatsapp) <= 15):
        error = "El WhatsApp tiene que ser solo números, con código de país y de área (ej: 5491122334455)."
    if error:
        return templates.TemplateResponse(
            request, "registro_continuar.html", {**_contexto(), "error": error}, status_code=422
        )

    con_funes = funes == "si"
    for _ in range(5):
        slug = await _slug_libre(nombre)
        try:
            await db.pool().execute(
                """
                INSERT INTO librerias
                    (slug, nombre, whatsapp, token_panel,
                     funes_habilitado, clerk_user_id, onboarding_completo)
                VALUES ($1, $2, $3, $4, $5, $6, FALSE)
                """,
                slug, nombre, whatsapp, nuevo_token_panel(), con_funes, usuario,
            )
            return RedirectResponse(f"/app/{slug}", status_code=303)
        except asyncpg.UniqueViolationError:
            # Otro registro ganó el slug entre el chequeo y el insert, o este
            # usuario ya tenia libreria (doble click): en ese caso se lo lleva.
            propia = await auth_clerk.libreria_de(request)
            if propia is not None:
                return RedirectResponse(f"/app/{propia['slug']}", status_code=303)
    raise HTTPException(status_code=409, detail="No se pudo crear la librería.")
