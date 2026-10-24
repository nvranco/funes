# Imágenes de preview de libros: servicios, almacenamiento y costos

Consulta realizada el 2026-10-24. Escala considerada: 1k a 20k libros, mayormente ediciones argentinas e hispanas. Postura legal: pragmática, con el riesgo marcado en cada fuente.

## 1. Estado actual del repo

- `funes_libros` (`funes/schema.sql:120-136`) tiene `isbn` (nullable), `titulo` y `autor`. No existe columna de portada de referencia ni de URL de imagen.
- La única imagen hoy es la foto que saca la librería. Se guarda en disco bajo `DATA_DIR` (volumen de Railway) y se sirve en `/api/{slug}/portada/{libro_id}.jpg` (`funes/routers/api_publico.py:55-80`), con una miniatura `_web.jpg` generada por `vision.generar_miniatura_portada`.
- Funes es un chat web. No usa la WhatsApp Cloud API: WhatsApp aparece como link `wa.me` y como preview Open Graph (`funes/routers/funes_chat.py:65-75`, imagen absoluta por https con hash de versión en la URL).
- `catalog/_ateneo_dataset/publicaciones_libros_ateneo.csv` (15.642 filas) trae `codigo_isbn` y la `url` de la ficha en la tienda de Ateneo. Es la mejor fuente de ISBN del repo.
- Riesgo propio: `schema.sql:543-562` lista libros en cuarentena porque el ISBN apuntaba a otra obra. Una portada traída por ISBN heredaría ese error, así que conviene aplicar solo a libros de confianza alta y comparar el título devuelto contra el propio cuando la API lo entrega.

Una portada de referencia por obra es un pipeline nuevo, no una extensión del existente.

## 2. Fuentes para obtener la portada

### Prueba de cobertura (30 ISBN reales del CSV de Ateneo, muestra con semilla fija)

La muestra tuvo 15 argentinos (978-987), 8 españoles (978-84) y 7 de otros prefijos. Con n=30 el margen de error es grande.

| Fuente | Hits | Detalle |
|---|---|---|
| Open Library (`covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false`) | 5/30 (17%) | Argentinos 2/15, españoles 3/8, otros 0/7. Un hit pesó 1,7 KB, probablemente inútil. |
| Google Books (endpoint de imagen `books.google.com/books/content?vid=ISBN:{isbn}&printsec=frontcover&img=1&zoom=1`) | 6/30 (20%) | 15 devolvieron un PNG placeholder, 9 un JPG gris genérico, 6 portadas reales de 128 px de ancho. |
| Google Books API oficial | No medible | HTTP 429 sin key (cuota anónima agotada). Requiere key propia. |
| Mercado Libre (`/sites/MLA/search`, `/products/search`) | 403 sin token | Requiere token OAuth. Con token se midió aparte, ver más abajo (87%). |

Las dos fuentes gratuitas globales cubren cada una cerca del 15-20% del catálogo argentino e hispano y casi no se solapan, por lo que encadenarlas rinde mejor (orden de 30% combinado en esta muestra). El resto necesita una fuente local.

### Servicios, uno por uno

| Servicio | Key | Precio | Límite | Cobertura ES/AR | Resolución | Almacenar o reenviar | Título + autor |
|---|---|---|---|---|---|---|---|
| Open Library | No | Gratis | 100 req/IP/5 min por ISBN | 17% medido | Hasta L (~500 px) | Hotlink permitido, bulk no. Zona gris | Sí, `search.json` y luego `cover_id` |
| Google Books | Sí | Gratis | ~1.000/día (dato de comunidad, sin confirmar oficial) | 20% medido | ~128 px (zoom mayor a veces) | Sin cláusula de caché, debe retirar contenido ante reclamo. Zona gris | Sí, `intitle:` + `inauthor:` |
| ISBNdb | Sí | USD 14,99 / 35,99 / 99,99 al mes | 1 a 5 req/s, 5k a 50k/día | No medida | Variable | Licencia de imagen sin confirmar | Sí |
| Mercado Libre | OAuth | Gratis | No verificado | 87% medido (26/30, catálogo de Funes) | ~800x1200, algunas 320x500 | Imágenes de vendedores. Zona gris | Sí, con ruido |
| LibraryThing | Dev key | Gratis | ~1.000/día, 1/s | No medida | 3 tamaños | Caché sí, redistribución no | No |
| Hardcover | Token | Gratis | Sin confirmar | Baja en español | Ok | Pide descargar y hospedar para uso profesional | Sí |
| Og:image de la ficha Ateneo | No | Gratis | Cortesía | Alta para el catálogo Ateneo | Buena | Zona gris, riesgo bajo-medio | No aplica |
| Amazon PA-API / Creators API | OAuth | Gratis | n/d | n/a | n/a | Restrictivo | No aplica |
| Goodreads, Syndetics | Cerrada, comercial | n/a | n/a | n/a | n/a | n/a | n/a |

