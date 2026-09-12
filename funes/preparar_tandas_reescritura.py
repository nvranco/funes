"""Reparte en tandas la reescritura del catalogo para que la hagan agentes.

    python funes/preparar_tandas_reescritura.py preparar --ids 1984,un-mundo-feliz
    python funes/preparar_tandas_reescritura.py preparar --macro literatura --cuantos 40
    python funes/preparar_tandas_reescritura.py estado

Mismo mecanismo que `funes/curaduria/tandas.py` (la sesion de curaduria): el
agente lee su tanda del disco y escribe el JSON al disco, asi el coordinador
nunca carga las fichas en su contexto. La diferencia es la fuente: aca los
libros salen del catalogo VIVO (funes_libros, Postgres), no de la sqlite de
curaduria, porque esto no elige libros nuevos — reescribe los que ya estan
publicados.

Es la alternativa por agentes a `funes/reescribir_abstractos.py`, que hace el
mismo trabajo llamando a Gemini por OpenRouter. Lo que cambia no es solo quien
escribe: las reglas son otras y estan en `funes/instructivo_reescritura_v5.md`
(ver ahi el por que de cada una). Por eso la version es `v5a` y no `v4`.

Este script NO vectoriza y NO escribe en la base: solo arma las tandas. Aplicar
lo que devuelven los agentes es el paso siguiente, separado a proposito —
igual que en curaduria, el texto se puede escribir ahora y el vector se calcula
despues, que es lo unico que cuesta plata.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8")

from app import db  # noqa: E402
from app.funes_chat import nucleo  # noqa: E402

DIR = RAIZ / "funes" / "_scraping" / "reescritura"
CUARENTENA = DIR / "cuarentena.txt"
VERSION = "v5a"
POR_TANDA = 40


def _forma_por_genero(libro: dict) -> str | None:
    """Que forma resuelve el libro SIN mirar `rasgos`.

    Es el dato que decide si el `tema` que escriba el agente es un respaldo o
    es lo unico que mantiene visible al libro: _forma_del_libro mira primero
    genero/subgenero y solo despues cae al tema (ver su comentario). Si aca da
    None, el tema es load-bearing y hay que decirselo al agente.
    """
    return nucleo._forma_del_libro({**libro, "rasgos": {}})


def _ficha(n: int, libro: dict) -> str:
    partes = [f"[{n}] {libro['titulo']}",
              f"    autor: {libro['autor'] or '(sin dato)'}",
              f"    macro: {libro['macro']}"]

    genero = libro.get("genero") or "-"
    subgenero = libro.get("subgenero") or "-"
    partes.append(f"    genero / subgenero: {genero} / {subgenero}")

    forma = _forma_por_genero(libro)
    if forma:
        partes.append(f"    forma por genero: SI ({forma}) -> tu `tema` es respaldo")
    else:
        partes.append("    forma por genero: NO -> tu `tema` es lo UNICO que "
                      "mantiene visible a este libro")

    if libro.get("nro_paginas"):
        partes.append(f"    paginas: {libro['nro_paginas']}")
    if libro.get("fecha_publicacion"):
        partes.append(f"    publicacion (de esta edicion): {libro['fecha_publicacion']}")
    if libro.get("isbn"):
        partes.append(f"    isbn: {libro['isbn']}")

    # La ficha actual va como DATO, nunca como molde: su forma es justo la que
    # el instructivo esta rompiendo. La sinopsis/experiencia que ya tengan los
    # v3 NO se muestran, a proposito — anclarian al agente a los textos que
    # justamente salieron mal.
    abstracto = " ".join((libro.get("abstracto") or "").split())
    if abstracto:
        partes.append(f"    ficha actual del catalogo: {abstracto[:700]}")
    else:
        partes.append("    ficha actual del catalogo: (vacia)")
    return "\n".join(partes)


async def _libros_por_ids(ids: list[str]) -> list[dict]:
    filas = await db.pool().fetch(
        "SELECT id, titulo, autor, abstracto, macro, genero, subgenero, nro_paginas, "
        "       isbn, fecha_publicacion, version_reescritura "
        "FROM funes_libros WHERE id = ANY($1::text[]) AND embedding_abstracto IS NOT NULL",
        ids,
    )
    por_id = {f["id"]: dict(f) for f in filas}
    faltan = [i for i in ids if i not in por_id]
    if faltan:
        print(f"  OJO: no estan en el catalogo (o no estan vectorizados): {', '.join(faltan)}")
    return [por_id[i] for i in ids if i in por_id]


def _en_cuarentena() -> set[str]:
    """Los que un agente no pudo identificar. No vuelven a salir en una tanda:
    si ya se decidio que no se sabe que libro es, mandarselo a otro agente es
    pagar el mismo trabajo para llegar a la misma conclusion."""
    if not CUARENTENA.exists():
        return set()
    return {l.split("#")[0].strip() for l in CUARENTENA.read_text(encoding="utf-8").splitlines()
            if l.split("#")[0].strip()}


async def _pendientes(macro: str, cuantos: int | None) -> list[dict]:
    """Los que todavia no estan en la version actual. Orden estable por id para
    que dos corridas de `preparar` armen las mismas tandas."""
    filas = await db.pool().fetch(
        "SELECT id, titulo, autor, abstracto, macro, genero, subgenero, nro_paginas, "
        "       isbn, fecha_publicacion, version_reescritura "
        "FROM funes_libros "
        "WHERE macro = $1 AND embedding_abstracto IS NOT NULL "
        "  AND (version_reescritura IS DISTINCT FROM $2) "
        "ORDER BY id",
        macro, VERSION,
    )
    cuarentena = _en_cuarentena()
    libros = [dict(f) for f in filas if f["id"] not in cuarentena]
    return libros[:cuantos] if cuantos else libros


def _escribir_tandas(libros: list[dict], por_tanda: int) -> None:
    DIR.mkdir(parents=True, exist_ok=True)

    sin_aplicar = sorted(DIR.glob("tanda_*.escritos.json"))
    if sin_aplicar:
        nombres = ", ".join(r.name for r in sin_aplicar[:4])
        raise SystemExit(
            f"Hay {len(sin_aplicar)} archivo(s) de agentes sin aplicar ({nombres}).\n"
            "Los veredictos se aplican por NUMERO contra el mapa que quedo en disco:\n"
            "si se regeneran las tandas ahora, cada texto caeria en el libro\n"
            "equivocado, en silencio. Aplicalos o borralos primero."
        )
    for viejo in list(DIR.glob("tanda_*.txt")) + list(DIR.glob("tanda_*.mapa.json")):
        viejo.unlink()

    tandas = [libros[i:i + por_tanda] for i in range(0, len(libros), por_tanda)]
    for i, grupo in enumerate(tandas, 1):
        (DIR / f"tanda_{i:02d}.txt").write_text(
            "\n\n".join(_ficha(j + 1, l) for j, l in enumerate(grupo)), encoding="utf-8")
        (DIR / f"tanda_{i:02d}.mapa.json").write_text(
            json.dumps({str(j + 1): {"id": l["id"], "titulo": l["titulo"],
                                     "macro": l["macro"]}
                        for j, l in enumerate(grupo)}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"  tanda_{i:02d}.txt: {len(grupo)} libros")
    print(f"\n{len(tandas)} tanda(s) de hasta {por_tanda} en {DIR}")


async def preparar(ids: str | None, macro: str, cuantos: int | None, por_tanda: int) -> None:
    if ids:
        libros = await _libros_por_ids([i.strip() for i in ids.split(",") if i.strip()])
    else:
        libros = await _pendientes(macro, cuantos)
    if not libros:
        print("no quedan libros para reescribir")
        return
    por_version: dict[str, int] = {}
    for l in libros:
        clave = l["version_reescritura"] or "(sin version)"
        por_version[clave] = por_version.get(clave, 0) + 1
    print(f"{len(libros)} libros a reescribir -> {VERSION}   {por_version}")
    _escribir_tandas(libros, por_tanda)


async def estado(macro: str) -> None:
    filas = await db.pool().fetch(
        "SELECT COALESCE(version_reescritura, '(sin version)') v, COUNT(*) n "
        "FROM funes_libros WHERE macro = $1 AND embedding_abstracto IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
        macro,
    )
    total = sum(f["n"] for f in filas)
    print(f"catalogo de {macro}: {total:,} libros vectorizados")
    for f in filas:
        marca = "  <- version actual" if f["v"] == VERSION else ""
        print(f"   {f['v']:14s} {f['n']:6,d}{marca}")

    pendientes = sum(f["n"] for f in filas if f["v"] != VERSION)
    print(f"\nfaltan reescribir: {pendientes:,} ({pendientes / POR_TANDA:.0f} tandas de {POR_TANDA})")

    if DIR.exists():
        for etiqueta, patron in (("tandas armadas", "tanda_*.txt"),
                                 ("devueltas sin aplicar", "tanda_*.escritos.json"),
                                 ("ya aplicadas", "tanda_*.json.aplicado")):
            n = len(list(DIR.glob(patron)))
            if n:
                print(f"   {etiqueta}: {n}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Tandas de reescritura del catalogo, para agentes")
    ap.add_argument("accion", choices=("preparar", "estado"))
    ap.add_argument("--ids", help="ids puntuales separados por coma (para una tanda de prueba)")
    ap.add_argument("--macro", default="literatura")
    ap.add_argument("--cuantos", type=int, help="tope de libros")
    ap.add_argument("--por-tanda", type=int, default=POR_TANDA)
    args = ap.parse_args()

    await db.conectar()
    try:
        if args.accion == "preparar":
            await preparar(args.ids, args.macro, args.cuantos, args.por_tanda)
        else:
            await estado(args.macro)
    finally:
        await db.cerrar()


if __name__ == "__main__":
    asyncio.run(main())
