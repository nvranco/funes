"""Piloto: la misma tarea de Fase B (buscar, verificar, escribir el abstracto)
pero con Gemini 3.1 Pro nativo -- grounding con Google Search + URL context en
vez de un agente Claude navegando con WebSearch/WebFetch.

    python catalog/curaduria/investigar_gemini.py probar
    python catalog/curaduria/investigar_gemini.py generar --limite 50 --concurrencia 4
    python catalog/curaduria/investigar_gemini.py estado

## Por que escribe en una tabla aparte

`enriquecimiento_piloto_gemini`, NO `enriquecimiento`. Es la misma logica que
"junte las bases en local primero, asi las auditamos juntas": mientras esto es
un piloto en evaluacion, no se mezcla con los 2.500 libros de produccion. Si el
piloto convence, migrar es una consulta SQL, no reescribir nada.

## Como se armo el prompt (viene de dos rondas de prueba manual en AI Studio)

La primera ronda (sin restringir dominios) devolvio abstractos correctos pero
citando mercadolibre.com y cuspide.com como fuente -- justo lo que
`instructivo_abstractos.md` prohibe. La segunda ronda, con la lista negra de
dominios explicita en el prompt, cito editoriales reales (adrianahidalgo.com,
editorialargonauta.com.ar) y Google Books en 7 de 9 casos. El unico agujero
que quedo: un libro con `confianza: media` pero `fuentes: []` -- la regla
"declara baja o media si no hay fuente de la jerarquia" no alcanzo, hacia
falta una regla dura sin excepcion. Los dos parches ya estan en el prompt de
aca abajo.

## Por que un libro por llamada, no tandas de a 12 como en abstractos.py

`groundingMetadata` (los dominios que realmente uso el modelo, no lo que el
modelo TIPEA en su campo "fuentes") viene atado a la respuesta entera, no a
un libro puntual dentro de una tanda. Separar en llamadas individuales pierde
paralelismo en tandas, pero a cambio cada dominio de grounding es atribuible
sin ambiguedad al libro que corresponde -- y ES el dato mas valioso de todo
este piloto: no hay que confiar en que el modelo escriba bien su propia
fuente, se puede verificar contra lo que Google efectivamente devolvio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))

BASE = RAIZ / "catalog" / "_scraping" / "curaduria.sqlite3"
MODELO_DEFAULT = "gemini-3.1-pro-preview"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

CONCURRENCIA_DEFAULT = 4
REINTENTOS = 3

# Dominios que NO valen como fuente (ver instructivo_abstractos.md): e-commerce
# y marketplaces. Esto se filtra dos veces: se lo prohibimos al modelo en el
# prompt, y ademas lo verificamos nosotros contra `groundingMetadata`, porque
# lo primero es una instruccion (puede fallar) y lo segundo es un hecho (lo
# que Google realmente devolvio).
DOMINIOS_PROHIBIDOS = (
    "mercadolibre", "cuspide.com", "buscalibre", "casadellibro",
    "uniliber", "sbs.com.ar", "yenny-elateneo", "amazon.", "goodreads",
)

# Los 9 libros del piloto manual: los 6 en cuarentena (Fase B con Claude ya se
# rindio con ellos, confianza baja) mas 3 de los "inalcanzables" de historia
# que Funes senalo como candidatos a reclasificar (arte/critica, tema=otro).
TEST_CLAVES = (
    "hola mundo|",
    "fernando pessoa|",
    "extasis|",
    "cuerpos impropios|lutereau",
    "china y el lado correcto de la historia|yao",
    "retazos|cervetto",
    "ante el tiempo|",
    "modos de ver|",
    "van gogh el suicidado por la sociedad|",
)

SYSTEM_INSTRUCTION = """Sos un bibliotecario argentino investigando fichas de un catálogo para un
recomendador de libros. Te paso un libro tal como viene de un scraping de
librerías (título en mayúsculas, datos a veces incompletos o cruzados con
otro libro). Para ese libro:

1. Buscá el libro por título + autor (o título + ISBN si no hay autor).
2. Verificá que lo que encontrás sea ESE libro exacto, contrastando contra
   el ISBN, la editorial y los datos que ya tenemos, antes de escribir nada.
3. La sinopsis que viene en la ficha puede ser de OTRO libro homónimo -- si
   no coincide con lo que confirma tu búsqueda, ignorala por completo.

JERARQUIA DE FUENTES -- de mayor a menor confianza:
  1. Página de la editorial.
  2. Wikipedia (español o inglés) de la obra específica.
  3. Catálogos bibliotecarios: Biblioteca Nacional, Library of Congress,
     Open Library, Google Books.
  4. Reseñas de prensa seria (diarios, revistas literarias reconocidas).
NO valen como fuente, aunque aparezcan en tu búsqueda: mercadolibre.com,
cuspide.com, buscalibre, casadellibro, sbs.com.ar, uniliber, ni ninguna
librería o marketplace online.

REGLA DURA, SIN EXCEPCION: si terminás sin ninguna fuente de la jerarquía de
arriba (campo "fuentes" vacío), la confianza tiene que ser "baja" y el campo
"abstracto" tiene que ir vacío. No existe combinación de "fuentes: []" con
confianza "media" o "alta" -- si eso pasa, está mal.

4. Si confirmás el libro con fuente válida: escribí un abstracto de
   110-130 palabras (70-170 aceptable), castellano rioplatense neutro, un
   párrafo, sin nombrar la editorial ni el año de la edición. Cubrí: de
   qué trata (concreto, con nombres y hechos, no generalidades), qué clase
   de libro es, cómo se lee, tono, a quién le sirve.

REGLAS ANTI-FORMULA (obligatorias, no sugerencia):
- Variá el arranque de la oración: no repitas la misma estructura de
  primera frase que usarías en otro libro.
- PROHIBIDO cerrar con la fórmula "[adjetivo de recomendación] para
  [audiencia] que busque/quiera/interese en...". Esto incluye, sin
  limitarse a: "imprescindible para", "insoslayable para", "excelente
  para", "ineludible para", "ideal para quien busca". Si querés decir a
  quién le sirve, integralo en otra parte de la oración, sin ese molde.
- PROHIBIDO: "el valor central es...", "lo mejor del libro es...".

5. Si la búsqueda no encuentra nada confiable o hay varios homónimos y no
   podés saber cuál es: NO inventes. Confianza "baja", sin abstracto.
6. Si el libro tiene género "HISTORIA DEL ARTE": indicá en "eje_si_arte" si
   el eje es entender una época/hecho histórico ("epoca") o pensar una idea
   o técnica en abstracto ("idea"). Si no aplica, poné "n/a".

Devolvé SOLO un JSON con esta forma exacta:
{"confianza": "alta|media|baja", "abstracto": "...", "autor": "...",
 "anio_obra": 0000, "titulo_original": "...", "nro_paginas": 000,
 "eje_si_arte": "epoca|idea|n/a", "fuentes": ["url1","url2"], "nota": "..."}
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "confianza": {"type": "STRING", "enum": ["alta", "media", "baja"]},
        "abstracto": {"type": "STRING"},
        "autor": {"type": "STRING"},
        "anio_obra": {"type": "INTEGER"},
        "titulo_original": {"type": "STRING"},
        "nro_paginas": {"type": "INTEGER"},
        "eje_si_arte": {"type": "STRING", "enum": ["epoca", "idea", "n/a"]},
        "fuentes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "nota": {"type": "STRING"},
    },
    "required": ["confianza", "fuentes", "nota"],
}


def _cargar_env() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    env_path = RAIZ / ".env"
    if env_path.exists():
        for linea in env_path.read_text(encoding="utf-8").splitlines():
            for var in ("GEMINI_API_KEY=", "GOOGLE_API_KEY="):
                if linea.strip().startswith(var):
                    return linea.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


