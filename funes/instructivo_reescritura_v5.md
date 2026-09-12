# Instructivo de reescritura — v5a

Lo lee un agente que recibe una tanda de libros del catálogo de Funes y escribe,
para cada uno, los dos textos que el motor de recomendación vectoriza.

`v5a` = versión 5, escrita por agente. (`v4` era el prompt para Gemini vía
OpenRouter, que nunca llegó a correr; `abs2` y `v3` son las dos generaciones
anteriores que hay hoy en el catálogo.)

---

## 1. Qué escribís y qué decide cada cosa

Cada libro del catálogo tiene dos textos que se convierten en vectores. Cada uno
compite contra una mitad distinta de lo que dijo el lector:

| Escribís | Va a la columna | Se compara contra | Peso en el ranking |
|---|---|---|---|
| **`sinopsis`** — de qué trata | `embedding_sinopsis` | El **ancla**: el libro que el lector nombró como referencia, y la **corrección** que escribe cuando rechaza una recomendación | ~0,50 |
| **`experiencia`** — qué es leerlo | `embedding_experiencia` | El **perfil**: lo que eligió en las preguntas fijas, y las respuestas a las 2 preguntas profundas | ~0,50 |
| **`tema`** | `rasgos` (sin vector) | Nada: es un **filtro duro**, antes del coseno | binario |

Los dos textos que escribís se vectorizan en columnas propias, al lado del
vector del `abstracto` viejo, que no se toca. Una aclaración de tiempos: el
vector de la `sinopsis` empieza a decidir el ancla recién cuando el catálogo
entero esté reescrito —hoy el ancla todavía compite contra el abstracto, y
terminar esta migración es justamente lo que lo cambia—. El de `experiencia`
ya está en uso.

No hay nada más. No hay reglas de negocio, ni curaduría humana, ni un ranking
editorial detrás: **estos dos textos son literalmente lo único que decide qué
libro recibe cada lector.** Si el texto describe mal al libro, no hay motor que
lo arregle.

Dos consecuencias prácticas:

- Lo que escribas en `sinopsis` es lo que hace que este libro aparezca cuando
  alguien dice "quiero algo como *Sapiens*". Lo que escribas en `experiencia` es
  lo que lo hace aparecer cuando alguien pide "algo corto que me atrape".
- Los dos textos se comparan con **cosenos**, no con palabras clave. Lo que
  importa no es mencionar el término correcto: es que el texto entero apunte en
  una dirección distinta a la de los otros 2.000 libros del catálogo.

---

## 2. La regla que atraviesa todo: **elegir, no cubrir**

Este es el punto más importante del instructivo, y es donde fallaron las dos
versiones anteriores.

Un texto que menciona *todos* los ejes posibles —ritmo, tono, densidad, atención,
para quién, en qué momento— queda cerca del centro del espacio de vectores. Y lo
que está cerca del centro está cerca de *todo*: ese libro empieza a aparecer como
respuesta a cualquier consulta, sin ser la respuesta correcta de ninguna.

Está medido en este catálogo: el coseno promedio entre dos libros cualquiera de
la misma macro es **0,38**, cuando debería ser cercano a 0. En la prueba piloto,
un solo título se llevó el 10,8% de todas las recomendaciones — no porque fuera
el mejor para esas 26 personas, sino porque su texto no se comprometía con nada
en particular.

### Así se ve el problema (dos textos reales del catálogo, los dos malos)

> **La mujer justa:** "Intenso y reflexivo: cada monólogo exige una inmersión
> profunda en la psique de los personajes. Pide una lectura atenta, ideal para
> momentos de tranquilidad. Es una obra para quienes disfrutan de la exploración
> de las relaciones humanas y la complejidad de la memoria."

> **La herencia de Eszter:** "Intensa y contenida, la lectura de esta novela
> corta se desarrolla en un único día, pidiendo una atención constante a los
> detalles y a los matices psicológicos. El tono melancólico y la prosa analítica
> de Márai invitan a una inmersión profunda en la mente de la protagonista. Es
> una lectura para quienes disfrutan de la introspección y de las historias que
> exploran las complejidades de las relaciones humanas."

Son dos libros distintos y los textos son casi paráfrasis: los dos abren con un
adjetivo, los dos dicen "inmersión profunda", los dos dicen "atención", los dos
cierran con "para quienes disfrutan de… las relaciones humanas". Ninguna de las
dos frases está *mal*; el problema es que **podrían intercambiarse sin que nadie
se diera cuenta**, y eso es exactamente lo que el coseno ve.

### La regla

Elegí **las dos o tres cosas más distintivas** de este libro y escribí solo eso.
**Tenés permiso explícito de omitir el resto.** Si el ritmo de este libro no
tiene nada de particular, no menciones el ritmo. Si el tono es el esperable para
su género, no menciones el tono. Un texto de 50 palabras que dice una sola cosa
verdadera y rara vale más que uno de 70 que dice seis cosas ciertas y genéricas.

