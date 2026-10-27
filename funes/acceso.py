"""Quien puede tocar el panel de una libreria, por cualquiera de las dos puertas.

  - Link secreto (el de siempre): /{slug}/panel/{token} y /api/{slug}/{token}.
  - Sesion de Clerk (alta propia): /app/{slug} y /api/app/{slug}.

Los mismos handlers sirven las dos, montados bajo dos prefijos. Lo unico que
cambia es de donde sale la libreria y cuales son las raices de las URLs que los
templates arman, y eso lo resuelve esta dependencia.
"""

import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from funes import auth_clerk, db


@dataclass
class Acceso:
    libreria: object
    panel: str   # raiz de las paginas: /app/{slug} o /{slug}/panel/{token}
    api: str     # raiz de la API: /api/app/{slug} o /api/{slug}/{token}
    por_sesion: bool


async def acceso(request: Request, slug: str) -> Acceso:
    fila = await db.pool().fetchrow(
        "SELECT * FROM librerias WHERE slug = $1 AND activa", slug
    )
    token = request.path_params.get("token")
    if token is not None:
        if fila is None or not secrets.compare_digest(fila["token_panel"], token):
            raise HTTPException(status_code=404)
        panel, api, por_sesion = f"/{slug}/panel/{token}", f"/api/{slug}/{token}", False
    else:
        usuario = await auth_clerk.usuario_de(request)
        if fila is None or usuario is None or fila["clerk_user_id"] != usuario:
            if request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401)
            # Las paginas mandan a ingresar, que despues devuelve al panel.
            raise HTTPException(status_code=307, headers={"Location": "/ingresar"})
        panel, api, por_sesion = f"/app/{slug}", f"/api/app/{slug}", True
    # Los templates arman sus links desde aca (request.state), asi no hay que
    # pasar la raiz por cada contexto.
    request.state.panel_raiz = panel
    request.state.api_raiz = api
    request.state.por_sesion = por_sesion
    return Acceso(fila, panel, api, por_sesion)
