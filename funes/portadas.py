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
import difflib
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
# Alto/ancho de una tapa de libro: casi todas caen entre 1,4 y 1,7. Una foto de
# producto de ML fuera de esa banda suele ser un libro fotografiado con
# envoltorio, sobre una mesa o en caja, no la tapa de frente.
_RELACION_TAPA = (1.38, 1.7)
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


class _Intento:
    """Lo que paso en una pasada por la cascada. `fallo` distingue "ninguna
    fuente la tiene" (se recuerda 30 dias) de "no pude preguntarle a alguna"
    (timeout, 429, 5xx, token vencido): eso no se puede anotar como "sin
    portada", porque cuando la fuente vuelva si la tendria."""

    def __init__(self) -> None:
        self.fallo = False


def _se_parecen(a: str, b: str) -> bool:
    """Dos palabras iguales o casi: los autores cambian de nombre segun el idioma
    de la fuente (Thomas Moro / Thomas More, Tolstoi / Tolstoy), y buscar el
    nombre en castellano en Open Library no encuentra al mismo autor en ingles.
    El titulo tiene que coincidir aparte, asi que la tolerancia es chica."""
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _coincide(titulo: str, autor: str, nombre: str, autores: str = "") -> bool:
    """El resultado de la fuente corresponde al libro pedido.

    El titulo tiene que aparecer como palabras completas ("eden" no esta en
    "garden"). Con autor, al menos una de sus palabras significativas (el
    apellido) tiene que estar en el resultado. Sin autor la prueba es mucho mas
    debil, asi que solo se acepta un titulo distintivo (3 palabras o mas) o uno
    identico al de la fuente: "Poemas" a secas no identifica ningun libro."""
    texto = _normalizar(f"{nombre} {autores}")
    t = _normalizar(titulo)
    if not t or not re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", texto):
        return False
    palabras = [p for p in _normalizar(autor).split() if len(p) > 3]
    if palabras:
        presentes = set(texto.split())
        return any(_se_parecen(p, t) for p in palabras for t in presentes)
    return len(t.split()) >= 3 or t == _normalizar(nombre)


def _normalizar_imagen(contenido: bytes) -> tuple[bytes, int, int] | None:
    """Valida y normaliza a JPEG. None si no sirve (placeholder, muy chica,
    ilegible). Las apaisadas pasan hasta cierto punto: las fotos de producto de
    ML a veces vienen 4:3 y siguen siendo la tapa."""
    if not (_BYTES_MINIMOS <= len(contenido) <= _BYTES_MAXIMOS):
        return None
    try:
        with Image.open(io.BytesIO(contenido)) as img:
            ancho, alto = img.size
        if ancho < _ANCHO_MINIMO or alto < ancho * 0.6:
            return None
        normal = vision.redimensionar(contenido, lado_mayor=_LADO_MAYOR)
        with Image.open(io.BytesIO(normal)) as img:
            return normal, img.size[0], img.size[1]
    except Exception:  # noqa: BLE001
        return None


def _es_fallo_de_fuente(status: int) -> bool:
    """404 es una respuesta ("no la tengo"); lo demas que no sea 200 es la fuente
    fallando."""
    return status != 200 and status != 404


async def _bajar(client: httpx.AsyncClient, url: str, intento: _Intento) -> bytes | None:
    try:
        resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        intento.fallo = True
        return None
    if _es_fallo_de_fuente(resp.status_code):
        intento.fallo = True
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return None
    return resp.content


async def _esperar_turno_ol(prioritario: bool = False) -> None:
    """Un pedido cada _INTERVALO_OL para la carga en lote, que es la que puede
    pasarse del limite. El de alguien que esta mirando la pantalla (uno o dos
    pedidos por libro) no espera nada: ni cola ni intervalo."""
    global _ultimo_ol
    if prioritario:
        return
    async with _lock_ol:
        espera = _INTERVALO_OL - (time.monotonic() - _ultimo_ol)
        if espera > 0:
            await asyncio.sleep(espera)
        _ultimo_ol = time.monotonic()


