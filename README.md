# Colectivos JAHA - Mapa en vivo

App web local para ver la ubicacion en tiempo real de los colectivos de
JAHA (Paraguay), mas rapida que la app oficial.

Usa la API publica de JAHA (`https://www.jaha.com.py/rest_backend`), que
no requiere login para consultar lineas y posiciones, pero no tiene CORS
habilitado. Por eso este proyecto trae un pequeno servidor en Python que
sirve de proxy y evita el problema de CORS en el navegador.

## Uso

Requiere Python 3.10+ (sin dependencias externas).

```bash
python server.py
```

Despues abrir [http://localhost:8787](http://localhost:8787) en el navegador,
elegir una linea y ver las unidades actualizarse solas.

## Estructura

- `server.py` - servidor local que sirve la pagina y proxea las llamadas a la API de JAHA.
- `static/index.html` - mapa (Leaflet + OpenStreetMap) con el selector de linea, el trazado del recorrido y los marcadores de las unidades.

## Nota

Consume un endpoint interno no documentado de JAHA. Pensado para uso
personal; mantener el intervalo de actualizacion razonable (10s por
defecto) para no generar carga innecesaria en su servidor.
