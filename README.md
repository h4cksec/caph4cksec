# caph4cksec

Captura masiva de URLs y genera una galería `index.html` en tiempo real.

Sigue y registra las redirecciones 3xx. Acepta URLs, archivos de lista y entrada por tubería. Con `-d` ejecuta **gobuster** y captura en paralelo las rutas que van apareciendo. El `index.html` se actualiza solo y se publica en una URL local mientras dura la ejecución.

## Requisitos

- Python 3.10+
- Chromium + chromedriver
- Dependencias de Python:

```bash
pip install -r requirements.txt
```

- Opcional: `gobuster` (solo para `-d`)

## Instalación

```bash
git clone https://github.com/h4cksec/caph4cksec.git
cd caph4cksec
pip install -r requirements.txt
chmod +x caph4cksec.py
```

## Ejecución

```bash
# Una URL
./caph4cksec.py https://sitio.com

# Varias URLs
./caph4cksec.py https://sitio.com https://otro.com

# Desde un archivo (una URL por línea)
./caph4cksec.py lista.txt

# Por tubería
cat lista.txt | ./caph4cksec.py

# Descubrir rutas con gobuster y capturarlas en paralelo
./caph4cksec.py -d https://sitio.com

# Carpeta de salida personalizada
./caph4cksec.py lista.txt -o salida
```

Sin argumentos muestra el panel de ayuda:

```bash
./caph4cksec.py
```

## Opciones

| Opción | Descripción |
|---|---|
| `-i, --input ARCHIVO` | Archivo con una URL por línea |
| `-o, --output CARPETA` | Carpeta de salida |
| `-d, --discover URL` | Ejecuta gobuster y captura las rutas en paralelo (repetible) |
| `-w, --wordlist ARCHIVO` | Wordlist para gobuster |
| `--gobuster-threads N` | Hilos de gobuster (por defecto 60) |
| `--gobuster-timeout SEG` | Tiempo límite por petición de gobuster (por defecto 7) |
| `--gobuster-max-time SEG` | Tiempo máximo total por objetivo (por defecto 3600) |
| `--gobuster-extensions EXT` | Extensiones para gobuster, ej. `php,html,txt` |
| `--gobuster-insecure` | Ignora errores de certificado TLS en gobuster |
| `--gobuster-args ARGS` | Argumentos adicionales para gobuster |
| `--workers N` | Capturas en paralelo (por defecto 4) |
| `--retries N` | Intentos por URL (por defecto 1) |
| `--timeout SEG` | Tiempo límite de navegación (por defecto 15) |
| `--wait SEG` | Espera antes de capturar (por defecto 0.2) |
| `--width PX` / `--height PX` | Tamaño de la ventana |
| `--max-height PX` | Altura máxima de la captura |
| `--max-width PX` | Ancho máximo si se usa `--full-width` |
| `--full-width` | Incluye el ancho con desplazamiento horizontal |
| `--no-scroll` | No recorre la página antes de capturar |
| `--headful` | Muestra Chromium en pantalla |
| `--strict` | Devuelve código 1 si alguna URL falló |
| `--user-agent UA` | User-Agent usado por Chromium y gobuster |

## Salida

- `index.html` — galería con todas las capturas, se actualiza en tiempo real
- `*.png` — captura de cada URL
- Vista en vivo en `http://127.0.0.1:<puerto>/` mientras dura la ejecución

## Autor

h4cksec
