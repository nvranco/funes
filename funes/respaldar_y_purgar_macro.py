"""Respalda en un JSON local todos los libros de una macro de produccion y,
recien despues de verificar el respaldo, los borra de funes_libros alla.

Pensado para sacar historia y divulgacion de produccion (el piloto solo
expone literatura, ver docs/pipeline-recomendacion.md) sin perderlos: quedan
completos en funes/_respaldos/<macro>_<fecha>.json para poder reinsertarlos
el dia de manana con funes/migrar_catalogo_via_endpoint.py (lee de la base
local, asi que antes de purgar produccion confirma que la base local -que no
se toca aca- ya tiene esa macro).

Por defecto es un dry-run: exporta y cuenta, no borra nada. Borra solo con
--confirmar.

    python funes/respaldar_y_purgar_macro.py --url https://ireneofunes.up.railway.app --token <ADMIN_TOKEN> --macro historia
    python funes/respaldar_y_purgar_macro.py --url ... --token ... --macro historia --confirmar
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
DIR_RESPALDOS = RAIZ / "funes" / "_respaldos"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete(url: str, token: str, macro: str, cantidad_esperada: int) -> dict:
    body = json.dumps({"cantidad_esperada": cantidad_esperada}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/admin/{token}/funes/purgar-macro/{macro}",
        data=body, headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {detalle}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="URL base de la app en produccion")
    ap.add_argument("--token", required=True, help="ADMIN_TOKEN de produccion")
    ap.add_argument("--macro", required=True, help="macro a respaldar y purgar, ej: historia")
    ap.add_argument("--confirmar", action="store_true", help="sin esto, solo respalda y cuenta (no borra)")
    args = ap.parse_args()

    print(f"exportando macro='{args.macro}' desde {args.url} ...")
    datos = _get(f"{args.url.rstrip('/')}/admin/{args.token}/funes/exportar-macro/{args.macro}")
    cantidad = datos["cantidad"]
    print(f"  {cantidad} libros exportados")
    if cantidad == 0:
        print("nada para respaldar, no se toca nada mas.")
        return

    DIR_RESPALDOS.mkdir(exist_ok=True)
    destino = DIR_RESPALDOS / f"{args.macro}_{datetime.now():%Y%m%d_%H%M%S}.json"
    destino.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  guardado en {destino} ({destino.stat().st_size / 1024:.0f} KB)")

    if not args.confirmar:
        print(f"\ndry-run: no se borro nada. Para purgar de produccion, repetir con --confirmar.")
        return

    print(f"\nborrando macro='{args.macro}' de produccion (cantidad_esperada={cantidad}) ...")
    resultado = _delete(args.url, args.token, args.macro, cantidad)
    print(f"  eliminados: {resultado['eliminados']}")


if __name__ == "__main__":
    main()