La pregunta a hacerse antes de escribir cada frase: **¿esta frase también es
cierta de otros cincuenta libros del catálogo?** Si la respuesta es sí, no la
escribas.

---

## 3. `sinopsis` — de qué trata (80 a 120 palabras)

El tema concreto, de qué época y lugar habla, quiénes aparecen, qué discute o qué
cuenta. **Cuanto más específico, mejor: nombres propios, lugares, años,
conceptos.** La especificidad es lo que separa este vector del montón.

Empezá directamente por el asunto, con el sujeto del que trata el libro.

> Ejemplo de buen comienzo: *"La caída de un imperio galáctico y el intento de un
> matemático de acortar los siglos de barbarie que vendrán, prediciendo el
> comportamiento de las masas."*

**Prohibido en la sinopsis:**

- La editorial.
- La biografía, los premios o la nacionalidad del autor.
- Decir qué *tipo* de libro es ("novela de", "libro de divulgación sobre").
- Hablar del lector o de la lectura — eso va en el otro campo.
- Adjetivos de valor: "imprescindible", "magistral", "fascinante".
- Cualquier dato que no puedas confirmar.
- Las palabras **"este libro", "la obra", "el autor", "la autora", "el texto",
  "esta novela", "este ensayo", "el volumen"** — *en ninguna parte del texto*, ni
  en el medio de una oración. Hablás del asunto, no del libro como objeto.

No empieces con "Este libro", "La obra", "El autor", "Esta novela", "Se trata
de", ni ninguna fórmula parecida. Si todas las fichas del catálogo empiezan
igual, todas terminan pareciéndose entre sí, que es justo lo que estamos tratando
de romper.

---

## 4. `experiencia` — qué es leerlo (50 a 70 palabras)

Acá aplicá la regla de la sección 2 con toda su fuerza: **dos o tres cosas, las
más distintivas, y nada más.**

Podés hablar de: el ritmo, si se lee de un tirón o de a poco, cuánta atención
pide, el tono, cómo está escrito, en qué momento o estado de ánimo cae bien, a
quién le suele gustar. **No es una lista para cubrir: es un menú del que elegís
dos o tres.** Mencionar los siete es el error.

Arrancá con un **adjetivo o un sustantivo**, nunca con un verbo ni con una
fórmula.

> Tres comienzos buenos, a propósito bien distintos entre sí:
> *"Denso pero corto: cada página pide releerse."*
> *"Ritmo de policial, aunque no lo sea."*
> *"Compañero de mesa de luz, de a diez páginas por noche."*

**Prohibido en la experiencia:**

- Arrancar con "Se lee", "Pide una lectura", "Es una lectura", "La lectura de
  este libro", "Se disfruta", "Invita a", "Requiere una".
- Las mismas palabras de objeto que la sinopsis: **"este libro", "la obra", "el
  autor", "el texto", "esta novela"** — en ninguna parte.
- **Contar de qué trata.** Eso ya está arriba. Si tu experiencia menciona
  personajes, trama, época o lugar, está invadiendo el otro campo — y cuando los
  dos textos se parecen, el motor pierde la única ventaja que tiene al separarlos.
- Los cierres genéricos: *"para quienes disfrutan de…"*, *"invita a una inmersión
  profunda"*, *"pide una lectura atenta"*, *"ideal para momentos de
  tranquilidad"*. Son las frases más repetidas del catálogo actual.

Escribilo en el idioma en que la gente habla de leer, no en el de una contratapa.

---

## 5. `tema` — el único campo estructurado, y el más caro de errar

Un valor de esta lista cerrada, según la macro del libro:

- **literatura:** `novela`, `genero`, `clasico`, `cuento`, `poesia`, `teatro`,
  `ensayo`, `memoria`, `otro`
- **historia:** `argentina`, `mundial`, `americana`, `belica`, `originarios`,
  `otro`
- **divulgacion:** `mente`, `vida`, `tecno`, `universo`, `cuerpo`, `tierra`,
  `numeros`, `otro`

**Esto no es una etiqueta decorativa: es un filtro duro que corre antes del
coseno.** Dos cosas que tenés que saber:

1. **En literatura, `otro` hace desaparecer el libro.** El filtro por forma es
   por igualdad estricta: un libro cuyo tema no mapea a ninguna de las cuatro
   formas que puede pedir el lector (`novela`, `genero`, `clasicos`, `breves`) no
   sale nunca, en ninguna consulta. No uses `otro` como "no estoy seguro" — usalo
   solamente cuando el libro genuinamente no es ninguna de las cuatro cosas (un
   recetario mal clasificado, un diccionario). Si dudás entre dos temas válidos,
   elegí el más probable: equivocarte entre dos formas es reversible, mandarlo a
   `otro` lo saca del catálogo.

2. **`cuento`, `poesia`, `teatro`, `ensayo` y `memoria` terminan todos en el
   mismo balde** (`breves`). El motor hoy no los distingue entre sí, así que no
   gastes esfuerzo afinando esa decisión: elegí el que corresponda y seguí.

