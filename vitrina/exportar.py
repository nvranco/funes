"""Exporta la landing de Funes a HTML estatico, para servirla sin la app.

Funes esta pausado: funes.store queda como vitrina (portfolio) servida por
Caddy desde vitrina/sitio. Se renderizan los mismos templates que usa la app,
con el registro deshabilitado (los CTA van a WhatsApp) y sin PostHog.

Correr desde la raiz del repo cada vez que cambie la landing:

    python vitrina/exportar.py

y commitear vitrina/sitio. El build de Railway no corre Python.
"""
import hashlib
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

RAIZ = Path(__file__).resolve().parent.parent
TEMPLATES = RAIZ / "funes" / "templates"
STATIC = RAIZ / "funes" / "static"
SITIO = Path(__file__).resolve().parent / "sitio"

BASE_URL = "https://funes.store"
CONTACTO = "nvrancovich@gmail.com"


def main() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    og_version = hashlib.sha1((STATIC / "funes-og.png").read_bytes()).hexdigest()[:10]

    paginas = {
        "index.html": env.get_template("funes_landing.html").render(
            posthog_key="",
            posthog_host="",
            base_url=BASE_URL,
            og_version=og_version,
            slug_demo="demo-libreros",
            registro_habilitado=False,
            vitrina=True,
        ),
        "funes/privacidad.html": env.get_template("funes_privacidad.html").render(
            contacto=CONTACTO,
        ),
    }

    if SITIO.exists():
        shutil.rmtree(SITIO)
    for nombre, html in paginas.items():
        destino = SITIO / nombre
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(html, encoding="utf-8")
    shutil.copytree(STATIC, SITIO / "static")
    print(f"Listo: {', '.join(paginas)} y static/ en {SITIO}")


if __name__ == "__main__":
    main()
