"""Revisa lo que devolvio un agente antes de que toque la base.

    python funes/revisar_tanda_reescritura.py                    # revisa todas las tandas devueltas
    python funes/revisar_tanda_reescritura.py --tanda 01
    python funes/revisar_tanda_reescritura.py --sin-vectores     # solo reglas de texto, gratis

Dos capas, y la segunda es la que no existia antes:

1. **Por libro** — largo, arranques prohibidos, meta-lenguaje, contaminacion
   entre los dos campos, confianza/fuentes, y el `tema` contra la lista cerrada.
   Es lo mismo que ya validaba `reescribir_abstractos.py`, mas lo que agrega
   v5a: el meta-lenguaje se chequea en LOS DOS campos (antes solo en la
   sinopsis) y se busca contenido filtrandose dentro de `experiencia`.

2. **Por tanda** — el coseno promedio par a par entre los textos nuevos. Esta
   es la unica capa que puede ver el problema que nos trajo hasta aca: un texto
   que "habla de todo un poco" pasa cualquier validacion por libro y solo se
   delata en el agregado, cuando se lo compara con sus hermanos. La referencia
   del catalogo actual es 0,38 y es demasiado alta.

Para poder comparar manzanas con manzanas, el coseno nuevo se mide contra el de
los MISMOS libros con su texto viejo (que ya esta vectorizado en la base, asi
que sale gratis). Eso cancela el sesgo de haber elegido una tanda con libros
parecidos entre si a proposito.

NO escribe en la base. Aplicar es un paso aparte y despues de mirar esto.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from itertools import combinations
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8")

from app import db  # noqa: E402
from app.funes_chat import nucleo  # noqa: E402

DIR = RAIZ / "funes" / "_scraping" / "reescritura"

# Las listas NO se importan de reescribir_abstractos.py por dos razones: ese
# modulo corre `asyncio.run(main())` al importarse (no tiene guarda
# `if __name__`), e implementa las reglas de v4, que no son estas. Estas son
# las de v5a — ver funes/instructivo_reescritura_v5.md.
PALABRAS = {"sinopsis": (80, 120), "experiencia": (50, 70)}

ARRANQUES = {
    "sinopsis": ("este libro", "la obra", "el libro", "esta novela", "este ensayo",
                 "se trata de", "un libro", "una novela", "el autor", "la autora",
                 "este volumen", "esta obra", "este estudio", "el texto", "este texto",
                 "novela de", "libro de", "ensayo de"),
    "experiencia": ("la lectura", "este libro", "es una lectura", "leer este",
                    "se trata de", "la obra", "el libro", "esta lectura",
                    "se lee", "pide una", "pide atencion", "pide atención",
                    "se hojea", "se disfruta", "se avanza", "invita a",
                    "es un libro", "es una obra", "requiere una"),
}

# Hablar del libro como objeto en vez de su asunto. v4 lo miraba solo en la
# sinopsis; v5a lo mira en los dos campos.
META = re.compile(
    r"\b(este libro|esta obra|el presente (libro|volumen|trabajo)|la obra|el autor|"
    r"la autora|el volumen|esta novela|este ensayo|el texto|este texto)\b", re.I)

# Los cierres que el catalogo actual repite hasta volverlos invisibles.
CLISES = ("para quienes disfrutan", "para quien disfruta", "invita a una inmersion",
          "invita a una inmersión", "pide una lectura atenta", "ideal para momentos",
          "inmersion profunda", "inmersión profunda", "una lectura atenta")

TEMAS = {
    "literatura": ("novela", "genero", "clasico", "cuento", "poesia", "teatro",
                   "ensayo", "memoria", "otro"),
    "historia": ("argentina", "mundial", "americana", "belica", "originarios", "otro"),
    "divulgacion": ("mente", "vida", "tecno", "universo", "cuerpo", "tierra",
                    "numeros", "otro"),
}
CONFIANZAS = ("alta", "media", "baja")

# Palabras que arrancan oracion y no son nombres propios, para no tomarlas como
# contaminacion de contenido dentro de `experiencia`.
_NO_PROPIAS = {"el", "la", "los", "las", "un", "una", "es", "su", "sus", "no", "se",
               "lo", "al", "del", "y", "o", "pero", "con", "sin", "para", "por",
               "cada", "todo", "toda", "mas", "más", "ritmo", "denso", "breve"}


def _palabras(texto: str) -> int:
    return len(texto.split())


def _propias(texto: str) -> set[str]:
    """Nombres propios probables: mayuscula inicial, no arrancando oracion."""
    fuera = set()
    for oracion in re.split(r"[.;:!?]\s*", texto):
        for i, palabra in enumerate(oracion.split()):
            limpia = palabra.strip("\"'(),.«»—-")
            if i == 0 or len(limpia) < 4 or not limpia[:1].isupper():
                continue
            if limpia.lower() in _NO_PROPIAS:
                continue
            fuera.add(limpia)
    return fuera


def revisar_libro(item: dict, info: dict) -> tuple[list[str], list[str]]:
    """(errores, avisos) de un libro. Error = no deberia aplicarse asi."""
    errores: list[str] = []
    avisos: list[str] = []

    confianza = item.get("confianza")
    if confianza not in CONFIANZAS:
        return [f"confianza invalida: {confianza!r}"], []
    if confianza == "baja":
        if not (item.get("motivo_baja") or "").strip():
            avisos.append("confianza baja sin motivo escrito")
        return errores, avisos  # va a cuarentena, no se valida el resto

    if not [u for u in (item.get("fuentes") or []) if str(u).strip()]:
        errores.append(f"confianza {confianza} sin fuentes citadas")

    for campo, (minimo, maximo) in PALABRAS.items():
        texto = " ".join(str(item.get(campo) or "").split())
        if not texto:
            errores.append(f"{campo} vacia")
            continue

        n = _palabras(texto)
        if not (minimo <= n <= maximo):
            destino = errores if (n < minimo * 0.75 or n > maximo * 1.3) else avisos
            destino.append(f"{campo}: {n} palabras (pedido {minimo}-{maximo})")

        arranque = " ".join(texto.lower().split()[:5])
        for formula in ARRANQUES[campo]:
            if arranque.startswith(formula):
                errores.append(f"{campo} arranca con la formula '{formula}'")
                break

        sin_titulo = texto.replace(info.get("titulo", ""), " ")
        if META.search(sin_titulo):
            errores.append(f"{campo} habla del libro como objeto (meta-lenguaje)")

        bajo = texto.lower()
        for clise in CLISES:
            if clise in bajo:
                avisos.append(f"{campo} usa un cliche del catalogo viejo: '{clise}'")
                break

    # Contenido filtrandose dentro de experiencia: nombres propios que tambien
    # estan en la sinopsis (personajes, lugares) o el apellido del autor.
    sinopsis = str(item.get("sinopsis") or "")
    experiencia = str(item.get("experiencia") or "")
    if sinopsis and experiencia:
        compartidos = _propias(experiencia) & _propias(sinopsis)
        if compartidos:
            avisos.append(f"experiencia cuenta contenido (comparte {', '.join(sorted(compartidos)[:3])})")

    tema = str(item.get("tema") or "").strip().lower()
    validos = TEMAS.get(info.get("macro", "literatura"), TEMAS["literatura"])
    if tema not in validos:
        errores.append(f"tema invalido: {tema!r} (se guardaria como 'otro' = libro invisible)")
    elif tema == "otro":
        avisos.append("tema 'otro': en literatura eso saca al libro de las cuatro formas")

    return errores, avisos


def _coseno(a, b) -> float:
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _promedio_par_a_par(vectores: list) -> float:
    pares = list(combinations(range(len(vectores)), 2))
    if not pares:
        return 0.0
    return sum(_coseno(vectores[i], vectores[j]) for i, j in pares) / len(pares)


async def revisar_corpus(items: list[dict], mapa: dict) -> None:
    """El chequeo que no existia: que tan parecidos son los textos NUEVOS entre
    si, comparado con los VIEJOS de los mismos libros."""
    print("\n--- chequeo de corpus (coseno promedio par a par) ---")
    utiles = [i for i in items if i.get("confianza") in ("alta", "media")
              and i.get("experiencia") and i.get("sinopsis")]
    if len(utiles) < 3:
        print("  menos de 3 libros utiles, no se puede medir")
        return

    ids = [mapa[str(i["n"])]["id"] for i in utiles if str(i["n"]) in mapa]
    filas = await db.pool().fetch(
        "SELECT id, embedding_abstracto, embedding_experiencia FROM funes_libros "
        "WHERE id = ANY($1::text[])", ids)
    viejos = [list(f["embedding_abstracto"]) for f in filas if f["embedding_abstracto"]]
    # El control que importa: para un libro que YA fue reescrito, lo que estos
    # textos reemplazan no es el abstracto sino su `experiencia` anterior.
    viejas_exp = [list(f["embedding_experiencia"]) for f in filas if f["embedding_experiencia"]]

    nuevas_exp = [await nucleo._embeber(i["experiencia"]) for i in utiles]
    nuevas_sin = [await nucleo._embeber(i["sinopsis"]) for i in utiles]

    prom_viejo = _promedio_par_a_par(viejos)
    prom_exp = _promedio_par_a_par(nuevas_exp)
    prom_sin = _promedio_par_a_par(nuevas_sin)

    print(f"  abstracto VIEJO de estos mismos libros: {prom_viejo:.3f}")
    print(f"  sinopsis  nueva:                        {prom_sin:.3f}")
    print(f"  experiencia nueva:                      {prom_exp:.3f}   <- la que carga el perfil")
    if len(viejas_exp) >= 3:
        prom_exp_vieja = _promedio_par_a_par(viejas_exp)
        print(f"  experiencia ANTERIOR (de los ya reescritos, n={len(viejas_exp)}): {prom_exp_vieja:.3f}")
        print(f"    -> contra lo que realmente reemplaza: {prom_exp - prom_exp_vieja:+.3f}")
    else:
        print("  (esta tanda no trae libros ya reescritos, asi que no hay")
        print("   `experiencia` anterior contra la cual comparar de igual a igual)")

    print("  referencia: el promedio de `experiencia` v3 sobre muestras al azar")
    print("  del catalogo da 0,59-0,63. El abstracto NO es el control correcto")
    print("  para este campo: mide otro trabajo, no el mismo hecho mejor o peor.")

    peores = sorted(
        ((_coseno(nuevas_exp[i], nuevas_exp[j]), utiles[i]["n"], utiles[j]["n"])
         for i, j in combinations(range(len(utiles)), 2)), reverse=True)[:3]
    print("\n  los pares mas parecidos entre si (experiencia):")
    for c, a, b in peores:
        ta = mapa.get(str(a), {}).get("titulo", a)
        tb = mapa.get(str(b), {}).get("titulo", b)
        print(f"    {c:.3f}  [{a}] {ta[:30]}  <->  [{b}] {tb[:30]}")


async def revisar(tanda: str | None, sin_vectores: bool) -> None:
    patron = f"tanda_{tanda}.escritos.json" if tanda else "tanda_*.escritos.json"
    rutas = sorted(DIR.glob(patron))
    if not rutas:
        print(f"no hay archivos devueltos en {DIR}")
        return

    for ruta in rutas:
        mapa_ruta = DIR / ruta.name.replace(".escritos.json", ".mapa.json")
        if not mapa_ruta.exists():
            print(f"{ruta.name}: falta el mapa")
            continue
        mapa = json.loads(mapa_ruta.read_text(encoding="utf-8"))
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        items = datos.get("libros") or []

        print(f"\n=== {ruta.name}: {len(items)} libros devueltos de {len(mapa)} pedidos ===")
        faltan = set(mapa) - {str(i.get("n")) for i in items}
        if faltan:
            print(f"  NO devolvio: {', '.join(sorted(faltan, key=int))}")

        ok = con_avisos = con_errores = baja = 0
        for item in items:
            n = str(item.get("n", ""))
            info = mapa.get(n, {})
            titulo = info.get("titulo", f"(n={n})")
            errores, avisos = revisar_libro(item, info)
            if item.get("confianza") == "baja":
                baja += 1
                print(f"  [{n:>2}] {titulo[:34]:36s} CUARENTENA: {item.get('motivo_baja', '')[:60]}")
                continue
            if errores:
                con_errores += 1
                print(f"  [{n:>2}] {titulo[:34]:36s} ERROR")
                for e in errores:
                    print(f"        - {e}")
            elif avisos:
                con_avisos += 1
                print(f"  [{n:>2}] {titulo[:34]:36s} aviso")
            else:
                ok += 1
            for a in avisos:
                print(f"        ~ {a}")

        print(f"\n  limpios {ok} | con avisos {con_avisos} | con errores {con_errores} | cuarentena {baja}")

        if not sin_vectores:
            await revisar_corpus(items, mapa)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Revisa una tanda antes de aplicarla")
    ap.add_argument("--tanda", help="numero de tanda, ej 01")
    ap.add_argument("--sin-vectores", action="store_true",
                    help="saltea el chequeo de corpus (no gasta embeddings)")
    args = ap.parse_args()

    await db.conectar()
    try:
        await revisar(args.tanda, args.sin_vectores)
    finally:
        await db.cerrar()


if __name__ == "__main__":
    asyncio.run(main())
