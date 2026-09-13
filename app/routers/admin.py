"""P5 — alta de librerías. Acceso: token en el path, comparado contra ADMIN_TOKEN.

Un token incorrecto devuelve 404, no 401: no queremos confirmarle a nadie que
la ruta existe (decisión D2, aplicada también acá).
"""

import secrets

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.config import ADMIN_TOKEN, MENSAJE_WA_DEFAULT
from app.metricas import calcular_metricas
from app.tokens import nuevo_token_panel, slugify

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _validar_token(token: str) -> None:
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=404)


# Toda columna que no este aca NO viaja en una sincronizacion, y en el destino
# queda como estaba (NULL en las nuevas). Mismo listado y misma advertencia que
# funes/migrar_catalogo_a_railway.py, del que este endpoint es la variante por
# HTTP: en vez de necesitar una URL de Postgres publica (un proxy TCP nuevo,
# expuesto a internet) para conectarse directo a la base de Railway, este
# endpoint corre DENTRO de la app en produccion y escribe en su propia base
# interna -el catalogo viaja como JSON sobre la conexion HTTPS que la app ya
# tiene, protegido por el mismo ADMIN_TOKEN que el resto de estas rutas.
COLUMNAS_CATALOGO = [
    "id", "titulo", "autor", "abstracto", "embedding_abstracto", "isbn", "fecha_publicacion",
    "categoria", "genero", "subgenero", "nro_paginas", "confianza_abstracto",
    "nota", "fuente", "macro", "macro_manual",
    "sinopsis", "experiencia", "embedding_sinopsis", "embedding_experiencia", "rasgos",
    "version_reescritura",
]


def _es_recorte_titulo(nuevo: str, viejo: str) -> bool:
    """Si `nuevo` es `viejo` al que le cortaron el final — mismo guardian que
    el script de migracion: un titulo curado a mano en destino ("DeMente. El
    cerebro, un hueso duro de roer") no se deja pisar por una version scrapeada
    y truncada ("Demente") que venga en el lote."""
    from app.funes_chat import nucleo

    a, b = nucleo._normalizar_texto(nuevo), nucleo._normalizar_texto(viejo)
    return bool(a) and b.startswith(a) and len(b) > len(a) + 4


@router.post("/admin/{token}/funes/sync-catalogo")
async def funes_sync_catalogo(token: str, payload: dict = Body(...)):
    """Aplica un lote de filas de funes_libros por UPSERT (ON CONFLICT DO
    UPDATE), igual que funes/migrar_catalogo_a_railway.py pero recibiendo el
    lote en el body en vez de leerlo de otra conexion a Postgres. Pensado para
    correrse en tandas chicas desde un script local (ver
    funes/migrar_catalogo_via_endpoint.py) para no mandar un body gigante de
    una sola vez."""
    _validar_token(token)
    libros = payload.get("libros")
    if not isinstance(libros, list) or not libros:
        raise HTTPException(status_code=400, detail="Body debe traer {'libros': [...]} con al menos uno.")
    for l in libros:
        if not isinstance(l, dict) or not l.get("id"):
            raise HTTPException(status_code=400, detail="Cada libro necesita al menos 'id'.")

    ids = [l["id"] for l in libros]
    existentes = {
        f["id"]: f["titulo"]
        for f in await db.pool().fetch(
            "SELECT id, titulo FROM funes_libros WHERE id = ANY($1::text[])", ids
        )
    }

    marcadores = ", ".join(f"${i}" for i in range(1, len(COLUMNAS_CATALOGO) + 1))
    set_ = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNAS_CATALOGO if c != "id")
    sql = (
        f"INSERT INTO funes_libros ({', '.join(COLUMNAS_CATALOGO)}) VALUES ({marcadores}) "
        f"ON CONFLICT (id) DO UPDATE SET {set_}"
    )

    nuevos = 0
    preservados = []
    filas = []
    for l in libros:
        id_ = l["id"]
        titulo_nuevo = l.get("titulo") or ""
        if id_ in existentes:
            if _es_recorte_titulo(titulo_nuevo, existentes[id_]):
                titulo_nuevo = existentes[id_]
                preservados.append(id_)
        else:
            nuevos += 1
        filas.append(tuple(
            titulo_nuevo if c == "titulo" else l.get(c) for c in COLUMNAS_CATALOGO
        ))

    await db.pool().executemany(sql, filas)
    return {
        "recibidos": len(libros),
        "nuevos": nuevos,
        "actualizados": len(libros) - nuevos,
        "titulos_preservados": preservados,
    }