# --- Fuentes: cada una devuelve la URL de una imagen candidata, o None ---------

def _tiene_forma_de_tapa(foto: dict) -> bool:
    """La foto de ML declara su tamano (max_width/max_height). Sin tamano no se
    puede saber y no se la prefiere."""
    try:
        relacion = float(foto["max_height"]) / float(foto["max_width"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
    return _RELACION_TAPA[0] <= relacion <= _RELACION_TAPA[1]


async def _url_ml(client: httpx.AsyncClient, token: str, params: dict, intento: _Intento,
                  titulo: str = "", autor: str = "", validar: bool = False) -> str | None:
    cabecera = {"Authorization": f"Bearer {token}"}
    try:
        busq = await client.get(
            f"{_API_ML}/products/search", headers=cabecera,
            params={"status": "active", "site_id": "MLA", **params},
        )
        if busq.status_code != 200:
            intento.fallo = intento.fallo or _es_fallo_de_fuente(busq.status_code)
            return None
        # Se corta en la primera foto con proporcion de tapa. Si ninguna la tiene se
        # sigue mirando los otros productos (los datos de las fotos ya vienen en la
        # misma respuesta, no cuesta bajar nada) y, de no haber, se usa la primera.
        respaldo = None
        for res in busq.json().get("results", [])[:5]:
            prod = await client.get(f"{_API_ML}/products/{res['id']}", headers=cabecera)
            if prod.status_code != 200:
                intento.fallo = intento.fallo or _es_fallo_de_fuente(prod.status_code)
                continue
            datos = prod.json()
            if validar and not _coincide(titulo, autor, str(datos.get("name", ""))):
                continue
            fotos = [f for f in datos.get("pictures") or [] if f.get("url")]
            for foto in fotos:
                if _tiene_forma_de_tapa(foto):
                    return foto["url"]
            if fotos and respaldo is None:
                respaldo = fotos[0]["url"]
        return respaldo
    except (httpx.HTTPError, ValueError, KeyError):
        intento.fallo = True
    return None


async def _url_ol_isbn(isbn: str, prioritario: bool = False) -> str:
    await _esperar_turno_ol(prioritario)
    return f"{_COVERS_OL}/b/isbn/{isbn}-L.jpg?default=false"


async def _url_ol_texto(client: httpx.AsyncClient, titulo: str, autor: str, intento: _Intento,
                        prioritario: bool = False) -> str | None:
    # Con el autor y, si no hay nada con portada, solo por titulo: Open Library
    # guarda al autor con su nombre en ingles, asi que filtrar por "Thomas Moro"
    # devuelve una edicion sin tapa y esconde la que si la tiene (Thomas More).
    # La validacion del autor la hace _coincide, con tolerancia.
    consultas = [{"title": titulo, "author": autor}] if autor.strip() else []
    consultas.append({"title": titulo})
    for i, params in enumerate(consultas):
        await _esperar_turno_ol(prioritario)
        try:
            resp = await client.get(
                f"{_API_OL}/search.json",
                params={**params, "limit": 10, "fields": "title,author_name,cover_i"},
            )
            if resp.status_code != 200:
                intento.fallo = intento.fallo or _es_fallo_de_fuente(resp.status_code)
                continue
            docs = resp.json().get("docs", [])
            for doc in docs:
                if doc.get("cover_i") and _coincide(
                        titulo, autor, doc.get("title", ""), " ".join(doc.get("author_name") or [])):
                    return f"{_COVERS_OL}/b/id/{doc['cover_i']}-L.jpg?default=false"
            # La busqueda solo por titulo es para cuando la del autor no trajo
            # NINGUNA edicion con tapa (el autor figura con otro nombre). Si traia
            # tapas y ninguna coincidio, repetir la busqueda sin autor solo suma
            # un pedido a un servicio gratuito.
            if i == 0 and len(consultas) > 1 and any(d.get("cover_i") for d in docs):
                break
        except (httpx.HTTPError, ValueError):
            intento.fallo = True
    return None


# --- Cascada y cache -----------------------------------------------------------

async def _resolver(isbn: str | None, titulo: str, autor: str, intento: _Intento,
                    prioritario: bool = False) -> tuple[bytes, int, int, str, str] | None:
    """Recorre la cascada. Devuelve (jpeg, ancho, alto, fuente, origen_url)."""
    token = await precios._token()
    if precios.hay_credenciales() and not token:
        intento.fallo = True   # ML esta configurado pero su token no sirve: cascada incompleta
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _USER_AGENT}) as client:
        pasos: list[tuple[str, object]] = []
        if isbn:
            if token:
                pasos.append(("ml-isbn", lambda: _url_ml(
                    client, token, {"product_identifier": isbn}, intento)))
            pasos.append(("ol-isbn", lambda: _url_ol_isbn(isbn, prioritario)))
        if titulo:
            consulta = f"{titulo} {autor}".strip()
            if token:
                pasos.append(("ml-texto", lambda: _url_ml(
                    client, token, {"q": consulta}, intento, titulo, autor, validar=True)))
            pasos.append(("ol-texto", lambda: _url_ol_texto(client, titulo, autor, intento, prioritario)))

        for fuente, buscar in pasos:
            url = await buscar()
            if not url:
                continue
            contenido = await _bajar(client, url, intento)
            if contenido is None:
                continue
            listo = _normalizar_imagen(contenido)
            if listo is None:
                logger.info("portadas_descartada fuente=%s url=%s", fuente, url)
                continue
            jpeg, ancho, alto = listo
            return jpeg, ancho, alto, fuente, url
    return None