ESQUEMA_TABLA = """
CREATE TABLE IF NOT EXISTS enriquecimiento_piloto_gemini (
    clave_obra TEXT PRIMARY KEY,
    modelo TEXT,
    confianza TEXT,
    abstracto TEXT,
    autor TEXT,
    anio_obra INTEGER,
    titulo_original TEXT,
    nro_paginas INTEGER,
    eje_si_arte TEXT,
    fuentes_modelo TEXT,
    dominios_grounding TEXT,
    fuentes_ok INTEGER,
    nota TEXT,
    escrito_en TEXT
);
"""


def _preparar_tabla(con: sqlite3.Connection) -> None:
    con.executescript(ESQUEMA_TABLA)
    con.commit()


def _ficha(f: sqlite3.Row) -> str:
    """Misma forma que `_ficha` en abstractos.py, para un solo libro."""
    partes = [f"TITULO: {f['titulo']}",
              f"autor (segun el catalogo): {f['autor'] or '(sin dato)'}",
              f"isbn: {f['isbn'] or '(sin dato)'}"]
    if f["editoriales"]:
        partes.append(f"editorial(es) segun el catalogo: {f['editoriales']}")
    if f["genero"]:
        partes.append(f"genero del catalogo: {f['genero']} / {f['subgenero'] or '-'}")
    if f["anio"]:
        partes.append(f"ano de ESTA edicion en catalogo (NO el de la obra): {f['anio']}")
    sin = (f["sinopsis"] or "").strip()
    if len(sin) > 60:
        partes.append(f"sinopsis del catalogo (puede ser de OTRO libro homonimo): {sin[:700]}")
    return "\n".join(partes)


def _pendientes(con: sqlite3.Connection, limite: int | None, claves: tuple[str, ...] | None) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    if claves:
        marcas = ",".join("?" for _ in claves)
        return con.execute(f"SELECT * FROM elegibles WHERE clave_obra IN ({marcas})", claves).fetchall()
    filas = con.execute(
        "SELECT e.* FROM elegibles e "
        "LEFT JOIN enriquecimiento_piloto_gemini p ON p.clave_obra = e.clave_obra "
        "WHERE e.elegido = 1 AND p.clave_obra IS NULL "
        "ORDER BY e.clave_obra"
    ).fetchall()
    return filas[:limite] if limite else filas


def _dominios_grounding(candidato: dict) -> list[str]:
    chunks = (candidato.get("groundingMetadata") or {}).get("groundingChunks") or []
    return [c["web"]["title"] for c in chunks if c.get("web", {}).get("title")]


def _fuentes_ok(dominios: list[str]) -> bool:
    return not any(malo in d.lower() for d in dominios for malo in DOMINIOS_PROHIBIDOS)