@router.get("/admin/{token}/funes/sync-catalogo/estado")
async def funes_sync_catalogo_estado(token: str):
    """Resumen para verificar que una sincronizacion impacto bien, sin tener
    que conectarse a la base directamente."""
    _validar_token(token)
    total = await db.pool().fetchval("SELECT count(*) FROM funes_libros")
    con_abstracto = await db.pool().fetchval(
        "SELECT count(*) FROM funes_libros WHERE embedding_abstracto IS NOT NULL"
    )
    con_sinopsis = await db.pool().fetchval(
        "SELECT count(*) FROM funes_libros WHERE embedding_sinopsis IS NOT NULL"
    )
    con_experiencia = await db.pool().fetchval(
        "SELECT count(*) FROM funes_libros WHERE embedding_experiencia IS NOT NULL"
    )
    por_macro = await db.pool().fetch(
        "SELECT macro, count(*) n FROM funes_libros GROUP BY 1 ORDER BY 2 DESC"
    )
    por_version = await db.pool().fetch(
        "SELECT COALESCE(version_reescritura, '(sin version)') v, count(*) n "
        "FROM funes_libros WHERE macro = 'literatura' GROUP BY 1 ORDER BY 2 DESC"
    )
    return {
        "total": total,
        "con_embedding_abstracto": con_abstracto,
        "con_embedding_sinopsis": con_sinopsis,
        "con_embedding_experiencia": con_experiencia,
        "por_macro": {r["macro"]: r["n"] for r in por_macro},
        "literatura_por_version": {r["v"]: r["n"] for r in por_version},
    }


@router.get("/admin/{token}/funes/exportar-macro/{macro}")
async def funes_exportar_macro(token: str, macro: str):
    """Vuelca todas las columnas de catalogo de una macro, para respaldarlas
    en un archivo local antes de purgarlas de produccion (ver
    funes/respaldar_y_purgar_macro.py). Es la contracara de sync-catalogo:
    en vez de escribir un lote, lee uno entero."""
    _validar_token(token)
    filas = await db.pool().fetch(
        f"SELECT {', '.join(COLUMNAS_CATALOGO)} FROM funes_libros WHERE macro = $1 ORDER BY id",
        macro,
    )
    return {"macro": macro, "cantidad": len(filas), "libros": [dict(f) for f in filas]}


@router.delete("/admin/{token}/funes/purgar-macro/{macro}")
async def funes_purgar_macro(token: str, macro: str, payload: dict = Body(...)):
    """Borra de funes_libros todas las filas de una macro. Requiere
    `cantidad_esperada` en el body -el conteo que el llamador ya respaldo- y
    aborta con 409 si no coincide con lo que hay en la base en este momento:
    guarda contra un macro mal escrito o una fila nueva que llego entre el
    respaldo y el borrado."""
    _validar_token(token)
    cantidad_esperada = payload.get("cantidad_esperada")
    if not isinstance(cantidad_esperada, int):
        raise HTTPException(status_code=400, detail="Body debe traer {'cantidad_esperada': N}.")
    actual = await db.pool().fetchval("SELECT count(*) FROM funes_libros WHERE macro = $1", macro)
    if actual != cantidad_esperada:
        raise HTTPException(
            status_code=409,
            detail=f"cantidad_esperada={cantidad_esperada} pero hay {actual} filas con macro='{macro}' ahora mismo.",
        )
    eliminados = await db.pool().fetchval(
        "WITH borrados AS (DELETE FROM funes_libros WHERE macro = $1 RETURNING 1) SELECT count(*) FROM borrados",
        macro,
    )
    return {"macro": macro, "eliminados": eliminados}


async def _listar_librerias():
    filas = await db.pool().fetch(
        """
        SELECT l.id, l.slug, l.nombre, l.token_panel, l.tipo_catalogo, l.funes_habilitado,
               COUNT(li.id) FILTER (
                   WHERE li.estado = 'publicado' AND li.archivado_en IS NULL
               ) AS cant_libros
        FROM librerias l
        LEFT JOIN libros li ON li.libreria_id = l.id
        WHERE l.activa
        GROUP BY l.id
        ORDER BY l.creado_en DESC
        """
    )
    return filas


@router.get("/admin/{token}", response_class=HTMLResponse)
async def admin_home(request: Request, token: str):
    _validar_token(token)
    librerias = await _listar_librerias()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "librerias": librerias,
            "mensaje_wa_default": MENSAJE_WA_DEFAULT,
            "nueva": None,
            "error": None,
            "token": token,
        },
    )


