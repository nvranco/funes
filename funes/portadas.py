"""Portadas de referencia para libros que no tienen foto propia.

Una sola funcion publica, `obtener`, con cache de lectura: busca la copia ya
guardada y, si falta, resuelve por una cascada de fuentes, normaliza la imagen,
la guarda en DATA_DIR/portadas/ y deja el rastro en la tabla `portadas`.

Cascada (el primer acierto gana, todo validado):

  1. MercadoLibre por ISBN      /products/search?product_identifier=
  2. Open Library por ISBN      covers.openlibrary.org/b/isbn/
  3. MercadoLibre por texto     /products/search?q=titulo autor   (sin medir)
  4. Open Library por texto     search.json -> cover_i             (sin medir)

Los pasos 3 y 4 solo aceptan un resultado cuyo titulo (y autor, si lo hay)
coincide con el pedido: sin ISBN hay mucho ruido (usados, packs, otras
ediciones).

El archivo se nombra por sha256 de los bytes ya normalizados: la misma tapa
traida por dos caminos es un solo archivo, y la URL nunca cambia de contenido,
asi que se sirve con cache inmutable. Mover esto a un bucket (R2) es copiar
la carpeta y cambiar la base de `url_de`.

Las portadas son de las editoriales y de los vendedores: guardarlas y mostrarlas
es zona gris. Por eso cada fila guarda fuente y origen_url, para poder retirar
una a pedido de su dueño.
"""

import asyncio
import hashlib
import io
import logging
import re
import time
import unicodedata

import httpx
from PIL import Image

from funes import db, vision
from funes.config import DATA_DIR
from funes.funes_chat import precios

logger = logging.getLogger("librero.portadas")

CARPETA = DATA_DIR / "portadas"
_API_ML = "https://api.mercadolibre.com"
_API_OL = "https://openlibrary.org"
_COVERS_OL = "https://covers.openlibrary.org"
_USER_AGENT = "librero-funes/1.0 (portadas de referencia; contacto en funes.app)"

_ANCHO_MINIMO = 300          # px: por debajo no sirve ni de miniatura
_BYTES_MINIMOS = 5_000       # Open Library devuelve miniaturas de ~1 KB
_BYTES_MAXIMOS = 8_000_000   # tope de descarga, para no tragarse cualquier cosa
_LADO_MAYOR = 800
_REINTENTO_SIN_PORTADA_DIAS = 30

# Open Library limita las busquedas por ISBN a 100 pedidos por IP cada 5 minutos.
# Un pedido cada 3 s queda por debajo con margen.
_INTERVALO_OL = 3.0
_lock_ol = asyncio.Lock()
_ultimo_ol = 0.0


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", sin_tildes.lower()).strip()


def _isbn(isbn: str | None) -> str | None:
    limpio = re.sub(r"[^0-9Xx]", "", isbn or "").upper()
    return limpio if len(limpio) in (10, 13) else None


def clave_de(isbn: str | None, titulo: str, autor: str) -> str:
    """ISBN normalizado, o titulo+autor normalizados si no hay ISBN."""
    limpio = _isbn(isbn)
    if limpio:
        return limpio
    return "ta:" + " ".join(_normalizar(f"{titulo} {autor}").split())[:200]


def url_de(sha: str) -> str:
    return f"/portadas/{sha}.jpg"


def ruta_de(sha: str):
    return CARPETA / sha[:2] / f"{sha}.jpg"


def _coincide(titulo: str, autor: str, nombre_encontrado: str) -> bool:
    """El nombre devuelto por la fuente contiene el titulo pedido y, si hay
    autor, al menos una de sus palabras significativas (apellido)."""
    nombre = _normalizar(nombre_encontrado)
    t = _normalizar(titulo)
    if not t or t not in nombre:
        return False
    palabras = [p for p in _normalizar(autor).split() if len(p) > 3]
    return not palabras or any(p in nombre for p in palabras)


def _normalizar_imagen(contenido: bytes) -> tuple[bytes, int, int] | None:
    """Valida y normaliza a JPEG. None si no sirve (placeholder, muy chica,
    ilegible)."""
    if not (_BYTES_MINIMOS <= len(contenido) <= _BYTES_MAXIMOS):
        return None
    try:
        with Image.open(io.BytesIO(contenido)) as img:
            ancho, alto = img.size
        if ancho < _ANCHO_MINIMO or alto < ancho * 0.8:
            return None
        normal = vision.redimensionar(contenido, lado_mayor=_LADO_MAYOR)
        with Image.open(io.BytesIO(normal)) as img:
            return normal, img.size[0], img.size[1]
    except Exception:  # noqa: BLE001
        return None