async def _investigar_uno(client: httpx.AsyncClient, api_key: str, modelo: str, ficha: str) -> dict:
    url = ENDPOINT.format(modelo=modelo)
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": ficha}]}],
        "tools": [{"google_search": {}}, {"url_context": {}}],
        "generationConfig": {
            "temperature": 1,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    ultimo_error: Exception | None = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = await client.post(
                url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            candidato = data["candidates"][0]
            texto = candidato["content"]["parts"][0]["text"]
            salida = json.loads(texto)
            salida["_dominios_grounding"] = _dominios_grounding(candidato)
            return salida
        except Exception as exc:  # noqa: BLE001
            ultimo_error = exc
            if intento < REINTENTOS:
                await asyncio.sleep(2 * intento)
    raise RuntimeError(f"fallo tras {REINTENTOS} intentos: {ultimo_error}")


async def generar(modelo: str, concurrencia: int, limite: int | None, claves: tuple[str, ...] | None) -> None:
    api_key = _cargar_env()
    if not api_key:
        raise SystemExit("Falta GEMINI_API_KEY (o GOOGLE_API_KEY) en el entorno o en .env. "
                          "Se consigue en AI Studio, icono de llave abajo a la izquierda.")

    con = sqlite3.connect(BASE)
    con.row_factory = sqlite3.Row
    _preparar_tabla(con)
    pendientes = _pendientes(con, limite, claves)
    if not pendientes:
        print("no hay nada pendiente")
        return
    print(f"investigando {len(pendientes)} libro(s) con {modelo}, concurrencia {concurrencia}\n")

    sem = asyncio.Semaphore(concurrencia)
    lock = asyncio.Lock()
    hechos = 0
    inicio = time.monotonic()

    async def uno(client: httpx.AsyncClient, fila: sqlite3.Row) -> None:
        nonlocal hechos
        ficha = _ficha(fila)
        async with sem:
            try:
                r = await _investigar_uno(client, api_key, modelo, ficha)
            except Exception as exc:  # noqa: BLE001
                print(f"  FALLO {fila['titulo']}: {exc}")
                return
        dominios = r.get("_dominios_grounding", [])
        async with lock:
            con.execute(
                "INSERT OR REPLACE INTO enriquecimiento_piloto_gemini "
                "(clave_obra, modelo, confianza, abstracto, autor, anio_obra, titulo_original, "
                " nro_paginas, eje_si_arte, fuentes_modelo, dominios_grounding, fuentes_ok, nota, escrito_en) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fila["clave_obra"], modelo, r.get("confianza"), r.get("abstracto"), r.get("autor"),
                 r.get("anio_obra"), r.get("titulo_original"), r.get("nro_paginas"), r.get("eje_si_arte"),
                 json.dumps(r.get("fuentes") or [], ensure_ascii=False),
                 json.dumps(dominios, ensure_ascii=False),
                 int(_fuentes_ok(dominios)), r.get("nota"),
                 datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            hechos += 1
            transcurrido = time.monotonic() - inicio
            print(f"  [{hechos}/{len(pendientes)}] {fila['titulo'][:50]:50s} "
                  f"confianza={r.get('confianza','?'):6s} fuentes_ok={_fuentes_ok(dominios)}  ({transcurrido:.0f}s)")

    async with httpx.AsyncClient(timeout=120) as client:
        await asyncio.gather(*(uno(client, f) for f in pendientes))

    con.close()
    print(f"\nterminado: {hechos}/{len(pendientes)} procesados")


def estado() -> None:
    con = sqlite3.connect(BASE)
    _preparar_tabla(con)
    total = con.execute("SELECT COUNT(*) FROM enriquecimiento_piloto_gemini").fetchone()[0]
    por_confianza = con.execute(
        "SELECT confianza, COUNT(*) FROM enriquecimiento_piloto_gemini GROUP BY confianza"
    ).fetchall()
    con_fuente_mala = con.execute(
        "SELECT COUNT(*) FROM enriquecimiento_piloto_gemini WHERE fuentes_ok = 0"
    ).fetchone()[0]
    print(f"piloto gemini: {total} libro(s) procesados")
    for conf, n in por_confianza:
        print(f"  confianza={conf}: {n}")
    print(f"  con dominio de fuente prohibido detectado: {con_fuente_mala}")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Piloto Fase B con Gemini nativo (grounding + URL context)")
    ap.add_argument("accion", choices=("probar", "generar", "estado"))
    ap.add_argument("--modelo", default=MODELO_DEFAULT)
    ap.add_argument("--concurrencia", type=int, default=CONCURRENCIA_DEFAULT)
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()

    if not BASE.exists():
        raise SystemExit(f"falta {BASE}")

    if args.accion == "estado":
        estado()
    elif args.accion == "probar":
        asyncio.run(generar(args.modelo, min(args.concurrencia, 3), None, TEST_CLAVES))
    else:
        asyncio.run(generar(args.modelo, args.concurrencia, args.limite, None))


if __name__ == "__main__":
    main()
