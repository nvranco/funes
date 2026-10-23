# El pipeline de recomendación de Funes

La fuente de verdad de cómo está armado es el diagrama
[`pipeline-recomendacion.drawio`](pipeline-recomendacion.drawio) (se abre en
draw.io o diagrams.net). Tiene tres páginas: el pipeline completo de punta a
punta, la anatomía de cada meta-prompt y la tabla de vectores y pesos. Este
documento no lo repite: guarda lo que el diagrama no puede decir, que es **por
qué está así**, con lo que medimos.

Si cambiás una pregunta, un filtro, un peso o un prompt en
`app/funes_chat/nucleo.py`, actualizá el diagrama en el mismo commit.

El piloto expone una sola rama (`nucleo.MACRO_UNICA = "literatura"`, 2.167 de
los 3.576 libros). Historia y divulgación están escritas y probadas, pero no se
ofrecen; volver a ellas es poner la constante en `None`.

## El flujo, en una línea

Todos arrancan con **q2** (desafío y longitud) y la pregunta del ancla **q4a**
(¿un libro o autor que se parezca?). Quien nombra una lectura va directo a la
recomendación (**camino A**). Quien no, elige entre escribir lo que quiere
(**B**) o que le hagan preguntas q1, q1b y q3 (**C**). El ancla genera dos
textos, qué es el libro y cómo se lee, y en A y B esos dos textos reemplazan a
lo que q1 y q3 habrían aportado.

## Filtros duros y filtros blandos

Un **filtro duro** saca libros del pool antes de calcular un solo coseno; un
**filtro blando** solo inclina el puntaje. El coseno no sabe decir «esto no»,
así que lo que el lector pide de forma inequívoca se recorta (macro, forma,
páginas, libro ya leído) y lo que es cuestión de grado se pondera (tono, valor
central, lo que quiere repetir).

Orden de aplicación, de lo más elegido explícitamente a lo más derivado:
macro, libro nombrado (por título, nunca por autor), forma (q1b), ya leídos
(por título y autor, así otra edición tampoco vuelve) y banda de páginas (q2:
corto ≤260, intermedio 180-420, largo ≥300; solapadas a propósito porque un
libro de 200 páginas es «corto» para uno e «intermedio» para otro).

**Escalera de aflojado.** Si el pool cae por debajo de 80 libros se suelta un
filtro, de abajo hacia arriba: primero páginas, después forma. La macro nunca se
afloja. Se registra cuál en `funes_sesiones.filtro_aflojado`.

Dos reglas, por el mismo motivo (un dato faltante no puede ser una condena): un
libro sin `nro_paginas` nunca se excluye por páginas, y uno sin `rasgos` nunca
se descarta por tema.

## Un vector por señal, no un texto concatenado

Concatenar todo en un solo texto hace desaparecer lo corto: el ancla eran 9
palabras sobre 56 y era inerte (cuatro referencias de nichos opuestos
devolvían 6 de 8 libros iguales). Cada señal tiene su vector y su peso
(tabla en la página 3 del diagrama). Medido con `bench/ancla_peso.py`, un peso
de 0,5 baja ese solapamiento de 6,0/8 a 4,0/8 y pierde solo 1 libro de 8 en
fidelidad al tema; de 0,65 para arriba el ancla empieza a pisar la macro.

Los pesos fijos pueden sumar más de 1, así que se escalan hasta un tope de 0,75
para todo lo que no es perfil y el perfil se queda con el resto (nunca menos de
0,25). En los caminos A y B, sin q1 ni q3, el ancla pesa 0,75 y se reparte 55 %
contra qué es el libro y 45 % contra cómo se lee.

**Las opciones fijas se vectorizan como «consultas»**, no como la etiqueta del
botón. La etiqueta corta de «los seres vivos» daba coseno 0,443 y traía
psicología; la consulta larga en idioma de catálogo da 0,600 y trae bichos y
plantas.

