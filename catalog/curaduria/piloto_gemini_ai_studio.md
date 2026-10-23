# Piloto: Gemini 3.1 Pro + Grounding para Fase B

Evaluación de si Gemini 3.1 Pro (grounding con Google Search + URL context)
puede reemplazar o complementar a los agentes Claude que hacen Fase B
(buscar, verificar, escribir el abstracto -- ver `instructivo_abstractos.md`).
Estado: **pausado, no descartado** -- la calidad convenció, quedó bloqueado en
un problema de facturación, no de capacidad del modelo.

## Qué se probó

Dos rondas manuales en el Playground de Google AI Studio (`gemini-3.1-pro-preview`,
thinking level High, temperatura 1 -- sin tocar, ver nota abajo), sobre 29
libros en total:

- **9 libros difíciles**: los 6 en cuarentena permanente (`confianza: baja`,
  Fase B con Claude ya se había rendido) + 3 de los "inalcanzables" de
  historia (arte/crítica, tema=otro) que señaló Funes.
- **20 libros con datos sólidos**, ya terminados por Claude con
  `confianza: alta` -- elegidos a propósito para poder comparar cabeza a
  cabeza contra lo que Claude ya había escrito para los mismos libros.

### Ronda 1 (prompt sin lista negra de dominios)

Detectó las 3 sinopsis cruzadas del catálogo (ver `instructivo_abstractos.md`,
sección "ruido de datos") sin que se le señalara. Se contuvo correctamente en
el caso imposible ("Retazos", sin datos suficientes). Pero citó fuentes de
e-commerce (mercadolibre.com, cuspide.com) como si valieran -- exactamente lo
que el instructivo prohíbe -- y convergió en un cierre formulaico
("es un material insoslayable/imprescindible para...").

### Ronda 2 (prompt con regla dura + lista negra de dominios)

Con dos parches al prompt -- (a) regla dura "si `fuentes` queda vacío,
`confianza` tiene que ser `baja`, sin excepción" y (b) lista negra explícita
de dominios de venta -- 7 de 9 fuentes pasaron a ser editoriales reales o
Google Books. Quedó un agujero: un libro (`fernando pessoa|`) declaró
`confianza: media` con `fuentes: []`, violando la regla explícita. Aparte,
"ineludible" reemplazó a las fórmulas prohibidas como nuevo cierre repetido
-- la misma trampa que ya documenta el instructivo con los agentes Claude.

### Ronda de 20 (comparación contra Claude)

El hallazgo más fuerte del piloto: para **"Partículas elementales"**, el
catálogo tiene el autor cruzado con un libro homónimo de otra editorial
(FCE). Gemini lo detectó solo y bajó su propia confianza a `media` por las
dudas. Cotejado contra `enriquecimiento`: **Claude había encontrado
exactamente lo mismo, de forma independiente** (mismo autor real -- Gerard
't Hooft --, mismo título original, año con un año de diferencia entre
edición/traducción). Dos motores separados llegando al mismo dato correcto
contra lo que decía el catálogo es la evidencia más sólida de que esto no es
"suena convincente" sino verificación real.

Del resto de los 20, acuerdo alto en año/autor con Claude, salvo:

- Coincidencia de dominio en fuentes: ninguna cita a e-commerce en 17
  libros -- el guardrail de la ronda 2 sostiene a esta escala.

## Ajustes de prompt pendientes para una próxima ronda

Ninguno bloquea, pero conviene sumarlos antes de escalar:

1. **`titulo_original` para obras ya escritas en castellano**: en 6 de 20
   (Fresán, Svampa, Fanjul, Balmaceda, García Hernández, Golombek) lo llenó
   con el mismo título en español. Regla a agregar: si la obra es original
   en castellano, ese campo va vacío (así lo hace Claude).
2. **Años de obras antiguas (a.C.)**: en "Gorgias" puso `anio_obra: -380`;
   Claude lo deja en blanco. Hay que fijar una convención única.
