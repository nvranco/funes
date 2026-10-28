"""LIBRERO — esqueleto Día 1.

Un solo servicio FastAPI: backend + templates server-side. Sin build step,
sin Node (ver stack, requisitos §8).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from funes import db
from funes.config import DOMINIO_FUNES
from funes.routers import admin, api_librero, api_publico, funes_chat, panel, publico, qr, registro

# Los logs de funes.vision (latencia/tokens/respuesta cruda del modelo) son el
# baseline de calidad y de unit economics del pipeline (requisitos §7 y §9).
# Sin nivel INFO explicito, Uvicorn los descarta antes de que lleguen a Railway.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.conectar()
    yield
    await db.cerrar()


app = FastAPI(title="Funes", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="funes/static"), name="static")

templates = Jinja2Templates(directory="funes/templates")


# Todo lo que se pide por GET a un host que no es DOMINIO_FUNES se muda a ese
# dominio: las direcciones viejas (*.up.railway.app) ya estan repartidas en
# links, QR impresos y paginas instaladas como app, y tienen que seguir llevando
# a donde corresponde. Lo que NO se muda, y por que:
#
#   - Todo lo que no es GET (los POST del chat y de la API, PATCH, DELETE). Una
#     pestaña abierta desde antes del cambio sigue posteando al host viejo;
#     mandarla al nuevo convierte esos fetch en cross-origin, el navegador pide
#     CORS, no hay, y la conversacion o la carga de libros se corta a la mitad.
#     Contestando en los dos lados no se corta nada, y la proxima visita ya
#     entra por el dominio nuevo sola.
#   - /health: el healthcheck de Railway llega con su propio Host, y un 302 lo
#     da por caido.
#   - /api/ y /static/: son pedidos de una pagina que ya estaba abierta en el
#     host viejo, por la misma razon cross-origin de arriba.
#   - /funes/ml/...: /funes/ml/callback quedo registrado en la consola de
#     MercadoLibre y el canje del token exige que coincida exacto.
#   - /funes/admin/...: no se comparte con nadie, y ahi viven el tablero del
#     piloto y el DELETE de una conversacion.
_RUTAS_QUE_NO_SE_MUDAN = ("/health", "/api/", "/static/", "/funes/ml/", "/funes/admin/")


def destino_de_funes(host: str, metodo: str, camino: str, query: str = "") -> str | None:
    """A que URL hay que mudar este pedido, o None si se atiende donde esta.

    Aparte del middleware para poder probarla sin levantar nada: es una regla de
    ruteo en produccion que, si se equivoca, no rompe una pagina sino la
    medicion del piloto, y eso no se ve mirando la pantalla.

    La query se conserva SIEMPRE, y por eso se calcula aca adentro y no afuera.
    No es cosmetico: ahi viaja el ?src= que dice por donde llego la persona, y
    es lo unico con lo que el piloto separa a un amigo de un desconocido. Un
    redirect que la pierde no rompe nada visible y arruina la unica medicion que
    despues no se puede reconstruir.
    """
    if not DOMINIO_FUNES:
        return None
    if (host or "").lower() == DOMINIO_FUNES:
        # Nada se mueve dentro del propio dominio. Sin este return temprano,
        # cualquier pedido en el propio host se redirigiria a si mismo (loop).
        return None
    if metodo != "GET":
        return None
    sin_barra = camino.rstrip("/") or "/"
    # El camino se compara con barra final para que "/api" tambien quede afuera.
    if (sin_barra + "/").startswith(_RUTAS_QUE_NO_SE_MUDAN) or sin_barra == "/health":
        return None
    cola = f"?{query}" if query else ""
    return f"https://{DOMINIO_FUNES}{sin_barra}{cola}"


@app.middleware("http")
async def dominio_de_funes(request: Request, call_next):
    """Manda los GET de los hosts viejos al dominio propio de Funes.

    302 y no 301: el 301 se lo guarda el navegador para siempre, y mientras el
    piloto se este moviendo conviene poder volver atras cambiando una variable
    en vez de pedirle a la gente que limpie el cache.

    Toda la decision vive en destino_de_funes(), que no depende de FastAPI y por
    eso se puede probar en el banco.
    """
    destino = destino_de_funes(
        request.url.hostname or "", request.method,
        request.url.path, request.url.query)
    if destino is None:
        return await call_next(request)
    return RedirectResponse(destino, status_code=302)


@app.get("/health")
async def health():
    await db.pool().fetchval("SELECT 1")
    return {"ok": True}


# Orden importante: publico.router define GET /{slug}, un catch-all de UN solo
# segmento. Starlette matchea rutas en el orden en que se agregan, asi que
# cualquier ruta literal de un segmento (como /health) tiene que registrarse
# ANTES de incluir este router, o el catch-all se la come primero.
app.include_router(admin.router)
app.include_router(registro.router)
# El panel y su API cuelgan de dos puertas: sesion de Clerk (/app/{slug}) y el
# link secreto de siempre (ver funes/acceso.py). Primero la nueva: sin eso,
# /api/app/{slug}/lotes lo agarraria la vieja como slug="app", token=<slug>.
app.include_router(api_librero.router, prefix="/api/app/{slug}")
app.include_router(api_librero.router, prefix="/api/{slug}/{token}")
app.include_router(api_publico.router)
app.include_router(funes_chat.router)
app.include_router(panel.router, prefix="/app/{slug}")
app.include_router(panel.router, prefix="/{slug}/panel/{token}")
app.include_router(qr.router)
app.include_router(publico.router)


@app.exception_handler(404)
async def no_encontrado(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"error": "no encontrado"})
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