**La corrección no compite contra la lista corta.** Los 8 candidatos se eligen
con lo que el lector dijo antes de ver un libro; si «dame otra» tuviera que
elegir ahí adentro, no podría traer nada nuevo, y cuanto más precisa la
corrección menos serviría. Por eso, cuando hay corrección, se rankea el pool
entero (unos 650 libros en literatura con banda intermedia, ~280 ms).

## Lo que aprendimos de los prompts

- **Nombre y rasgo se preguntan por separado (q4a, q4b).** Con una sola caja el
  LLM tenía que adivinar si le daban una obra o un rasgo, y a quien elogiaba el
  sistema de casas de Harry Potter le devolvía novelas de magos. Preguntado por
  separado, desambiguar pasa a ser estructura.
- **Todo pedido se reescribe en positivo.** Los embeddings no entienden
  negaciones: «nada denso» empuja hacia lo denso. Aplica al reformulador del
  rechazo y al resumen de un libro leído.
- **El reformulador clasifica antes de invertir.** Con un único ejemplo («muy
  denso y viejo» → «ágil y contemporáneo») aprendió que «viejo» siempre es queja
  y le dio vuelta a quien pedía «un clásico». Ahora primero distingue pedido de
  queja, y ante la duda lo trata como pedido.
- **La voz no recibe el campo `experiencia` del libro.** Habla de lo que exige
  leerlo (1.286 de 1.366 textos mencionan atención, ritmo o calma) y empuja al
  modelo hacia lo único que el mensaje tiene prohibido decir.
- **El empujón es un campo JSON aparte**, no una palabra marcadora dentro del
  texto: el modelo llegó a escribir «EMPUNJON:», «EMPUPON:» y «EMPUMON:», y el
  regex dejaba la marca rota en la pantalla del lector.
- **Ninguna llamada auxiliar es un requisito.** Si falla el ancla se embebe el
  texto crudo; si falla la opinión sobre un leído, la charla sigue sin esa señal.
- **La búsqueda web es solo el respaldo del ancla**, cuando el modelo admite que
  no reconoce la referencia: sale unas 59 veces más cara que la llamada pelada.
  El precio de referencia se apagó por esa razón (era el 86 % del costo de una
  conversación y nadie lo pedía); el botón «¿Dónde lo consigo?» abre una
  búsqueda de Google.

## Interruptores del motor y por qué están así

| mecanismo | estado | motivo |
|---|---|---|
| `_DOS_VECTORES` (perfil y ajuste comparan contra `experiencia`) | **on** | Un contrafactual sobre 236 recomendaciones del piloto: los 3 libros que más se repetían (21, 9 y 7 de 236) caen a 4, 0 y 0. Contra el juez offline la primera recomendación acertaba menos (4 a 1 sobre 18), así que quedó pendiente medirlo con veredictos reales. |
| `_PESO_DIVERSIDAD = 0,25` (castigar parecerse a lo ya mostrado; mismo autor = 1) | **on** | El juez offline no lo veía, pero en el piloto dos lectores rechazaron un libro de Dan Brown y recibieron otro del mismo autor. |
| `_CENTRAR` (restar la media de la macro) | **off** | Arregla los hubs (máximo 20→13) pero el juez baja de 3,21 a 2,96: la dirección común de una macro dice también «esto es una novela», y restarla corre el ranking hacia los libros raros. |
| `_ANCLA_CONTRA_SINOPSIS` | **off** | El ancla y la corrección comparan contra el abstracto, no contra la sinopsis. Está por medir en un catálogo completo en v5a. |

## Cómo mantenerlo

- `bench/probar_motor.py` es gratis: filtros, pesos, validadores y los tres
  caminos. `--http` agrega un smoke contra el server local.
- Los benchmarks que llaman al LLM (`bench/simular.py`, `bench/probar_ancla.py`)
  gastan crédito de OpenRouter: no se lanzan sin avisar ni con el código a medio
  tocar.
- El diagrama es la fuente de verdad: si lo que el código hace y lo que el
  diagrama dice no coinciden, hay que arreglar uno de los dos en el mismo commit.