3. **Año de obras póstumas / conferencias publicadas después**: en
   "Psicoanálisis y ciencias humanas" (Althusser) puso el año de la primera
   edición francesa póstuma (1993); Claude puso el año real de las
   conferencias (1964). Ambos defendibles, pero hay que elegir uno.
4. **"ineludible" y sinónimos**: sumarlos a la lista de cierres prohibidos.

## Una decisión de contenido sin resolver

El libro de Agatha Christie ("Y no quedo ninguno") trajo `titulo_original`
con el título original de 1939, que incluye un insulto racial ("Ten Little
Niggers"). Es dato históricamente correcto, pero no recomendado para guardar
tal cual en producción -- Claude, para el mismo libro, ya usa solo
"And Then There Were None". Pendiente: confirmar que se descarta el título
viejo (mi recomendación) o decidir lo contrario.

## Por qué quedó pausado: facturación, no calidad

El script (`catalog/curaduria/investigar_gemini.py`, ver más abajo) no pudo
correr contra la API real:

- `gemini-3.1-pro` vía API cruda: cupo **cero** en el tier gratis
  (`generate_content_free_tier_requests, limit: 0`).
- Cualquier modelo (incluso Flash) con `tools: [google_search, url_context]`:
  429 inmediato. El grounding en sí, no el modelo, es lo que no tiene cupo
  gratis -- confirmado comparando la misma llamada con y sin `tools`.
- Los límites más altos de la suscripción Google AI Pro aplican **adentro
  del Playground web**, no a una API key usada desde un script. Son cupos
  separados aunque los pague la misma suscripción.

Arreglo: activar Cloud Billing en el proyecto de esa API key (el botón
"Canjear USD30 en créditos" en AI Studio). No se activó todavía -- decisión
pendiente del usuario.

## La opción descartada: automatizar el Playground con Selenium

Se evaluó y se descartó (no por dificultad técnica solamente):

- No saca el techo de cupo, lo mueve: el límite diario del Playground es el
  mismo con o sin clicks automatizados.
- Automatizar el login de una cuenta de Google con Selenium pelea contra la
  detección de bots de Google (reCAPTCHA, bloqueos de "actividad inusual").
- La interfaz de AI Studio es una SPA sin contrato estable: cualquier
  rediseño rompe los selectores, sin aviso.
- Los términos de la API de Gemini dicen textualmente: *"Grounded Results
  cannot be accessed or collected by automated or programmatic means or used
  to create a database."* Aplica en principio a cualquier forma de guardar
  resultados de grounding en una base -- no solo a Selenium -- así que es
  algo para revisar con más cuidado antes de escalar por cualquier vía, no
  un argumento específico contra Selenium.

## Qué queda armado y usable

- **`catalog/curaduria/investigar_gemini.py`**: script completo (prompt v2 con
  los dos parches de la ronda 2, tabla propia `enriquecimiento_piloto_gemini`
  para no mezclar con producción, verificación automática de dominios de
  `groundingMetadata` contra una lista negra -- no confía en lo que el
  modelo *dice* que citó, lo cruza contra lo que Google realmente devolvió).
  Listo para correr en cuanto se decida activar billing; hasta entonces,
  sirve como referencia del prompt afinado en las dos rondas.
- **La clave del prompt** (system instruction) vive en ese mismo archivo,
  como constante `SYSTEM_INSTRUCTION` -- no hace falta mantenerla aparte.

## Próximo paso, si se retoma

1. Activar billing, correr `investigar_gemini.py probar` (9 libros de
   control) para confirmar que ahora sí completa sin 429.
2. Aplicar los 4 ajustes de prompt de la sección de arriba.
3. Sumar una etapa de verificación cruzada con agentes Claude (idea del
   usuario, independiente de si se sigue con Gemini o no): tomar una
   muestra de lo que sea que produzca el pipeline y que un agente con
   búsqueda audite puntualmente autor/año/título contra fuentes reales --
   sistematizando lo que se hizo a mano esta sesión comparando contra
   `enriquecimiento`.
