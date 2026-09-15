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
- **Avisos de proximidad**: se puede elegir una linea y un radio (1/5/10 km)
  para recibir una notificacion push cuando algun bus de esa linea entre en
  ese radio de tu ubicacion. Usa Web Push estandar (Service Worker + VAPID),
  asi que funciona aunque la pestana este cerrada, y soporta muchos
  dispositivos en paralelo sin mezclarse entre si (cada uno tiene su propia
  suscripcion en el servidor, con su propia linea/radio/ubicacion). Para
  evitar notificaciones repetidas, el servidor solo avisa en la transicion
  de "fuera del radio" a "dentro del radio"; si el bus se queda adentro no
  vuelve a avisar hasta que salga y vuelva a entrar.
- **Desvíos observados**: el servidor conserva muestras de GPS públicas de
  los buses por hasta 21 días. Al elegir una línea, el mapa compara ese
  historial con el trazado oficial y dibuja en fucsia discontinuo únicamente
  los tramos que quedan fuera de él. Para evitar falsos positivos por ruido
  GPS o una maniobra aislada, un tramo debe aparecer en al menos dos viajes
  distintos y superar 140 m antes de mostrarse. El historial es compartido:
  lo que se aprende mientras una persona consulta una línea queda disponible
  para las demás. Al principio estará vacío y se completa gradualmente.

## Estructura

- `server.py` - servidor local: proxea las llamadas a la API de JAHA (con
  una cache corta compartida por linea) y maneja las suscripciones de
  notificacion (VAPID, guardado en `data/`, hilo de fondo que chequea
  proximidad cada 20s y manda los push con `pywebpush`).
- `static/index.html` - mapa (Leaflet + OpenStreetMap), buscador de lineas,
  calculo de rumbo/sentido, y el panel de avisos de proximidad.
- `static/sw.js` - service worker minimo, solo recibe el push y muestra la
  notificacion.
- `data/` - generado en el primer uso (clave VAPID + suscripciones activas).
  También incluye `observed_bus_tracks.sqlite3`, el historial compartido de
  posiciones de buses. No se sube al repositorio.

## Notas y limitaciones

- Consume un endpoint interno no documentado de JAHA. Pensado para uso
  personal; mantener el intervalo de actualizacion razonable (10s por
  defecto) para no generar carga innecesaria en su servidor.
- El rumbo calculado depende de que tan seguido reporta posicion cada
  unidad; con reportes muy espaciados el suavizado ayuda pero no hace
  milagros.
- Web Push solo funciona en `https://` o en `localhost`. Para probarlo en
  tu PC anda perfecto; si esto se va a usar desde varios telefonos/PCs
  distintos de verdad, el servidor tiene que estar en un hosting con HTTPS.
- Para que el historial y las estelas compartidas sobrevivan a un reinicio en
  ese hosting, `data/observed_bus_tracks.sqlite3` debe estar en un volumen
  persistente. Si el proveedor descarta el disco en cada despliegue, también
  descartará ese aprendizaje compartido.
- El estado de las suscripciones vive en memoria + un JSON en `data/`; si
  el servidor se reinicia no se pierden (se recargan del archivo), pero no
  es una base de datos pensada para volumenes grandes.
