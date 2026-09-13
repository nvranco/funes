"""El dashboard de Funes por libreria: mismo tipo de numero que el piloto
(piloto.py) - proporcion con su margen, semaforo por umbral - pero recortado a
UNA libreria.

Por que no vive en piloto.py: ese modulo declara en su propio docstring que
"cada numero de aca alimenta una hipotesis [HF-1..HF-4]... si algo no se puede
trazar a una, no va". Una libreria mirando sus propias conversaciones no tiene
cohortes que proteger entre si -no hay sesgo de cortesia de un "conocido" que
excluir, es simplemente su publico real-, asi que este calculo no encaja ahi.
Lo que SI se reusa de piloto.py es lo generico: proporcion()/margen() (el
mismo problema estadistico en los dos lugares), y las constantes de huso y
umbral.
"""

import datetime

from app import db
from app.funes_chat import piloto

DIAS_TIMELINE = 7
EVENTOS_LIMITE = 60

# Mismas etiquetas que ve el lector en el chat (funes_chat.html), no los
# valores viejos de columna - ver el comentario de alla sobre el cambio de
# "Me la llevo" a "Precisa". La clave de color es la del dict TONOS
# (_funes_tarjetas.html).
_ETIQUETAS_VEREDICTO = {
    "me_la_llevo": ("Precisa", "verde"),
    "puede_ser": ("Dudosa", "gris"),
    "no_me_interesa": ("Floja", "rojo"),
}


async def calcular(libreria_id: int) -> dict:
    embudo = await _embudo(libreria_id)
    precision = await _precision(libreria_id)
    timeline = await _timeline(libreria_id, piloto.ahora().date())
    return {
        "alcance": embudo["sesiones"],
        "finalizacion": piloto.proporcion(
            embudo["con_recomendacion"], embudo["empezaron"], piloto.UMBRALES["HF-1"]),
        "precision": piloto.proporcion(
            precision["con_acierto"], precision["calificadas"], piloto.UMBRALES["HF-2"]),
        "timeline": timeline,
        "resumen_semana": {
            "total": sum(f["total"] for f in timeline),
            "verdes": sum(f["verdes"] for f in timeline),
            "amarillas": sum(f["amarillas"] for f in timeline),
            "rojas": sum(f["rojas"] for f in timeline),
        },
        "eventos": await _eventos(libreria_id),
    }


async def _embudo(libreria_id: int):
    return await db.pool().fetchrow(
        """
        SELECT count(DISTINCT s.id) AS sesiones,
               count(DISTINCT s.id) FILTER (WHERE s.q1 <> '') AS empezaron,
               count(DISTINCT r.sesion_id) AS con_recomendacion
        FROM funes_sesiones s
        LEFT JOIN funes_recomendaciones r ON r.sesion_id = s.id
        WHERE s.libreria_id = $1
        """,
        libreria_id,
    )


async def _precision(libreria_id: int):
    return await db.pool().fetchrow(
        """
        SELECT count(DISTINCT r.sesion_id) AS calificadas,
               count(DISTINCT r.sesion_id) FILTER (WHERE r.veredicto = 'me_la_llevo')
                   AS con_acierto
        FROM funes_recomendaciones r
        JOIN funes_sesiones s ON s.id = r.sesion_id
        WHERE r.veredicto IS NOT NULL
          AND r.creado_en::date >= $2
          AND s.libreria_id = $1
        """,
        libreria_id, piloto.CORTE_VEREDICTO_PUNTERIA,
    )


