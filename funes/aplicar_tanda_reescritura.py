"""Escribe en el catalogo lo que devolvieron los agentes, y recien ahi vectoriza.

    python funes/aplicar_tanda_reescritura.py --tanda 01
    python funes/aplicar_tanda_reescritura.py              # todas las devueltas
    python funes/aplicar_tanda_reescritura.py --seco       # que haria, sin escribir

Es el ultimo paso del circuito: `preparar` arma las tandas, el agente las
escribe, `revisar` las mira, esto las aplica. Va separado de `revisar` a
proposito — mirar no deberia poder romper nada.

Tres destinos por libro, como en la sesion de curaduria:

  - **aplicado**: pasa la validacion. Se escriben sinopsis, experiencia y
    rasgos.tema, se calculan sus DOS vectores y se marca `v5a`.
  - **cuarentena**: el agente declaro `confianza: baja` — no pudo confirmar de
    que libro se trata. Se anota en cuarentena.txt y no vuelve a salir en una
    tanda. Es la unica defensa real contra publicar el texto de otro libro.
  - **rechazado**: falla de forma (largo, formula, meta-lenguaje, tema
    invalido). No se escribe nada y el libro vuelve al pool: lo va a reescribir
    otro agente en otra tanda.

Lo que NUNCA toca: `abstracto` y `embedding_abstracto`. Los tres vectores
conviven para que cambiar cual usa el ranking sea un flag y no una migracion
(ver _ANCLA_CONTRA_SINOPSIS en nucleo.py). El abstracto ademas sigue
alimentando tres prompts —la voz, el "contame mas" y las preguntas
profundas— hasta que se decida moverlos a la sinopsis.
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
from funes.revisar_tanda_reescritura import DIR, revisar_libro  # noqa: E402

VERSION = "v5a"
CUARENTENA = DIR / "cuarentena.txt"


async def _escribir(libro_id: str, item: dict) -> None:
    """Los dos textos, el tema y los dos vectores nuevos. `embedding_abstracto`
    no aparece en el UPDATE: es justamente lo que no se pisa."""
    sinopsis = " ".join(str(item["sinopsis"]).split())
    experiencia = " ".join(str(item["experiencia"]).split())
    vec_sinopsis = await nucleo._embeber(sinopsis)
    vec_experiencia = await nucleo._embeber(experiencia)
    await db.pool().execute(
        """
        UPDATE funes_libros
        SET sinopsis = $2,
            experiencia = $3,
            -- Merge y no reemplazo: los otros 8 campos de rasgos no los lee
            -- nadie hoy, pero borrarlos seria tirar trabajo ya hecho por si
            -- alguna vez se usan. Solo `tema` se pisa.
            rasgos = COALESCE(rasgos, '{}'::jsonb) || jsonb_build_object('tema', $4::text),
            embedding_sinopsis = $5,
            embedding_experiencia = $6,
            version_reescritura = $7
        WHERE id = $1
        """,
        libro_id, sinopsis, experiencia, str(item["tema"]).strip().lower(),
        vec_sinopsis, vec_experiencia, VERSION,
    )


def _en_cuarentena() -> set[str]:
    if not CUARENTENA.exists():
        return set()
    return {l.split("#")[0].strip() for l in CUARENTENA.read_text(encoding="utf-8").splitlines()
            if l.split("#")[0].strip()}


async def aplicar(tanda: str | None, seco: bool) -> None:
    patron = f"tanda_{tanda}.escritos.json" if tanda else "tanda_*.escritos.json"
    rutas = sorted(DIR.glob(patron))
    if not rutas:
        print(f"no hay tandas devueltas en {DIR}")
        return

    total_ok = total_cuar = total_mal = 0
    nuevas_cuarentena: list[str] = []

    for ruta in rutas:
        mapa_ruta = DIR / ruta.name.replace(".escritos.json", ".mapa.json")
        if not mapa_ruta.exists():
            print(f"{ruta.name}: falta el mapa, se saltea")
            continue
        mapa = json.loads(mapa_ruta.read_text(encoding="utf-8"))
        items = (json.loads(ruta.read_text(encoding="utf-8")).get("libros") or [])

        print(f"\n=== {ruta.name} ===")
        ok = cuar = mal = 0
        for item in items:
            n = str(item.get("n", ""))
            info = mapa.get(n)
            if info is None:
                print(f"  [{n}] no esta en el mapa de esta tanda, se descarta")
                mal += 1
                continue
            titulo = info["titulo"]

            if item.get("confianza") == "baja":
                nuevas_cuarentena.append(f"{info['id']}  # {titulo} — {item.get('motivo_baja', '')[:90]}")
                print(f"  [{n:>2}] {titulo[:38]:40s} CUARENTENA")
                cuar += 1
                continue

            errores, _avisos = revisar_libro(item, info)
            if errores:
                print(f"  [{n:>2}] {titulo[:38]:40s} RECHAZADO: {errores[0][:60]}")
                mal += 1
                continue

            if not seco:
                await _escribir(info["id"], item)
            ok += 1
            print(f"  [{n:>2}] {titulo[:38]:40s} ok")

        print(f"  -> aplicados {ok} | cuarentena {cuar} | rechazados {mal}")
        total_ok += ok
        total_cuar += cuar
        total_mal += mal

        if not seco:
            # Se renombra en vez de borrar: deja de bloquear a `preparar` y
            # queda el rastro de que escribio cada agente, por si hay que
            # auditar un texto raro contra el archivo original.
            os.replace(ruta, ruta.with_suffix(".json.aplicado"))

    if nuevas_cuarentena and not seco:
        with CUARENTENA.open("a", encoding="utf-8") as f:
            f.write("\n".join(nuevas_cuarentena) + "\n")

    print(f"\n{'(SECO, no se escribio nada) ' if seco else ''}"
          f"aplicados {total_ok} | cuarentena {total_cuar} | rechazados {total_mal}")
    if total_mal:
        print("  los rechazados vuelven al pool: los toma otra tanda mas adelante")
    if total_cuar:
        print(f"  cuarentena acumulada: {len(_en_cuarentena())} libros -> {CUARENTENA}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Aplica una tanda reescrita al catalogo")
    ap.add_argument("--tanda", help="numero de tanda, ej 01")
    ap.add_argument("--seco", action="store_true", help="no escribe ni vectoriza")
    args = ap.parse_args()

    await db.conectar()
    try:
        await aplicar(args.tanda, args.seco)
    finally:
        await db.cerrar()


if __name__ == "__main__":
    asyncio.run(main())