@router.post("/admin/{token}", response_class=HTMLResponse)
async def admin_crear(
    request: Request,
    token: str,
    nombre: str = Form(...),
    whatsapp: str = Form(...),
    slug: str = Form(""),
    mensaje_wa_template: str = Form(MENSAJE_WA_DEFAULT),
    funes_habilitado: bool = Form(False),
):
    _validar_token(token)

    whatsapp = whatsapp.strip()
    slug_final = slugify(slug or nombre)
    token_panel = nuevo_token_panel()
    error = None
    nueva = None

    if not whatsapp.isdigit() or not (10 <= len(whatsapp) <= 15):
        error = "El WhatsApp tiene que ser solo números, en formato internacional (ej: 5491122334455)."
    else:
        try:
            await db.pool().execute(
                """
                INSERT INTO librerias
                    (slug, nombre, whatsapp, token_panel, mensaje_wa_template, funes_habilitado)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                slug_final,
                nombre.strip(),
                whatsapp,
                token_panel,
                mensaje_wa_template.strip() or MENSAJE_WA_DEFAULT,
                funes_habilitado,
            )
            base = str(request.base_url).rstrip("/")
            nueva = {
                "nombre": nombre.strip(),
                "url_panel": f"{base}/{slug_final}/panel/{token_panel}",
            }
        except Exception as exc:  # noqa: BLE001 — mostrar el motivo al admin alcanza acá
            error = f"No se pudo crear (¿el slug ya existe?): {exc}"

    librerias = await _listar_librerias()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "librerias": librerias,
            "mensaje_wa_default": MENSAJE_WA_DEFAULT,
            "nueva": nueva,
            "error": error,
            "token": token,
        },
    )


@router.get("/admin/{token}/librerias/{libreria_id}/metricas", response_class=HTMLResponse)
async def admin_metricas(request: Request, token: str, libreria_id: int):
    """Panel de lectura de la tabla eventos + estado del catalogo para una
    libreria puntual."""
    _validar_token(token)

    libreria = await db.pool().fetchrow(
        "SELECT id, slug, nombre, creado_en FROM librerias WHERE id = $1", libreria_id
    )
    if libreria is None:
        raise HTTPException(status_code=404)

    filas_eventos = await db.pool().fetch(
        "SELECT tipo, payload, session_id, creado_en FROM eventos "
        "WHERE libreria_id = $1 ORDER BY creado_en DESC",
        libreria_id,
    )
    filas_libros = await db.pool().fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE estado = 'publicado' AND archivado_en IS NULL) AS publicados,
            COUNT(*) FILTER (WHERE estado = 'vendido' AND archivado_en IS NULL) AS vendidos,
            COUNT(*) FILTER (WHERE estado = 'pendiente' AND archivado_en IS NULL) AS pendientes,
            COUNT(*) FILTER (WHERE duplicado_de IS NOT NULL) AS duplicados_detectados,
            COUNT(*) FILTER (WHERE archivado_en IS NOT NULL) AS archivados
        FROM libros WHERE libreria_id = $1
        """,
        libreria_id,
    )
    filas_lotes = await db.pool().fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE estado = 'publicado') AS publicados,
            COUNT(*) FILTER (WHERE archivado_en IS NOT NULL) AS archivados
        FROM lotes WHERE libreria_id = $1
        """,
        libreria_id,
    )
    filas_catalogos = await db.pool().fetch(
        "SELECT id, nombre, color, padre_id FROM catalogos WHERE libreria_id = $1", libreria_id
    )

    metricas = calcular_metricas(filas_eventos, filas_libros, filas_lotes, filas_catalogos)

    return templates.TemplateResponse(
        request,
        "metricas.html",
        {
            "libreria": libreria,
            "token": token,
            "es_admin": True,
            **metricas,
        },
    )


@router.post("/admin/{token}/librerias/{libreria_id}/funes")
async def admin_toggle_funes(token: str, libreria_id: int):
    """Prende/apaga Funes para una libreria existente (togglea, no setea a un
    valor puntual: el boton del listado no sabe el estado actual mas que por
    lo que ya renderizo, y togglear evita una carrera con dos clicks seguidos
    dando el mismo resultado). Bloqueado para 'cds': Funes hoy solo sabe de
    literatura, y prenderlo ahi seria una promesa que el catalogo no cumple."""
    _validar_token(token)
    fila = await db.pool().fetchrow(
        "UPDATE librerias SET funes_habilitado = NOT funes_habilitado "
        "WHERE id = $1 AND tipo_catalogo = 'libros' "
        "RETURNING funes_habilitado",
        libreria_id,
    )
    if fila is None:
        raise HTTPException(
            status_code=400,
            detail="No existe esa librería, o cataloga CDs y Funes no aplica.",
        )
    return {"funes_habilitado": fila["funes_habilitado"]}


@router.post("/admin/{token}/librerias/{libreria_id}/borrar")
async def admin_borrar_libreria(token: str, libreria_id: int):
    """Borrado duro: elimina la libreria y, en cascada, sus lotes/fotos/libros/
    eventos (ver FKs en schema.sql). No hay vuelta atras — a diferencia de
    "vaciar inventario" (que archiva), esto saca la fila entera de la base.
    Las fotos que haya en el volumen /data quedan huerfanas en disco, pero no
    se sirven mas (el registro que las referencia ya no existe)."""
    _validar_token(token)
    resultado = await db.pool().execute("DELETE FROM librerias WHERE id = $1", libreria_id)
    if resultado == "DELETE 0":
        raise HTTPException(status_code=404, detail="No existe esa librería.")
    return {"ok": True}