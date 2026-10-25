"""Rate limit para los endpoints publicos de Funes.

Hace falta antes de pegar un QR en la calle: los endpoints no tienen auth y cada
recomendacion gasta 2 embeddings y una llamada al LLM. Sin esto, un script (o un
crawler entusiasta) puede vaciar la cuenta de OpenRouter en una tarde.

Ventana deslizante en memoria, sin dependencias nuevas: no hay Redis en este
proyecto, no hay middleware, y corre una sola instancia en Railway. El costo de
esa simplicidad es que los contadores se reinician en cada deploy, lo cual es
aceptable para lo que protege.

Dos baldes, y el segundo es el que importa:
- Por IP: frena al que se pone a apretar el boton.
- Global: frena el abuso distribuido, que el balde por IP no ve. Es el que
  protege la billetera.
"""

import logging
import time

from fastapi import HTTPException, Request

logger = logging.getLogger("librero.funes_chat")

# Una conversacion completa gasta 3 llamadas caras (2 preguntas profundas + 1
# recomendacion) y hasta 2 mas si pide otras recomendaciones. 20 en 10 minutos
# deja lugar de sobra para dos conversaciones seguidas y corta el bucle.
_LIMITE_IP = (20, 600)
_LIMITE_GLOBAL = (400, 3600)

# Los endpoints que solo escriben en la bitacora o leen la cache de precios no
# gastan LLM: se limitan mucho mas laxo para no romper la instrumentacion, que
# es justamente lo que no queremos perder.
_LIMITE_IP_BARATO = (120, 600)

# La demo para libreros es publica, la comparte gente que no conocemos y cada
# conversacion gasta LLM de verdad. Dos topes propios, ademas de los de arriba:
# por IP (una persona probando dos o tres veces entra de sobra) y uno global por
# dia, para que un link que se viraliza no vacie la cuenta.
_LIMITE_DEMO_IP = (12, 3600)
_LIMITE_DEMO_GLOBAL = (200, 86400)

_ventanas: dict[str, list[float]] = {}


def _ip(request: Request) -> str:
    """La IP real del visitante.

    Detras del proxy de Railway, request.client.host es el proxy: si usaramos
    eso, todas las personas compartirian un solo balde y el limite se dispararia
    con dos usuarios simultaneos.

    Se toma el ULTIMO elemento de X-Forwarded-For y no el primero. El primero es
    el que dice ser el cliente, y cualquiera puede mandar esa cabecera: bastaba
    con cambiarla en cada request para estrenar un balde vacio cada vez, o sea
    que el limite por IP no limitaba nada. El ultimo lo escribe el proxy de
    Railway con la IP desde la que realmente le llego la conexion, y eso no se
    puede falsificar desde afuera. En local no hay cabecera y se usa el peer."""
    reenviado = request.headers.get("x-forwarded-for", "")
    if reenviado:
        saltos = [x.strip() for x in reenviado.split(",") if x.strip()]
        if saltos:
            return saltos[-1]
    return request.client.host if request.client else "desconocido"


def _permitido(clave: str, maximo: int, ventana: int) -> bool:
    ahora = time.monotonic()
    marcas = [t for t in _ventanas.get(clave, []) if ahora - t < ventana]
    if len(marcas) >= maximo:
        _ventanas[clave] = marcas
        return False
    marcas.append(ahora)
    _ventanas[clave] = marcas
    return True


def _cabe(clave: str, maximo: int, ventana: int) -> bool:
    """Como _permitido, pero sin anotar la marca: sirve para chequear varios
    topes y anotarlos recien cuando el pedido pasa todos."""
    ahora = time.monotonic()
    marcas = [t for t in _ventanas.get(clave, []) if ahora - t < ventana]
    _ventanas[clave] = marcas
    return len(marcas) < maximo


def _anotar(clave: str) -> None:
    _ventanas.setdefault(clave, []).append(time.monotonic())


def _limpiar() -> None:
    """Saca del dict las claves que ya no tienen marcas vivas, para que no crezca
    indefinidamente con las IPs que pasaron una sola vez."""
    if len(_ventanas) < 500:
        return
    ahora = time.monotonic()
    for clave in [k for k, v in _ventanas.items() if not v or ahora - v[-1] > 3600]:
        _ventanas.pop(clave, None)


def controlar(request: Request, caro: bool = True, demo: bool = False) -> None:
    """Levanta 429 si se paso del limite. `caro=True` para los endpoints que
    gastan LLM. `demo=True` para los de la libreria de demostracion: suma los
    topes de _LIMITE_DEMO_*."""
    _limpiar()
    ip = _ip(request)
    maximo, ventana = _LIMITE_IP if caro else _LIMITE_IP_BARATO
    if not _permitido(f"ip:{ip}:{'caro' if caro else 'barato'}", maximo, ventana):
        logger.warning("funes_chat_limite_ip ip=%s caro=%s", ip, caro)
        raise HTTPException(status_code=429, detail="Demasiados pedidos. Probá en un rato.")

    if not caro:
        return

    # Los topes de la demo y el global se chequean todos antes de anotar
    # ninguno: un pedido rechazado por uno no descuenta cupo de los otros.
    topes = []
    if demo:
        topes.append((f"demo-ip:{ip}", _LIMITE_DEMO_IP, logging.WARNING,
                      "funes_chat_limite_demo_ip ip=%s", (ip,),
                      "Ya probaste varias veces la demo. Escribinos y lo hablamos."))
        topes.append(("demo-global", _LIMITE_DEMO_GLOBAL, logging.ERROR,
                      "funes_chat_limite_demo_global alcanzado", (),
                      "La demo llegó al tope de hoy. Probá mañana o escribinos."))
    topes.append(("global", _LIMITE_GLOBAL, logging.ERROR,
                  "funes_chat_limite_global alcanzado", (),
                  "Funes está saturado. Probá más tarde."))
    for clave, (maximo, ventana), nivel, mensaje_log, args_log, detalle in topes:
        if not _cabe(clave, maximo, ventana):
            logger.log(nivel, mensaje_log, *args_log)
            raise HTTPException(status_code=429, detail=detalle)
    for clave, *_ in topes:
        _anotar(clave)
