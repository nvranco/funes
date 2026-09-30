"""Redireccion publica de las carpitas de mesa: /qr/<token> -> el Funes de la
libreria vinculada a ese codigo (ver schema.sql:qr_codigos). El token es
opaco y no cambia nunca -lo que cambia es a que libreria apunta-, asi que la
misma carpita fisica se puede reciclar sin reimprimir con solo revincularla
desde el panel del librero o desde /admin.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.funes_chat import bitacora

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/qr/{token}")
async def qr_redirigir(request: Request, token: str):
    codigo = await db.pool().fetchrow(
        "SELECT libreria_id, destino_generico FROM qr_codigos WHERE token = $1", token
    )
    if codigo is None:
        raise HTTPException(status_code=404)

    base = str(request.base_url).rstrip("/")
    # La cohorte viaja en la redireccion. Sin esto cada scan de carpita
    # llegaba a /funes sin ?src= y la bitacora lo contaba como 'link', la
    # misma cohorte que un amigo que manda la URL. Default 'qr' (mostrador);
    # un poster imprime /qr/<token>?src=flyer y se separa solo.
    src = bitacora.normalizar_origen(request.query_params.get("src") or "qr")

    if codigo["libreria_id"] is None:
        if codigo["destino_generico"]:
            return RedirectResponse(
                f"{base}/funes?src={src}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        return templates.TemplateResponse(
            request, "qr_sin_vincular.html", {}, status_code=200
        )

    libreria = await db.pool().fetchrow(
        "SELECT slug FROM librerias WHERE id = $1 AND activa", codigo["libreria_id"]
    )
    if libreria is None:
        return templates.TemplateResponse(
            request, "qr_sin_vincular.html", {}, status_code=200
        )

    # 302, nunca 301: si el codigo se reasigna (reciclaje de la carpita
    # fisica), el proximo scan tiene que poder llevar a otro lado sin que el
    # celular del cliente haya cacheado el destino viejo. Cache-Control
    # explicito por lo mismo -302 ya no es cacheable por default, pero no
    # queda a criterio del navegador/proxy intermedio.
    return RedirectResponse(
        f"{base}/funes/{libreria['slug']}?src={src}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
