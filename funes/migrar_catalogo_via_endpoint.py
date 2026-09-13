"""Sincroniza funes_libros (local -> produccion) llamando al endpoint admin
en vez de conectarse directo a la base de Railway.

Es la variante de funes/migrar_catalogo_a_railway.py que no necesita una URL
de Postgres publica: ese script requiere un proxy TCP nuevo (expone la base a
internet) para poder conectarse desde afuera. Este pega por HTTPS al propio
`app`, que ya es publico, protegido por el mismo ADMIN_TOKEN que el resto de
las rutas /admin. El catalogo viaja en tandas chicas (ver LOTE) para no mandar
un body gigante de una sola vez.

    python funes/migrar_catalogo_via_endpoint.py --url https://ireneofunes.up.railway.app --token <ADMIN_TOKEN>
    python funes/migrar_catalogo_via_endpoint.py --url ... --token ... --macro literatura
    python funes/migrar_catalogo_via_endpoint.py --url ... --token ... --simular

Reejecutable: el endpoint hace UPSERT por id, asi que correr esto de nuevo
despues de tocar el catalogo en local no duplica nada.
"""
import argparse
import asyncio
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/librero")
os.environ.setdefault("ADMIN_TOKEN", "x")

from app import db  # noqa: E402

# Mismo listado que el endpoint y que migrar_catalogo_a_railway.py: toda
# columna que no este aca no viaja.
COLUMNAS = [
    "id", "titulo", "autor", "abstracto", "embedding_abstracto", "isbn", "fecha_publicacion",
    "categoria", "genero", "subgenero", "nro_paginas", "confianza_abstracto",
    "nota", "fuente", "macro", "macro_manual",
    "sinopsis", "experiencia", "embedding_sinopsis", "embedding_experiencia", "rasgos",
    "version_reescritura",
]
LOTE = 25


def _serializable(fila: dict) -> dict:
    d = dict(fila)
    for k, v in d.items():
        if isinstance(v, (datetime.date, datetime.datetime)):
            d[k] = v.isoformat()
        elif isinstance(v, list):
            d[k] = list(v)
    return d


def _postear(url: str, token: str, libros: list[dict]) -> dict:
    body = json.dumps({"libros": libros}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/admin/{token}/funes/sync-catalogo",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} en lote: {detalle}")


def _estado(url: str, token: str) -> dict:
    req = urllib.request.Request(f"{url.rstrip('/')}/admin/{token}/funes/sync-catalogo/estado")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="URL base de la app en produccion")
    ap.add_argument("--token", required=True, help="ADMIN_TOKEN de produccion")
    ap.add_argument("--macro", help="Si se pasa, sincroniza solo esa macro")
    ap.add_argument("--simular", action="store_true", help="solo cuenta y arma los lotes, no postea nada")
    args = ap.parse_args()

    await db.conectar()
    try:
        where = "WHERE macro = $1" if args.macro else ""
        params = [args.macro] if args.macro else []
        filas = await db.pool().fetch(
            f"SELECT {', '.join(COLUMNAS)} FROM funes_libros {where} ORDER BY id", *params)
    finally:
        await db.cerrar()

    print(f"origen: {len(filas)} libros" + (f" (macro={args.macro})" if args.macro else ""))
    if not filas:
        print("Nada para sincronizar.")
        return

    libros = [_serializable(dict(f)) for f in filas]

    if args.simular:
        print(f"--simular: se mandarian {len(libros)} libros en {(len(libros) + LOTE - 1) // LOTE} lotes de {LOTE}.")
        return

    print("estado ANTES:")
    print(json.dumps(_estado(args.url, args.token), ensure_ascii=False, indent=2))

    total_nuevos = 0
    total_actualizados = 0
    preservados_todos = []
    for inicio in range(0, len(libros), LOTE):
        tanda = libros[inicio:inicio + LOTE]
        resultado = _postear(args.url, args.token, tanda)
        total_nuevos += resultado["nuevos"]
        total_actualizados += resultado["actualizados"]
        preservados_todos.extend(resultado["titulos_preservados"])
        print(f"  {inicio + len(tanda)}/{len(libros)}  "
              f"(nuevos {resultado['nuevos']}, actualizados {resultado['actualizados']})", flush=True)

    print(f"\ntotal: {total_nuevos} nuevos, {total_actualizados} actualizados")
    if preservados_todos:
        print(f"{len(preservados_todos)} titulos preservados en destino (el local traia un recorte): "
              f"{', '.join(preservados_todos[:10])}{'...' if len(preservados_todos) > 10 else ''}")

    print("\nestado DESPUES:")
    print(json.dumps(_estado(args.url, args.token), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