# Claves cuya ultima busqueda fallo por culpa de una fuente (no por no tener la
# portada): no se anotan en la tabla, pero tampoco se reintentan de inmediato.
_fallos_recientes: dict[str, float] = {}
_ESPERA_TRAS_FALLO_S = 600


def _fallo_reciente(clave: str) -> bool:
    desde = _fallos_recientes.get(clave)
    if desde is None:
        return False
    if time.monotonic() - desde < _ESPERA_TRAS_FALLO_S:
        return True
    del _fallos_recientes[clave]
    return False


async def obtener(isbn: str | None, titulo: str, autor: str = "",
                  buscar_en_red: bool = True, prioritario: bool = False) -> str | None:
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
    if not buscar_en_red or _fallo_reciente(clave):
        return None

    intento = _Intento()
    encontrada = await _resolver(isbn_limpio, titulo, autor, intento, prioritario)
    if encontrada is None:
        if intento.fallo:
            _fallos_recientes[clave] = time.monotonic()
            logger.warning("portadas_fuente_fallo clave=%s (no se anota como sin_portada)", clave)
            return None
        await db.pool().execute(
            """INSERT INTO portadas (clave, estado) VALUES ($1, 'sin_portada')
               ON CONFLICT (clave) DO UPDATE SET estado = 'sin_portada', sha256 = NULL,
                   fuente = NULL, origen_url = NULL, revisada_en = now()""",
            clave,
        )
        return None
    _fallos_recientes.pop(clave, None)

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


# --- Uso desde las vistas ------------------------------------------------------
#
# Una vista no puede esperar a las APIs externas (Open Library se pacea a 3 s por
# pedido). Dos patrones: `iniciar` + `esperar` para UN libro cuando alguien lo esta
# mirando (Funes), y `consultar` + `buscar_en_segundo_plano` para una lista
# (catalogos): lo que ya esta guardado sale en esta vista, lo que falta se
# resuelve para la proxima.