async def _timeline(libreria_id: int, hoy: datetime.date) -> list[dict]:
    """Una fila por dia de los ultimos DIAS_TIMELINE (incluye hoy), con el
    conteo de conversaciones CALIFICADAS (al menos un veredicto) segun su
    MEJOR valoracion - la valoracion superior pisa a las inferiores en el
    conteo, mismo mapeo ordinal que usa piloto.py para el test pareado HF-4.
    Dias sin ninguna calificada quedan en 0 y no se saltean: el grafico
    siempre tiene DIAS_TIMELINE barras."""
    desde = hoy - datetime.timedelta(days=DIAS_TIMELINE - 1)
    filas = await db.pool().fetch(
        f"""
        WITH por_sesion AS (
            SELECT s.id AS sesion_id,
                   (s.creado_en AT TIME ZONE '{piloto.ZONA}')::date AS dia,
                   max(CASE r.veredicto WHEN 'me_la_llevo' THEN 2
                                        WHEN 'puede_ser' THEN 1
                                        WHEN 'no_me_interesa' THEN 0 END) AS mejor
            FROM funes_sesiones s
            JOIN funes_recomendaciones r ON r.sesion_id = s.id
            WHERE s.libreria_id = $1
              AND r.veredicto IS NOT NULL
              AND r.creado_en::date >= $3
              AND (s.creado_en AT TIME ZONE '{piloto.ZONA}')::date >= $2
            GROUP BY s.id
        )
        SELECT dia,
               count(*) FILTER (WHERE mejor = 2) AS verdes,
               count(*) FILTER (WHERE mejor = 1) AS amarillas,
               count(*) FILTER (WHERE mejor = 0) AS rojas
        FROM por_sesion GROUP BY dia
        """,
        libreria_id, desde, piloto.CORTE_VEREDICTO_PUNTERIA,
    )
    por_dia = {f["dia"]: f for f in filas}
    dias = [desde + datetime.timedelta(days=i) for i in range(DIAS_TIMELINE)]
    crudos = []
    for d in dias:
        f = por_dia.get(d)
        v, a, r = (f["verdes"], f["amarillas"], f["rojas"]) if f else (0, 0, 0)
        crudos.append({"dia": d, "verdes": v, "amarillas": a, "rojas": r, "total": v + a + r})

    tope = max((f["total"] for f in crudos), default=0) or 1
    for f in crudos:
        f["pct_total"] = round(f["total"] / tope * 100, 1)
        f["pct_verde"] = round(f["verdes"] / f["total"] * 100, 1) if f["total"] else 0
        f["pct_amarilla"] = round(f["amarillas"] / f["total"] * 100, 1) if f["total"] else 0
        f["pct_roja"] = round(f["rojas"] / f["total"] * 100, 1) if f["total"] else 0
    return crudos


async def _eventos(libreria_id: int, limite: int = EVENTOS_LIMITE) -> list[dict]:
    """Una fila por conversacion que empezo (q1 <> ''): o abandono antes de
    ver un libro, o la recomendacion que "gano" esa conversacion. Cuando hay
    veredicto, gana el mejor -mismo criterio "la valoracion superior pisa a
    las inferiores" que usa _timeline()-; cuando ninguna se califico, se
    muestra la ULTIMA que se le mostro al lector (la de mayor `orden`), que
    es lo mas parecido a "en que quedo la charla".

    Se trae de la base mas reciente primero (para que el LIMIT recorte las
    ultimas N y no las primeras N de siempre) y se da vuelta antes de
    devolver: en pantalla es un log tipo chat, lo ultimo abajo del todo."""
    filas = await db.pool().fetch(
        f"""
        WITH candidatos AS (
            SELECT r.sesion_id, r.titulo, r.autor, r.veredicto,
                   row_number() OVER (
                       PARTITION BY r.sesion_id
                       ORDER BY
                           CASE r.veredicto WHEN 'me_la_llevo' THEN 2
                                            WHEN 'puede_ser' THEN 1
                                            WHEN 'no_me_interesa' THEN 0
                                            ELSE -1 END DESC,
                           r.orden DESC
                   ) AS rn
            FROM funes_recomendaciones r
            JOIN funes_sesiones s ON s.id = r.sesion_id
            WHERE s.libreria_id = $1
        )
        SELECT (s.creado_en AT TIME ZONE '{piloto.ZONA}') AS creado_en,
               EXISTS (SELECT 1 FROM funes_recomendaciones r2 WHERE r2.sesion_id = s.id)
                   AS con_recomendacion,
               c.titulo, c.autor, c.veredicto
        FROM funes_sesiones s
        LEFT JOIN candidatos c ON c.sesion_id = s.id AND c.rn = 1
        WHERE s.libreria_id = $1 AND s.q1 <> ''
        ORDER BY s.creado_en DESC
        LIMIT $2
        """,
        libreria_id, limite,
    )
    eventos = []
    for f in filas:
        if not f["con_recomendacion"]:
            eventos.append({"tipo": "abandono", "creado_en": f["creado_en"]})
            continue
        label, color = _ETIQUETAS_VEREDICTO.get(f["veredicto"], (None, None))
        eventos.append({
            "tipo": "recomendacion",
            "creado_en": f["creado_en"],
            "titulo": f["titulo"],
            "autor": f["autor"],
            "veredicto_label": label,
            "veredicto_color": color,
        })
    eventos.reverse()
    return eventos