La ficha te va a decir si el libro **ya resuelve su forma por género/subgénero**.
Cuando lo hace, tu `tema` es un respaldo y casi no se usa. Cuando la ficha dice
que **no resuelve**, tu `tema` es lo único que mantiene a ese libro visible.

---

## 6. `confianza` y `fuentes` — la defensa contra inventar

Antes de escribir, confirmá que la información corresponde a **ESTE** libro.
Los catálogos de librería cruzan fichas entre productos y hay homónimos: lo único
confiable del dato que recibís son el título y el autor.

Declará una confianza por libro:

- **`alta`** — confirmaste la obra y su contenido con al menos una fuente.
- **`media`** — la identificaste con razonable seguridad, con algún detalle sin
  confirmar.
- **`baja`** — **no pudiste confirmar de qué libro se trata.** El libro va a
  cuarentena y no se publica nada.

Con `alta` o `media`, citá las URLs que consultaste en `fuentes`.

**Si no sabés qué libro es, poné `baja` y no escribas los textos.** No rellenes
para llegar al mínimo de palabras. Un libro sin texto es mejor que un libro con
el texto de otro: el segundo se le recomienda a una persona real que después
descubre que el libro no tiene nada que ver.

Esto no es hipotético. El catálogo tenía cuatro fichas escritas así —una decía
textualmente *"no fue posible identificar con certeza la novela… se mantiene la
hipótesis más plausible según su ubicación física en el estante"*— y una de ellas
se convirtió en el libro más recomendado del catálogo apenas cambiamos una opción
del motor. Hubo que sacarlas a mano.

---

## 7. Lo que NO hay que escribir

Las versiones anteriores pedían ocho campos más (`tono`, `exigencia`, `ritmo`,
`humor`, `final`, `epoca`, `lugar`, `para`). **Ninguno de los ocho lo lee el
motor**: se guardaban y nadie los consultaba nunca. No los completes.

Tampoco toques el `abstracto` viejo: sigue existiendo para otra cosa (la voz de
Funes) y no es tu trabajo.

---

## 8. Lo que vas a recibir

Un archivo `.txt` con tu tanda. Cada libro viene así:

```
[7] Un mundo feliz
    autor: Aldous Huxley
    macro: literatura
    genero / subgenero: NOVELAS / CIENCIA FICCION FANTASTICA
    forma por genero: SI (genero) -> tu `tema` es respaldo
    paginas: 288
    ficha actual del catalogo: Novela de extension intermedia que describe una...
```

Sobre la **ficha actual**: es *dato*, no modelo de redacción. Su forma —empezar
con "Novela de", nombrar todos los ejes, cerrar con "el valor central es"— es
exactamente la que estamos rompiendo. Usala para saber de qué libro se trata,
nunca para copiarle el molde. Puede además estar equivocada o ser de otro libro.

---

## 9. Formato de salida

Escribí un único archivo JSON con el nombre que te indique el pedido:

```json
{
  "libros": [
    {
      "n": 7,
      "confianza": "alta",
      "fuentes": ["https://…"],
      "sinopsis": "…",
      "experiencia": "…",
      "tema": "genero"
    },
    {
      "n": 12,
      "confianza": "baja",
      "fuentes": [],
      "motivo_baja": "Hay al menos tres obras distintas con este título y el catálogo no trae ISBN."
    }
  ]
}
```

El `n` es el número entre corchetes de la ficha. Con `confianza: "baja"` alcanza
con `n`, `confianza` y `motivo_baja`.

---

## 10. Autochequeo antes de entregar

Revisá tu tanda completa —no libro por libro, **la tanda entera junta**— y
corregí antes de guardar:

1. **¿Cuántas de tus `experiencia` podrían intercambiarse entre libros sin que se
   note?** Si hay dos que dicen lo mismo con otras palabras, reescribí las dos.
2. **¿Se repiten arranques?** Si tres textos empiezan con el mismo adjetivo o la
   misma construcción, cambiá dos.
3. **¿Alguna `experiencia` cuenta de qué trata el libro?** Sacalo.
4. **¿Aparece "este libro", "la obra", "el autor", "el texto" en alguno de los
   dos campos?** Sacalo.
5. **¿Algún `tema` quedó en `otro`?** Confirmá que es genuino y no una duda.
6. **Largos:** sinopsis 80-120 palabras, experiencia 50-70.
7. **¿Alguna confianza `alta`/`media` sin fuentes?** O citás, o es `baja`.

Después de aplicarse, cada tanda se mide entera: se calcula el **coseno promedio
entre tus textos de `experiencia`**. La referencia **no** es el 0,38 del
`abstracto` — ese campo mide otra cosa (de qué trata, no qué es leerlo) y no es
el control correcto acá. El control real es la `experiencia` de la versión
anterior (`v3`/`abs2`) sobre muestras al azar del catálogo: da 0,59-0,63. Bajar
de ahí ya es una mejora real. **No fuerces el texto para llegar a 0,38**:
ningún campo de este catálogo, ni el mejor escrito hasta ahora, bajó tanto —
forzarlo produce textos raros por rareza, no textos distintivos.
