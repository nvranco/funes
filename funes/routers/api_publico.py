"""API pública (requisitos §6): catálogo descargable entero (E5) y registro
de eventos — la tabla que alimenta toda la contabilidad de la innovación."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from funes import db, portadas, vision

router = APIRouter()

TIPOS_EVENTO_VALIDOS = {"vista", "busqueda", "clic_whatsapp", "scan_qr"}


class Evento(BaseModel):
    tipo: str
    payload: dict = {}
    session_id: str = ""


@router.get("/api/{slug}/catalogo.json")
async def catalogo_json(slug: str):
    libreria = await db.pool().fetchrow(
        "SELECT id, tipo_catalogo FROM librerias WHERE slug = $1 AND activa", slug
    )
    if libreria is None:
        raise HTTPException(status_code=404)

    libros = await db.pool().fetch(
        """
        SELECT id, titulo, autor, catalogo_id,
               (foto_portada_id IS NOT NULL AND mostrar_foto) AS tiene_foto
        FROM libros
        WHERE libreria_id = $1 AND estado = 'publicado' AND archivado_en IS NULL
        ORDER BY (foto_portada_id IS NOT NULL AND mostrar_foto) DESC, titulo
        """,
        libreria["id"],
    )
    # Los libros sin foto propia usan la portada de referencia ya guardada; los
    # que todavia no se buscaron se buscan en segundo plano y aparecen en la
    # proxima carga, sin hacer esperar a nadie. Solo en catalogos de libros: las
    # fuentes son de libros, y buscar un album por titulo traeria tapas ajenas.
    de_referencia: dict = {}
    if libreria["tipo_catalogo"] == "libros":
        sin_foto = [l for l in libros if not l["tiene_foto"]]
        de_referencia, ya_descartados = await portadas.consultar(sin_foto)
        portadas.buscar_en_segundo_plano(
            [l for l in sin_foto if l["id"] not in de_referencia and l["id"] not in ya_descartados])
    datos = [
        {
            "id": l["id"],
            "titulo": l["titulo"],
            "autor": l["autor"],
            "catalogo_id": l["catalogo_id"],
            "imagen_url": (f"/api/{slug}/portada/{l['id']}.jpg" if l["tiene_foto"]
                           else de_referencia.get(l["id"])),
        }
        for l in libros
    ]
    return JSONResponse(datos, headers={"Cache-Control": "public, max-age=60"})


@router.get("/api/{slug}/portada/{libro_id}.jpg")
async def portada_libro(slug: str, libro_id: int):
    """Sirve la foto de tapa de un libro publicado, de cara al publico (sin
    token). Solo si esta publicado, no archivado, y el librero eligio
    mostrarla — mismo criterio que catalogo_json para decidir que se ve."""
    fila = await db.pool().fetchrow(
        """
        SELECT f.path FROM libros li
        JOIN librerias r ON r.id = li.libreria_id
        JOIN fotos f ON f.id = li.foto_portada_id
        WHERE li.id = $1 AND r.slug = $2 AND r.activa
          AND li.estado = 'publicado' AND li.archivado_en IS NULL
          AND li.foto_portada_id IS NOT NULL AND li.mostrar_foto
        """,
        libro_id, slug,
    )
    if fila is None:
        raise HTTPException(status_code=404)

    path = Path(fila["path"])
    miniatura = vision.ruta_miniatura(path)
    if miniatura.exists():
        path = miniatura
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/portadas/{sha}.jpg")
async def portada_de_referencia(sha: str):
    """Portada de referencia traida de una fuente externa (ver funes/portadas.py).
    El nombre es el sha256 del contenido, asi que no cambia nunca y se sirve
    con cache inmutable."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(status_code=404)
    ruta = portadas.ruta_de(sha)
    if not ruta.exists():
        raise HTTPException(status_code=404)
    return FileResponse(
        ruta, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/api/{slug}/evento")
async def registrar_evento(slug: str, request: Request, evento: Evento):
    libreria = await db.pool().fetchrow(
        "SELECT id FROM librerias WHERE slug = $1 AND activa", slug
    )
    if libreria is None:
        raise HTTPException(status_code=404)

    if evento.tipo not in TIPOS_EVENTO_VALIDOS:
        raise HTTPException(status_code=400, detail=f"Tipo de evento invalido: {evento.tipo}")

    await db.pool().execute(
        "INSERT INTO eventos (libreria_id, tipo, payload, session_id) VALUES ($1, $2, $3::jsonb, $4)",
        libreria["id"], evento.tipo, json.dumps(evento.payload), evento.session_id or None,
    )
    return {"ok": True}