async def _bajar(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return None
    return resp.content


async def _esperar_turno_ol() -> None:
    global _ultimo_ol
    async with _lock_ol:
        espera = _INTERVALO_OL - (time.monotonic() - _ultimo_ol)
        if espera > 0:
            await asyncio.sleep(espera)
        _ultimo_ol = time.monotonic()


# --- Fuentes: cada una devuelve la URL de una imagen candidata, o None ---------

async def _url_ml(client: httpx.AsyncClient, token: str, params: dict,
                  titulo: str = "", autor: str = "", validar: bool = False) -> str | None:
    cabecera = {"Authorization": f"Bearer {token}"}
    try:
        busq = await client.get(
            f"{_API_ML}/products/search", headers=cabecera,
            params={"status": "active", "site_id": "MLA", **params},
        )
        if busq.status_code != 200:
            return None
        for res in busq.json().get("results", [])[:5]:
            prod = await client.get(f"{_API_ML}/products/{res['id']}", headers=cabecera)
            if prod.status_code != 200:
                continue
            datos = prod.json()
            if validar and not _coincide(titulo, autor, str(datos.get("name", ""))):
                continue
            fotos = datos.get("pictures") or []
            if fotos and fotos[0].get("url"):
                return fotos[0]["url"]
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


async def _url_ol_isbn(isbn: str) -> str:
    await _esperar_turno_ol()
    return f"{_COVERS_OL}/b/isbn/{isbn}-L.jpg?default=false"


async def _url_ol_texto(client: httpx.AsyncClient, titulo: str, autor: str) -> str | None:
    await _esperar_turno_ol()
    try:
        resp = await client.get(
            f"{_API_OL}/search.json",
            params={"title": titulo, "author": autor, "limit": 5,
                    "fields": "title,author_name,cover_i"},
        )
        if resp.status_code != 200:
            return None
        for doc in resp.json().get("docs", []):
            if not doc.get("cover_i"):
                continue
            nombre = f"{doc.get('title', '')} {' '.join(doc.get('author_name') or [])}"
            if _coincide(titulo, autor, nombre):
                return f"{_COVERS_OL}/b/id/{doc['cover_i']}-L.jpg?default=false"
    except (httpx.HTTPError, ValueError):
        pass
    return None


# --- Cascada y cache -----------------------------------------------------------

async def _resolver(isbn: str | None, titulo: str, autor: str) -> tuple[bytes, int, int, str, str] | None:
    """Recorre la cascada. Devuelve (jpeg, ancho, alto, fuente, origen_url)."""
    token = await precios._token()
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
        pasos: list[tuple[str, object]] = []
        if isbn:
            if token:
                pasos.append(("ml-isbn", lambda: _url_ml(client, token, {"product_identifier": isbn})))
            pasos.append(("ol-isbn", lambda: _url_ol_isbn(isbn)))
        if titulo:
            consulta = f"{titulo} {autor}".strip()
            if token:
                pasos.append(("ml-texto", lambda: _url_ml(
                    client, token, {"q": consulta}, titulo, autor, validar=True)))
            pasos.append(("ol-texto", lambda: _url_ol_texto(client, titulo, autor)))

        for fuente, buscar in pasos:
            url = await buscar()
            if not url:
                continue
            contenido = await _bajar(client, url)
            if contenido is None:
                continue
            listo = _normalizar_imagen(contenido)
            if listo is None:
                logger.info("portadas_descartada fuente=%s url=%s", fuente, url)
                continue
            jpeg, ancho, alto = listo
            return jpeg, ancho, alto, fuente, url
    return None


async def obtener(isbn: str | None, titulo: str, autor: str = "",
                  buscar_en_red: bool = True) -> str | None:
    """URL local (`/portadas/<sha>.jpg`) de la portada del libro, o None.

    `buscar_en_red=False` solo consulta lo ya guardado: para las vistas donde
    no se puede esperar ~1 s a las APIs externas."""
    isbn_limpio = _isbn(isbn)
    clave = clave_de(isbn_limpio, titulo, autor)
    fila = await db.pool().fetchrow(
        "SELECT estado, sha256, revisada_en, "
        "(revisada_en < now() - make_interval(days => $2)) AS vieja "
        "FROM portadas WHERE clave = $1", clave, _REINTENTO_SIN_PORTADA_DIAS,
    )
    if fila is not None:
        if fila["estado"] == "ok" and ruta_de(fila["sha256"]).exists():
            return url_de(fila["sha256"])
        if fila["estado"] == "sin_portada" and not fila["vieja"]:
            return None
    if not buscar_en_red:
        return None

    encontrada = await _resolver(isbn_limpio, titulo, autor)
    if encontrada is None:
        await db.pool().execute(
            """INSERT INTO portadas (clave, estado) VALUES ($1, 'sin_portada')
               ON CONFLICT (clave) DO UPDATE SET estado = 'sin_portada', sha256 = NULL,
                   fuente = NULL, origen_url = NULL, revisada_en = now()""",
            clave,
        )
        return None

    jpeg, ancho, alto, fuente, origen = encontrada
    sha = hashlib.sha256(jpeg).hexdigest()
    ruta = ruta_de(sha)
    if not ruta.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(jpeg)
    await db.pool().execute(
        """INSERT INTO portadas (clave, estado, sha256, fuente, origen_url, ancho, alto, bytes)
           VALUES ($1, 'ok', $2, $3, $4, $5, $6, $7)
           ON CONFLICT (clave) DO UPDATE SET estado = 'ok', sha256 = EXCLUDED.sha256,
               fuente = EXCLUDED.fuente, origen_url = EXCLUDED.origen_url, ancho = EXCLUDED.ancho,
               alto = EXCLUDED.alto, bytes = EXCLUDED.bytes, revisada_en = now()""",
        clave, sha, fuente, origen, ancho, alto, len(jpeg),
    )
    logger.info("portadas_guardada clave=%s fuente=%s bytes=%d", clave, fuente, len(jpeg))
    return url_de(sha)


async def retirar(clave: str) -> bool:
    """Saca una portada a pedido de su dueño: marca la fila y borra el archivo
    si ninguna otra clave lo usa."""
    fila = await db.pool().fetchrow("SELECT sha256 FROM portadas WHERE clave = $1", clave)
    if fila is None:
        return False
    sha = fila["sha256"]
    await db.pool().execute(
        "UPDATE portadas SET estado = 'sin_portada', sha256 = NULL, revisada_en = now() + interval '3650 days' "
        "WHERE clave = $1", clave,
    )
    if sha and not await db.pool().fetchval("SELECT 1 FROM portadas WHERE sha256 = $1 LIMIT 1", sha):
        ruta_de(sha).unlink(missing_ok=True)
    return True