Notas por servicio:

- **Open Library**: usar `covers.openlibrary.org` con `?default=false`, User-Agent identificado y un ritmo de 1 request cada 3 s o más. Para bulk, contactar a openlibrary@archive.org. No hay dump rolling de portadas. Fuente: https://openlibrary.org/dev/docs/api/covers.
- **Google Books**: key obligatoria para datos públicos (https://developers.google.com/books/docs/v1/using). Términos: https://developers.google.com/books/terms. Enviar la imagen por WhatsApp implica descargarla y reenviarla.
- **ISBNdb**: planes Basic / Premium / Pro, con 7 días de prueba. Con 1k a 20k libros alcanza una corrida de un mes del plan Basic (USD 14,99) para los libros que queden sin imagen, siempre que antes se confirme en sus términos que se pueden guardar las imágenes. La página de precios dio 403 al fetch directo, así que los precios vienen de resultados de búsqueda.
- **Mercado Libre**: la fuente más prometedora para ediciones argentinas (catálogo de productos buscable por GTIN igual a ISBN). Ya existe `bench/ml_spike.py` en el repo: revisar qué credenciales y resultados tiene antes de empezar de cero.
- **Amazon**: PA-API 5 deprecada, el reemplazo exige cuenta Associates con al menos 10 ventas calificadas en 30 días. Descartada.
- **Ficha Ateneo**: es scraping puntual de una página conocida por libro, no masivo. Técnicamente tiene el mayor hit rate para este catálogo, porque la imagen es la de la edición exacta. No se probó.
- **Casa del Libro, Cúspide, editoriales**: sin API pública. Su portada vía og:image sería scraping, fuera del alcance acordado.

### Prueba de la ficha Ateneo (20 fichas al azar, 1,5 s entre pedidos)

20/20 fichas devolvieron `og:image` y la imagen descargó bien (hosting Tiendanube, de 18 KB a 221 KB, mediana ~50 KB). Incluye ediciones argentinas, españolas y manga. No se revisó a ojo que cada imagen sea la tapa y no una foto de producto, ni su resolución en píxeles. Es el mayor acierto medido, pero solo vale para libros que están en el CSV de Ateneo.

### Mercado Libre

Medido el 2026-10-24 con el token OAuth de la app de Funes, sobre 30 ISBN al azar de `funes_libros` (el catálogo real de Funes): `/products/search?product_identifier=ISBN` y luego `/products/{id}` para leer `pictures`.

- 26/30 (87%) devolvieron producto con imagen. Los 4 restantes no tenían producto en el catálogo (Lisístrata, A la sombra de Einstein, El último mohicano, El campeón eterno).
- Resolución: la mayoría ronda 700-800 x 1200 px (máximo 1036x778). Cuatro vinieron chicas, de unos 320x500 px (Diario de una mudanza, Fortuna, Orestes, Si me necesitas, llamame). Sirve de sobra para WhatsApp y catálogos.
- En todos los casos la búsqueda por ISBN respondió HTTP 200, y el nombre del producto devuelto coincide a ojo con el título consultado. No se comparó con código y no se vieron las imágenes. Hay que validar título y autor al ingerir, por los ISBN en cuarentena.
- Varios libros traen más de una foto (hasta 5). La primera suele ser la tapa, pero no está garantizado.
- Es 5 veces mejor que Open Library o Google Books por separado (17-20%), sobre una muestra distinta (catálogo de Funes y no el CSV de Ateneo) y de tamaño chico.
- Términos de uso: las imágenes son de los vendedores y de ML. Reenviarlas o guardarlas es zona gris, sin revisar los términos de la API.
- Requiere el token OAuth de la app, que dura ~6 h y se refresca con `funes/funes_chat/precios.py::_token`.

### Cascada recomendada

1. Foto propia de la librería (`foto_portada_id`), si existe.
2. Ingesta offline, una sola vez, con copia propia. Funes nunca consulta en vivo desde el chat.
3. Por ISBN válido y confiable: Mercado Libre primero (87% medido), luego Google Books con key propia y `zoom` alto, luego Open Library.
4. Para libros del CSV de Ateneo: og:image de su `url` (20/20 medido). Puede ir antes o después de Mercado Libre según el riesgo legal que se acepte.
5. Sin ISBN o sin hit: Open Library `search.json` por título y autor (`cover_i`), luego Google `intitle:` + `inauthor:`. Aceptar solo con coincidencia de título y autor normalizados.
6. Rescate opcional: un mes de ISBNdb Basic.
7. Sin resultado: no enviar imagen. Descartar placeholders por tamaño fijo (Google: 1.269 B y 10.794 B; Open Library: archivos de menos de 5 KB) y por dimensiones mínimas.

### Riesgo legal

Las portadas tienen copyright de las editoriales. Cualquier almacenamiento y reenvío es zona gris. Open Library y Google toleran el hotlink pero no declaran permiso de redistribución. El volumen bajo y el uso de referencia reducen el riesgo práctico. Mantener un mecanismo para retirar una portada ante reclamo (Google lo exige).

## 3. Almacenamiento y entrega

Supuestos: imagen normalizada de 100 a 150 KB. 1k libros son ~0,15 GB y 20k son ~3 GB (6 GB si fueran de 300 KB). Tráfico: unas 5k vistas al mes con 1k libros y ~100k vistas (~15 GB de salida) con 20k.

| Opción | Storage | Egress y requests | CDN | Resize y WebP | Costo/mes con 1k | Costo/mes con 20k | Complejidad |
|---|---|---|---|---|---|---|---|
| Cloudflare R2 | USD 0,015/GB | Egress gratis. Clase A USD 4,50/M, clase B USD 0,36/M | Sí, con dominio en Cloudflare | Vía Images Transformations | USD 0 (free tier) | USD 0 | Baja (API S3, boto3) |
| AWS S3 + CloudFront | ~USD 0,023/GB (*) | CloudFront: 1 TB y 10M req gratis al mes | CloudFront | No nativo | ~USD 0 | ~USD 0,07 | Alta (IAM, OAC, distribución) |
| Cloudflare Images (guardado) | USD 5 cada 100k imágenes | USD 1 cada 100k entregadas | Sí | Variantes, WebP y AVIF auto | ~USD 5 (**) | ~USD 6 | Baja a media |
| Backblaze B2 | USD 6,95/TB, 10 GB gratis | Egress gratis hasta 3x lo almacenado | No propio (se combina con Cloudflare) | No | USD 0 | USD 0 | Media (dos servicios) |
| Railway volume | USD 0,15/GB | USD 0,05/GB de salida | No (lo sirve FastAPI) | Solo Pillow en código | ~USD 0,02 | ~USD 0,45 + ~USD 0,75 de egress | Ya en uso. Hobby limita a 5 GB |
| Postgres bytea | USD 0,15/GB | Cada GET pasa por API y DB | No | No | ~USD 0,02 | ~USD 0,45 + ~USD 0,75 | Fácil, pero infla backups y ocupa conexiones |
| Solo hotlink | USD 0 | USD 0 | El del origen | No | USD 0 | USD 0 | Mínima pero frágil: bloqueos, rate limit, URLs que cambian |

(*) El precio de S3 Standard no apareció en la página leída; USD 0,023/GB es el precio de lista conocido, sin verificar hoy.
(**) El plan Free de Images solo da 5.000 transformaciones únicas al mes, sin guardado ni entrega. No se encontró el mínimo mensual del plan pago.

Con Transformations sobre R2: 5.000 únicas gratis al mes, luego USD 0,50 cada 1.000. Con 1 o 2 variantes fijas, 20k libros son 20-40k transformaciones únicas, unos USD 17-20 una sola vez. La documentación no confirma `format=auto`, R2 como origen ni la ruta `/cdn-cgi/image/`: probar antes de depender de ello.

Fuentes (todas consultadas el 2026-10-24): https://developers.cloudflare.com/r2/pricing/, https://aws.amazon.com/s3/pricing/, https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/, https://developers.cloudflare.com/images/pricing/, https://developers.cloudflare.com/images/transform-images/, https://www.backblaze.com/cloud-storage/pricing, https://docs.railway.com/reference/pricing/plans.

## 4. Envío por WhatsApp

Hoy Funes no envía imágenes por WhatsApp. Si se agrega un bot con la Cloud API:

- Formatos: solo JPEG y PNG (8 bits), máximo 5 MB. WebP no vale para imágenes normales, así que normalizar a JPEG (`vision.redimensionar` ya lo hace). Fuente: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media.
- Subida a Meta: `POST /<PHONE_NUMBER_ID>/media` devuelve un media id. Los ids de subida expiran a los 30 días y hay que resubir o cachear id con fecha. Las URLs de descarga duran 5 minutos.
- Envío por link: el mensaje de imagen acepta un link https público además del id (conocimiento de la API, no verificado en esta consulta). Con una URL estable en CDN no hay que gestionar expiración.
- Costo: precio por mensaje entregado, por categoría y país. Una imagen dentro de la ventana de 24 h es mensaje de servicio. Según la página de precios, desde el 1-oct-2026 los de servicio se cobran salvo los primeros 1.000 al mes por número. No se pudo obtener la tarifa de Argentina: verificarla antes de decidir (https://developers.facebook.com/docs/whatsapp/pricing). Fuera de la ventana solo valen plantillas, que pueden llevar imagen en el header.

## 5. Patrón de cache

1. Resolver la fuente por ISBN o id de catálogo con la cascada de la sección 2.
2. Descargar una sola vez, con timeout, límite de bytes y verificación de Content-Type.
3. Normalizar con Pillow como `vision.redimensionar`: `exif_transpose`, RGB, lado mayor de 800 px, JPEG q80-85 progresivo (60-120 KB), más una variante de 300-400 px para listados. JPEG y no WebP si el mismo archivo va a WhatsApp.
4. Deduplicar por sha256 de los bytes normalizados. Clave inmutable `portadas/{sha[:2]}/{sha}.jpg` con `Cache-Control: public, max-age=31536000, immutable`. Portada nueva implica URL nueva.
5. Servir desde un dominio estable delante del bucket.
6. Reintentos con backoff, registro de errores y descarte de placeholders.

Esquema sugerido (el nombre `catalogo` es hipotético, no se revisó la tabla real):

```sql
CREATE TABLE IF NOT EXISTS portadas (
  id SERIAL PRIMARY KEY,
  sha256 CHAR(64) NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  ancho SMALLINT, alto SMALLINT,
  mime TEXT NOT NULL DEFAULT 'image/jpeg',
  storage_key TEXT NOT NULL,
  fuente TEXT,
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  wa_media_id TEXT,
  wa_media_expira TIMESTAMPTZ  -- subida + 30 dias
);
ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS portada_id INTEGER REFERENCES portadas(id);
ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS portada_origen_url TEXT;
ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS portada_estado TEXT; -- pendiente|ok|sin_portada|error
ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS portada_intentos SMALLINT DEFAULT 0;
ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS portada_revisada_en TIMESTAMPTZ;
```

## 6. Recomendación

- **Almacenamiento**: Cloudflare R2 con dominio propio en Cloudflare, subida con boto3 al descargar y normalizar, y dos variantes JPEG fijas generadas con Pillow (evita pagar y depender de Image Transformations). Costo esperado de USD 0 al mes en ambas escalas. Alternativa sin servicios nuevos: Railway volume, con tope de 5 GB en Hobby y sin CDN. Cloudflare Images pago y Postgres bytea no se justifican a esta escala. El hotlink sirve como fuente inicial, nunca como URL final.
- **Obtención**: ingesta offline con la cascada de la sección 2. Mercado Libre es la fuente principal (87% medido, ya autorizada en producción). Completar con Google Books (key propia) y Open Library, y con la ficha de Ateneo para los libros del CSV si se acepta el riesgo. ISBNdb queda como rescate de un mes.
- **WhatsApp**: cuando haya bot, enviar por link a la URL estable. Si la entrega por URL da problemas, subir a Meta y cachear `wa_media_id` con vencimiento de 30 días.

## 7. Pendientes sin cerrar

- Tarifa de WhatsApp para Argentina.
- Precio de S3 Standard verificado hoy.
- Si Image Transformations acepta R2 como origen con `format=auto`.
- Cobertura real de ISBNdb (requiere key). Mercado Libre (26/30) y Ateneo (20/20) ya medidos; falta confirmar a ojo que las imágenes sean tapas y que el producto de ML sea la misma edición.
- Términos de la API de Mercado Libre sobre guardar y reenviar imágenes de productos.
- Licencia de uso de las imágenes de ISBNdb.
- Cuota oficial de Google Books con key propia.