# clave -> tarea que la esta resolviendo. Sirve a la vez de referencia fuerte
# (asyncio solo guarda una debil) y para no buscar dos veces el mismo libro: si
# alguien lo mira mientras el lote ya lo esta buscando, se espera esa misma.
_en_curso: dict[str, asyncio.Task] = {}
_semaforo = asyncio.Semaphore(2)
_MAX_EN_CURSO = 300


async def _correr(isbn: str | None, titulo: str, autor: str, prioritario: bool) -> str | None:
    try:
        if prioritario:
            return await obtener(isbn, titulo, autor, prioritario=True)
        async with _semaforo:
            return await obtener(isbn, titulo, autor)
    except Exception:  # noqa: BLE001
        logger.exception("portadas_fallo titulo=%r", titulo)
        return None


def _tarea_de(isbn: str | None, titulo: str, autor: str, prioritario: bool) -> asyncio.Task:
    clave = clave_de(isbn, titulo, autor)
    tarea = _en_curso.get(clave)
    if tarea is None:
        tarea = asyncio.create_task(_correr(isbn, titulo, autor, prioritario))
        _en_curso[clave] = tarea
        tarea.add_done_callback(lambda _t, c=clave: _en_curso.pop(c, None))
    return tarea


def iniciar(isbn: str | None, titulo: str, autor: str = "") -> asyncio.Task:
    """Empieza a buscar YA la portada de un libro que alguien esta por ver, sin
    hacer cola detras de la carga en lote. Se espera despues con `esperar`, asi la
    busqueda corre mientras se arma el resto de la respuesta."""
    return _tarea_de(isbn, titulo, autor, prioritario=True)


async def esperar(tarea: asyncio.Task, segundos: float = 3.0) -> str | None:
    """Resultado de `iniciar`, con tope de espera. Si vence, la busqueda sigue
    (el escudo evita que wait_for la cancele) y la proxima vista ya la encuentra."""
    try:
        return await asyncio.wait_for(asyncio.shield(tarea), timeout=segundos)
    except asyncio.TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("portadas_esperar_fallo")
        return None


async def consultar(libros: list[dict]) -> tuple[dict, set]:
    """Para una lista de libros (dict con id, isbn?, titulo, autor) devuelve
    ({id: url} de los que ya tienen portada guardada, {id} de los que se buscaron
    hace poco y no la tienen). No sale a la red."""
    claves = {l["id"]: clave_de(l.get("isbn"), l["titulo"], l.get("autor", "")) for l in libros}
    if not claves:
        return {}, set()
    filas = await db.pool().fetch(
        "SELECT clave, estado, sha256, "
        "(revisada_en < now() - make_interval(days => $2)) AS vieja "
        "FROM portadas WHERE clave = ANY($1::text[])",
        list(set(claves.values())), _REINTENTO_SIN_PORTADA_DIAS,
    )
    con_archivo = [f for f in filas if f["estado"] == "ok"]
    # Un stat por fila, fuera del event loop: en un volumen de red no es gratis.
    existentes = await asyncio.to_thread(
        lambda: {f["clave"]: url_de(f["sha256"]) for f in con_archivo if ruta_de(f["sha256"]).exists()})
    descartadas = {f["clave"] for f in filas if f["estado"] == "sin_portada" and not f["vieja"]}
    urls = {i: existentes[c] for i, c in claves.items() if c in existentes}
    sin_portada = {i for i, c in claves.items() if c in descartadas}
    return urls, sin_portada


def buscar_en_segundo_plano(libros: list[dict]) -> None:
    """Agenda la resolucion de los libros que aun no se buscaron. El que llama
    ya saco los que tienen portada y los que figuran como 'sin_portada'."""
    for libro in libros:
        if len(_en_curso) >= _MAX_EN_CURSO:
            return
        isbn, titulo, autor = libro.get("isbn"), libro["titulo"], libro.get("autor", "")
        clave = clave_de(isbn, titulo, autor)
        if titulo.strip() and clave not in _en_curso and not _fallo_reciente(clave):
            _tarea_de(isbn, titulo, autor, prioritario=False)


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
