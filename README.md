# Colectivos JAHA - Mapa en vivo

App web local para ver la ubicacion en tiempo real de los colectivos de
JAHA (Paraguay), mas rapida que la app oficial.

Usa la API publica de JAHA (`https://www.jaha.com.py/rest_backend`), que
no requiere login para consultar lineas y posiciones, pero no tiene CORS
habilitado. Por eso este proyecto trae un pequeno servidor en Python que
sirve de proxy y evita el problema de CORS en el navegador.

## Uso

Requiere Python 3.10+.

```bash
python server.py
```

Despues abrir [http://localhost:8787](http://localhost:8787) en el navegador,
buscar una linea (por nombre o numero) y ver las unidades actualizarse solas.

Para las notificaciones de proximidad (opcional, ver mas abajo):

```bash
pip install pywebpush
```

Sin esa dependencia el resto de la app funciona igual; solo se desactiva
el boton de aviso de proximidad.

## Funciones

- **Buscador y favoritos**: filtra las lineas por nombre o numero, y permite
  marcar favoritas (guardadas en el navegador).
- **Rumbo real de cada unidad**: la flecha de cada bus no solo usa el rumbo
  que informa JAHA (`sen`), sino que ademas calcula su propio rumbo a partir
  del desplazamiento real entre posiciones sucesivas, con suavizado (para
  no saltar por ruido de GPS) y un umbral minimo de desplazamiento. Cuando
  hay recorrido cargado para la linea, tambien intenta reconocer si la
  unidad va de "Ida" o de "Vuelta" comparando su avance contra las
  polilineas de la ruta.
- **Filtro por ramal**: cuando una línea tiene varios recorridos aparece una
  barra horizontal con “Todos” y cada ramal, junto con su cantidad de buses
  activos. El cambio es inmediato: oculta los otros trazados, marcadores,
  estelas y desvíos sin volver a descargar la línea ni detener su seguimiento.
  Las observaciones nuevas guardan también el nombre del recorrido para que
  los desvíos persistentes permanezcan asociados a su ramal.
- **Planificador desde la ubicación del usuario**: permite usar el GPS como
  origen y devuelve recorridos directos de JAHA y Más con ramal, sentido,
  punto de subida, punto de bajada y una duración aproximada. El servidor une
  las secciones de cada recorrido y proyecta origen y destino sobre los
  segmentos completos, sin depender solamente de los vértices publicados. Al
  elegir una opción, el mapa diferencia las conexiones a pie, el tramo útil en
  colectivo y las unidades que circulan en el sentido necesario para el viaje.
  En el teléfono, al marcar puntos manualmente el panel se minimiza para dejar
  libre el mapa; desde la barra compacta se puede cambiar A/B, buscar o ampliar
  el formulario sin perder lo elegido.
- **Avisos de proximidad**: se puede elegir una línea y un radio (500 m, 1/2/5/10 km)
  para recibir una notificacion push cuando algun bus de esa linea entre en
  ese radio del último punto de ubicación guardado. Usa Web Push estándar
  (Service Worker + VAPID),
  asi que funciona aunque la pestana este cerrada, y soporta muchos
  dispositivos en paralelo sin mezclarse entre si (cada uno tiene su propia
  suscripcion en el servidor, con su propia linea/radio/ubicacion). Para
  evitar notificaciones repetidas, el servidor conserva el estado al recargar,
  tolera pequeñas oscilaciones del GPS y no interpreta una respuesta vacía
  como alejamiento inmediato. Al tocar el aviso se abre esa línea en el mapa.
  Con la página cerrada, el aviso usa el último punto de ubicación guardado.
- **Trayectos observados compartidos**: el servidor consulta las líneas aunque
  no haya una página abierta, compara cada bus con su recorrido oficial y
  conserva solamente la evidencia que queda fuera. La ruta oficial nunca se
  modifica. La estela GPS reciente se muestra en granate; los tramos ajustados
  a calles usan naranja para 2 buses distintos, azul para 3 y morado para 4 o
  más. Una pasada se identifica por línea, unidad y viaje, de modo que varios
  visitantes mirando el mismo bus no aumentan el conteo.
- **Líneas sin recorrido oficial**: sus movimientos se guardan como “recorrido
  observado”, claramente separado de un desvío. Una descarga fallida no se
  interpreta como ausencia de ruta y se conserva el último trazado conocido.
- **Ajuste a calles**: el servidor conserva las posiciones originales durante
  siete días, descarta saltos GPS aislados y procesa las estelas en una cola
  acotada. Solo una geometría de alta confianza puede convertirse en línea; el
  agregado confirma cuántas unidades y pasadas recorrieron cada tramo.
- **Comentarios privados**: desde Ajustes se puede enviar un problema o
  sugerencia con nombre o sin él. Los datos técnicos son opcionales y requieren
  una casilla marcada expresamente. Un código privado permite al remitente
  eliminar su comentario sin iniciar sesión.

## Estructura

- `server.py` - servidor local: proxea las llamadas a la API de JAHA (con
  una cache corta compartida por linea) y maneja las suscripciones de
  notificacion (VAPID, guardado en `data/`, hilo de fondo que chequea
  proximidad cada 20s y manda los push con `pywebpush`).
- `observed_routes.py` - recolector central, detección de salida y regreso,
  almacenamiento de pasadas, ajuste a calles y estadísticas compartidas.
- `feedback.py` - validación y almacenamiento privado de comentarios y sesiones
  de administración.
- `static/index.html` - mapa (Leaflet + OpenStreetMap), buscador de lineas,
  calculo de rumbo/sentido, y el panel de avisos de proximidad.
- `static/observed-routes.js` - representa las estelas y alternativas enviadas
  por el servidor; el navegador no puede crear ni inflar evidencia compartida.
- `static/feedback.js` - formulario de comentarios y consentimiento opcional.
- `static/admin-feedback.html` - panel privado para revisar comentarios.
- `static/sw.js` - service worker minimo, solo recibe el push y muestra la
  notificacion.
- `data/` - generado en el primer uso (clave VAPID + suscripciones activas).
  También incluye `observed_bus_tracks.sqlite3`, el historial compartido de
  posiciones de buses, y `feedback.sqlite3`. No se sube al repositorio.

## Railway y almacenamiento persistente

La aplicación usa automáticamente el directorio indicado por
`RAILWAY_VOLUME_MOUNT_PATH`. En Railway hay que agregar un volumen al servicio
con punto de montaje `/data`; Railway crea esa variable automáticamente. El
archivo SQLite, las estelas y las suscripciones quedan entonces dentro del
volumen y sobreviven a reinicios y despliegues.

El servicio debe ejecutarse con una sola réplica mientras use SQLite. La ruta
`/api/observed-health` sirve como healthcheck. La configuración incluida en
`railway.json` inicia el servidor, configura ese healthcheck y reinicia el
proceso si falla.

Variables opcionales:

La plantilla [`.env.example`](.env.example) contiene la configuración inicial
recomendada. La clave real debe cargarse como secreto del entorno, no editarse
dentro de archivos versionados.

- `DATA_DIR`: reemplaza la ubicación de datos fuera de Railway.
- `OBSERVED_COLLECTOR=0`: desactiva el recolector central.
- `OBSERVED_POLL_SECONDS`: intervalo objetivo del recolector (mínimo 15 s;
  predeterminado 30 s). El ciclo real también depende del número de líneas.
- `PLANNER_REFRESH_SECONDS`: intervalo para renovar el catálogo geográfico
  compartido del planificador (mínimo 5 minutos; predeterminado 30 minutos).
- `GEOCODER_SEARCH_URL`: endpoint HTTPS de búsqueda compatible con Nominatim,
  propio o contratado. Sin configurarlo se eligen puntos en el mapa o con GPS;
  buscar por dirección muestra un aviso. Las búsquedas son explícitas (botón o
  Enter), con caché y límite compartido, no consultas mientras se escribe.
  No se utiliza por defecto el servidor público de Nominatim. Confirmar las
  condiciones y la atribución exigidas por el proveedor antes de configurarlo.
- `OSRM_MATCH_URL`: servidor compatible con la API Match de OSRM. El valor
  predeterminado es `https://router.project-osrm.org`; para más volumen se
  recomienda una instancia propia.
- `TOMTOM_API_KEY`: clave de **Snap to Roads API**, configurada como secreto
  exclusivamente en el servidor. Si está presente, TomTom se selecciona
  automáticamente; nunca se incluye en JavaScript ni en respuestas HTTP.
- `MATCH_PROVIDER`: `tomtom`, `osrm` o `disabled`. Si se elige TomTom, el modo
  sombra está activo por defecto: valida y guarda temporalmente la geometría,
  pero todavía no modifica las líneas visibles.
- `MATCH_SHADOW_MODE=0`: publica los tramos validados. Activarlo debe hacerse
  después de revisar los resultados de la fase inicial.
- `TOMTOM_MONTHLY_LIMIT` y `TOMTOM_WEEKLY_LIMIT`: topes duros de solicitudes
  (predeterminados: 2200 al mes y 400 por ejecución semanal). Las respuestas
  recuperadas de caché no consumen presupuesto.
- `TOMTOM_WEEKDAY` y `TOMTOM_HOUR`: día (`0` lunes a `6` domingo) y hora local
  de Asunción para el lote; los valores predeterminados son domingo a las 03:00.
- `MATCH_RUN_ON_START=1`: ejecuta un primer lote al iniciar. Se recomienda
  dejarlo apagado hasta verificar la configuración y el presupuesto.
- `OBSERVED_RAW_RETENTION_DAYS`: retención de puntos GPS y trabajos pendientes
  (predeterminado: 7). Las líneas confirmadas y sus conteos agregados permanecen.
- `MATCH_MAX_PENDING_JOBS` y `MATCH_MAX_ATTEMPTS`: límites de cola y reintentos
  (predeterminados: 5000 y 8).
- `OBSERVED_MIN_CONFIRMED_BUSES` y `OBSERVED_MIN_CONFIRMED_PASSES`: evidencia
  mínima para mostrar un tramo confirmado (predeterminado: 2 buses o 2 pasadas).

El panel privado `/admin/feedback` incluye una pestaña **Auditoría de rutas**.
Durante el modo sombra permite comparar los puntos GPS de los últimos siete
días con la geometría ajustada, revisar confianza, rechazos, cola y presupuesto.
Los puntos dejan de estar disponibles al vencer la retención; las métricas y
geometrías de auditoría se conservan durante 30 días. El botón **Validar 1
ahora** ejecuta una prueba controlada de un solo recorrido, exige sesión y token
CSRF, solo funciona en modo sombra y respeta el límite mensual.
- `FEEDBACK_ADMIN_PASSWORD`: contraseña exclusiva del panel de comentarios,
  con al menos 16 caracteres. Sin ella el panel no permite iniciar sesión.

## Comentarios y privacidad

El formulario está en **Ajustes → Enviar comentario o sugerencia**. El nombre y
la línea son opcionales. Los mensajes solo aparecen en el panel privado
`/admin/feedback`; conocer esa URL no concede acceso. Sin contraseña configurada
la función pública permanece desactivada. El panel exige la contraseña,
limita los intentos de acceso y usa una sesión de 12
horas con cookie `HttpOnly`, `SameSite=Strict` y `Secure` en Railway. Las
acciones administrativas también exigen un token de protección.

El formulario no adjunta datos técnicos salvo que el usuario marque la casilla
correspondiente. En ese caso se envían solo tipo de dispositivo, sistema,
navegador, ancho aproximado de pantalla y versión web. El servidor no agrega
IP, ubicación del usuario ni identificadores del dispositivo a la base de
comentarios. Los mensajes se conservan hasta 180 días; los datos técnicos se
eliminan a los 30 días. Cada envío devuelve un código privado para eliminar
el mensaje y sus datos técnicos desde el mismo formulario. El código debe
guardarse: no puede recuperarse desde el servidor.

Antes de activar esta función en Railway, hay que crear una copia manual del
volumen de producción desde **bus → Backups → Create Backup** y configurar
`FEEDBACK_ADMIN_PASSWORD` como variable privada del servicio. No se debe
guardar la contraseña en Git ni enviar a otras personas una copia de `data/`.

## Notas y limitaciones

- El planificador ofrece viajes directos, sin transbordos ni horarios. Los
  minutos no incluyen espera o tráfico. Las caminatas son distancias en línea
  recta y los puntos sugeridos no certifican una parada habilitada: verificar
  accesibilidad y dónde está permitido subir. No une tramos desconectados ni
  presupone un circuito continuo porque las cabeceras estén cerca.
- Validación local: `python -m unittest discover -s tests -q` y
  `node --test tests/test_planner_frontend.cjs`.
- Consume un endpoint interno no documentado de JAHA. Pensado para uso
  personal; mantener el intervalo de actualizacion razonable (10s por
  defecto) para no generar carga innecesaria en su servidor.
- El rumbo calculado depende de que tan seguido reporta posicion cada
  unidad; con reportes muy espaciados el suavizado ayuda pero no hace
  milagros.
- Web Push solo funciona en `https://` o en `localhost`. Para probarlo en
  tu PC anda perfecto; si esto se va a usar desde varios telefonos/PCs
  distintos de verdad, el servidor tiene que estar en un hosting con HTTPS.
- Sin un volumen de Railway, la aplicación funciona pero el historial se
  pierde al reemplazar el contenedor durante un despliegue.
- El estado de las suscripciones vive en memoria + un JSON en `data/`; si
  el servidor se reinicia no se pierden (se recargan del archivo), pero no
  es una base de datos pensada para volumenes grandes.
