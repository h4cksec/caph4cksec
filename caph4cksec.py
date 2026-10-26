#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "urls.txt"
DEFAULT_OUTPUT = BASE_DIR / "salida"
DEFAULT_WORDLIST = Path("/usr/share/wordlists/dirbuster/directory-list-2.3-medium.txt")
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
FALLBACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
STATUS_LABEL = {"ok": "OK", "parcial": "PARCIAL", "error": "ERROR"}
GOBUSTER_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
GOBUSTER_URL_RE = re.compile(
    r"(?P<url>https?://[^\s]+?)\s+\(Status:\s*(?P<status>\d{3})\)",
    re.IGNORECASE,
)
CONSOLE_LOCK = threading.Lock()


def console_print(message: str, to_stderr: bool = False) -> None:
    with CONSOLE_LOCK:
        print(message, file=sys.stderr if to_stderr else sys.stdout, flush=True)


def atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class RedirectHop:
    status: int
    url: str
    location: str


@dataclass
class Capture:
    index: int
    url: str
    final_url: str
    title: str
    file: str
    status: str
    attempts: int
    duration: float
    width: int
    height: int
    truncated: bool
    placeholder: bool
    method: str
    error: str
    redirects: list[RedirectHop] = field(default_factory=list)


@dataclass
class Discovery:
    base_url: str
    wordlist: str
    command: str
    routes: list[str] = field(default_factory=list)
    error: str = ""


class DriverPool:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.local = threading.local()
        self.drivers: list[webdriver.Chrome] = []
        self.lock = threading.Lock()

    def _build(self) -> webdriver.Chrome:
        options = Options()
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium:
            options.binary_location = chromium
        if not self.args.headful:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--allow-insecure-localhost")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-breakpad")
        options.add_argument("--disable-component-update")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-features=Translate,MediaRouter,OptimizationHints")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-sync")
        options.add_argument("--mute-audio")
        options.add_argument("--no-service-autorun")
        options.add_argument("--password-store=basic")
        options.add_argument("--use-mock-keychain")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-ipc-flooding-protection")
        options.add_argument("--force-device-scale-factor=1")
        options.add_argument("--no-first-run")
        options.add_argument("--lang=es-ES")
        options.add_argument(f"--window-size={self.args.width},{self.args.height}")
        options.add_argument(f"--user-agent={self.args.user_agent}")
        options.page_load_strategy = "eager"
        options.unhandled_prompt_behavior = "dismiss"
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        options.add_experimental_option(
            "prefs",
            {
                "download.prompt_for_download": False,
                "profile.default_content_setting_values.notifications": 2,
                "profile.default_content_setting_values.geolocation": 2,
            },
        )
        service_path = shutil.which("chromedriver")
        service = (
            Service(executable_path=service_path, log_output=os.devnull)
            if service_path
            else Service(log_output=os.devnull)
        )
        driver = webdriver.Chrome(options=options, service=service)
        driver.set_page_load_timeout(self.args.timeout)
        driver.set_script_timeout(self.args.timeout)
        driver.implicitly_wait(0)
        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        return driver

    def get(self) -> webdriver.Chrome:
        driver = getattr(self.local, "driver", None)
        if driver is None:
            driver = self._build()
            self.local.driver = driver
            with self.lock:
                self.drivers.append(driver)
        return driver

    def reset(self) -> None:
        driver = getattr(self.local, "driver", None)
        if driver is None:
            return
        try:
            driver.quit()
        except Exception:
            pass
        with self.lock:
            if driver in self.drivers:
                self.drivers.remove(driver)
        self.local.driver = None

    def close_all(self) -> None:
        with self.lock:
            drivers = list(self.drivers)
            self.drivers.clear()
        for driver in drivers:
            try:
                driver.quit()
            except Exception:
                pass


class SpanishHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "uso: "
        super().add_usage(usage, actions, groups, prefix)


class SpanishArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        translations = {
            "the following arguments are required:": "faltan estos argumentos:",
            "unrecognized arguments:": "argumentos no reconocidos:",
            "expected one argument": "se esperaba un valor",
            "invalid choice:": "opción no válida:",
            "invalid int value:": "valor entero no válido:",
            "invalid float value:": "valor decimal no válido:",
            "argument ": "argumento ",
        }
        for source, target in translations.items():
            message = message.replace(source, target)
        super().error(message)


def parse_args() -> argparse.Namespace:
    parser = SpanishArgumentParser(
        add_help=False,
        formatter_class=SpanishHelpFormatter,
        description=(
            "Autor: h4cksec. "
            "Captura todas las URLs y genera un index.html con la galería. "
            "Sigue y registra las redirecciones 3xx. "
            "Acepta URLs, archivos de lista y entrada por tubería. "
            "Con -d ejecuta gobuster y captura en paralelo las rutas que van apareciendo. "
            "El index.html se actualiza en tiempo real y se publica en una URL local mientras dura la ejecución."
        ),
        epilog=(
            "Autor: h4cksec\n\n"
            "Ejemplos:\n"
            "  %(prog)s lista.txt\n"
            "  %(prog)s https://sitio.com\n"
            "  %(prog)s -d https://sitio.com\n"
            "  %(prog)s -d https://sitio.com -w /ruta/wordlist.txt -o salida\n"
            "  cat lista.txt | %(prog)s"
        ),
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y sale")
    parser.add_argument(
        "urls",
        nargs="*",
        metavar="URL_O_ARCHIVO",
        help="URLs o archivos de lista. Ejemplo: herramienta.py lista.txt o herramienta.py https://sitio.com",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=None,
        metavar="ARCHIVO",
        help=f"Archivo con una URL por línea (por defecto: {DEFAULT_INPUT}).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="CARPETA",
        help=f"Carpeta de salida (por defecto: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "-d",
        "--discover",
        action="append",
        metavar="URL",
        default=[],
        help="Ejecuta gobuster contra esta URL y captura en paralelo las rutas que aparecen. Se puede repetir.",
    )
    parser.add_argument(
        "-w",
        "--wordlist",
        type=Path,
        default=None,
        metavar="ARCHIVO",
        help=f"Lista de palabras para gobuster (por defecto: {DEFAULT_WORDLIST}).",
    )
    parser.add_argument(
        "--gobuster-threads",
        type=int,
        default=60,
        metavar="N",
        help="Hilos de gobuster (por defecto: 60).",
    )
    parser.add_argument(
        "--gobuster-timeout",
        type=float,
        default=7.0,
        metavar="SEG",
        help="Tiempo límite por petición de gobuster en segundos (por defecto: 7).",
    )
    parser.add_argument(
        "--gobuster-max-time",
        type=float,
        default=3600.0,
        metavar="SEG",
        help="Tiempo máximo total por objetivo de gobuster en segundos (por defecto: 3600).",
    )
    parser.add_argument(
        "--gobuster-extensions",
        default="",
        metavar="EXT",
        help="Extensiones para gobuster, separadas por comas. Ejemplo: php,html,txt.",
    )
    parser.add_argument(
        "--gobuster-insecure",
        action="store_true",
        help="Ignora errores de certificado TLS en gobuster.",
    )
    parser.add_argument(
        "--gobuster-args",
        default="",
        metavar="ARGUMENTOS",
        help="Argumentos adicionales para gobuster, escritos entre comillas.",
    )
    parser.add_argument("--workers", type=int, default=4, metavar="N", help="Capturas en paralelo (por defecto: 4; súbelo si tu equipo lo permite).")
    parser.add_argument("--retries", type=int, default=1, metavar="N", help="Intentos por URL (por defecto: 1).")
    parser.add_argument("--timeout", type=float, default=15.0, metavar="SEG", help="Tiempo límite de navegación en segundos (por defecto: 15).")
    parser.add_argument("--wait", type=float, default=0.2, metavar="SEG", help="Espera final antes de capturar, en segundos (por defecto: 0.2).")
    parser.add_argument("--width", type=int, default=1440, metavar="PX", help="Ancho de la ventana.")
    parser.add_argument("--height", type=int, default=900, metavar="PX", help="Alto de la ventana.")
    parser.add_argument("--max-height", type=int, default=20000, metavar="PX", help="Altura máxima de la captura.")
    parser.add_argument("--max-width", type=int, default=3000, metavar="PX", help="Ancho máximo si se usa --full-width.")
    parser.add_argument("--full-width", action="store_true", help="Incluye el ancho con desplazamiento horizontal.")
    parser.add_argument("--no-scroll", action="store_true", help="No recorre la página antes de capturar.")
    parser.add_argument("--headful", action="store_true", help="Muestra Chromium en pantalla.")
    parser.add_argument("--strict", action="store_true", help="Devuelve código 1 si alguna URL falló.")
    parser.add_argument("--user-agent", default=DEFAULT_UA, metavar="UA", help="Agente de usuario (User-Agent) usado por Chromium y gobuster.")
    args = parser.parse_args()
    args.workers = max(1, args.workers)
    args.retries = max(1, args.retries)
    args.timeout = max(5.0, args.timeout)
    args.wait = max(0.0, args.wait)
    args.width = max(320, args.width)
    args.height = max(200, args.height)
    args.max_height = max(args.height, args.max_height)
    args.max_width = max(args.width, args.max_width)
    args.gobuster_threads = max(1, args.gobuster_threads)
    args.gobuster_timeout = max(1.0, args.gobuster_timeout)
    args.gobuster_max_time = max(10.0, args.gobuster_max_time)
    return args



def normalize_url(value: str) -> str | None:
    url = value.strip().strip("<>").strip("\"'")
    if not url or url.startswith("#"):
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        return url
    return f"https://{url}"


def read_list_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo de URLs: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"La ruta es un directorio, no un archivo: {path}")
    return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()


def looks_like_url(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if value.startswith("//"):
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return True
    first = value.split("/", 1)[0]
    if first.lower().startswith(("www.", "localhost", "127.", "0.0.0.0", "[::1]")):
        return True
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$", first):
        return True
    return bool(re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?$", first))


def looks_like_file(value: str) -> bool:
    value = value.strip().strip("\"'")
    if not value:
        return False
    if value.startswith("//"):
        return False
    if value.startswith(("/", "./", "../", "~/")):
        return True
    suffix = Path(value).suffix.lower()
    if suffix in {".txt", ".lst", ".list", ".csv", ".urls", ".url", ".json", ".jsonl"}:
        return "/" not in value and "\\" not in value
    return False


def load_urls(args: argparse.Namespace, allow_default: bool = True) -> tuple[list[str], str]:
    raw: list[str] = []
    sources: list[str] = []

    for token in args.urls:
        if token == "-":
            piped = sys.stdin.read()
            if piped.strip():
                raw.extend(piped.splitlines())
                sources.append("stdin")
            continue
        path = Path(token).expanduser()
        if path.exists():
            raw.extend(read_list_file(path))
            sources.append(str(path.resolve()))
            continue
        if looks_like_file(token):
            raise FileNotFoundError(f"No existe el archivo de URLs: {path}")
        if looks_like_url(token):
            raw.append(token)
            sources.append("argumentos")
            continue
        raw.append(token)
        sources.append("argumentos")

    if args.input is not None:
        path = args.input.expanduser()
        if str(path) == "-":
            piped = sys.stdin.read()
            if piped.strip():
                raw.extend(piped.splitlines())
                sources.append("stdin")
        else:
            raw.extend(read_list_file(path))
            sources.append(str(path.resolve()))

    piped_stdin = ""
    if not sys.stdin.isatty():
        try:
            piped_stdin = sys.stdin.read()
        except Exception:
            piped_stdin = ""
    if piped_stdin.strip():
        raw.extend(piped_stdin.splitlines())
        sources.append("stdin")

    if not raw and not sources and not args.urls and args.input is None:
        if allow_default and DEFAULT_INPUT.exists():
            raw = read_list_file(DEFAULT_INPUT)
            sources.append(str(DEFAULT_INPUT.resolve()))
        elif allow_default:
            raise FileNotFoundError(
                f"No se indicaron URLs y no existe {DEFAULT_INPUT}. "
                "Crea ese archivo, pasa una lista o una URL como argumento, o usa una tubería."
            )

    urls: list[str] = []
    for line in raw:
        normalized = normalize_url(line)
        if normalized:
            urls.append(normalized)
    unique_sources = list(dict.fromkeys(sources))
    return urls, ", ".join(unique_sources) if unique_sources else "sin entrada"


def resolve_wordlist(args: argparse.Namespace) -> Path:
    path = (args.wordlist or DEFAULT_WORDLIST).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"No existe la lista de palabras de gobuster: {path}. "
            "Indica otra con -w /ruta/wordlist.txt."
        )
    if path.is_dir():
        raise IsADirectoryError(f"La lista de palabras debe ser un archivo, no un directorio: {path}")
    return path.resolve()


def parse_gobuster_output(output: str) -> list[str]:
    routes: list[str] = []
    for raw_line in re.split(r"[\r\n]+", GOBUSTER_ANSI_RE.sub("", output)):
        match = GOBUSTER_URL_RE.search(raw_line)
        if not match:
            continue
        normalized = normalize_url(match.group("url").rstrip(".,;)]"))
        if normalized:
            routes.append(normalized)
    return list(dict.fromkeys(routes))


def run_gobuster(
    base_url: str,
    wordlist: Path,
    args: argparse.Namespace,
    on_route: Callable[[str], None] | None = None,
) -> tuple[Discovery, list[str]]:
    command = [
        "gobuster",
        "dir",
        "-u",
        base_url,
        "-w",
        str(wordlist),
        "-q",
        "--no-progress",
        "--no-color",
        "-e",
        "-t",
        str(args.gobuster_threads),
        "--timeout",
        f"{args.gobuster_timeout:g}s",
        "-a",
        args.user_agent,
    ]
    if args.gobuster_extensions:
        command.extend(["-x", args.gobuster_extensions])
    if args.gobuster_insecure:
        command.append("-k")
    if args.gobuster_args:
        command.extend(shlex.split(args.gobuster_args))

    command_text = shlex.join(command)
    console_print("")
    console_print(f"Gobuster: {base_url}")
    console_print(f"  Lista de palabras: {wordlist}")
    console_print(
        f"  Hilos: {args.gobuster_threads} | "
        f"Tiempo límite por petición: {args.gobuster_timeout:g}s"
    )
    console_print(f"  Comando: {command_text}")
    console_print("  Analizando...")

    output_lines: list[str] = []
    live_routes: list[str] = []
    error = ""
    elapsed = 0.0
    process: subprocess.Popen[str] | None = None

    def log(message: str, to_stderr: bool = False) -> None:
        console_print(message, to_stderr=to_stderr)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        error = f"No se pudo ejecutar gobuster: {clean_error(exc)}"
        log(f"  Advertencia: {error}", to_stderr=True)
    else:
        def read_output() -> None:
            stream = process.stdout
            if stream is None:
                return
            for raw_line in stream:
                line = GOBUSTER_ANSI_RE.sub("", raw_line).strip()
                if not line:
                    continue
                output_lines.append(line)
                match = GOBUSTER_URL_RE.search(line)
                if match:
                    normalized = normalize_url(match.group("url").rstrip(".,;)]"))
                    if normalized and normalized not in live_routes:
                        live_routes.append(normalized)
                        log(f"  [gobuster] {line}")
                        if on_route is not None:
                            try:
                                on_route(normalized)
                            except Exception as exc:
                                log(
                                    f"  Advertencia: no se pudo encolar {normalized}: {clean_error(exc)}",
                                    to_stderr=True,
                                )
                else:
                    log(f"  [gobuster] {line}")

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        last_heartbeat = 0.0
        timed_out = False
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > args.gobuster_max_time:
                    timed_out = True
                    process.kill()
                    break
                if elapsed - last_heartbeat >= 5.0:
                    last_heartbeat = elapsed
                    route_word = "ruta" if len(live_routes) == 1 else "rutas"
                    log(f"  [gobuster] {elapsed:.0f}s, {len(live_routes)} {route_word}...")
                time.sleep(0.15)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            reader.join(timeout=3)
            elapsed = time.monotonic() - started

        if timed_out:
            error = f"gobuster superó el tiempo máximo de {args.gobuster_max_time:g}s"
            log(f"  Advertencia: {error}", to_stderr=True)
        elif process.returncode not in (0, None):
            details = " ".join(output_lines[-4:]).strip()
            error = f"gobuster terminó con código {process.returncode}"
            if details:
                error = f"{error}: {details}"
            error = clean_error(RuntimeError(error))
            log(f"  Advertencia: {error}", to_stderr=True)

    routes = list(dict.fromkeys(live_routes + parse_gobuster_output("\n".join(output_lines))))
    console_print(f"  Rutas encontradas: {len(routes)} ({elapsed:.1f}s)")
    discovery = Discovery(
        base_url=base_url,
        wordlist=str(wordlist),
        command=command_text,
        routes=routes,
        error=error,
    )
    return discovery, routes

def discover_urls(
    args: argparse.Namespace,
    wordlist: Path,
    on_route: Callable[[str], None] | None = None,
) -> tuple[list[str], list[Discovery]]:
    if shutil.which("gobuster") is None:
        raise RuntimeError("No se encontró gobuster en el sistema.")
    discovered: list[str] = []
    discoveries: list[Discovery] = []
    for raw_target in args.discover:
        base_url = normalize_url(raw_target)
        if not base_url:
            console_print(
                f"Advertencia: URL de descubrimiento no válida: {raw_target}",
                to_stderr=True,
            )
            continue
        if on_route is not None:
            on_route(base_url)
        discovery, routes = run_gobuster(base_url, wordlist, args, on_route=on_route)
        discoveries.append(discovery)
        discovered.append(base_url)
        discovered.extend(routes)
        if on_route is not None:
            for route in discovery.routes:
                on_route(route)
    return list(dict.fromkeys(discovered)), discoveries


def safe_filename(index: int, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or "local"
    path = parsed.path.strip("/")
    stem = f"{host}_{path}" if path else host
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = stem.strip("._-")[:80] or "pagina"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{index:03d}_{stem}_{digest}.png"


def clean_error(exc: BaseException, limit: int = 500) -> str:
    message = re.sub(r"\s+", " ", str(exc)).strip()
    if not message:
        message = exc.__class__.__name__
    return message[:limit]


def session_dead(exc: BaseException) -> bool:
    message = str(exc).lower()
    markers = (
        "invalid session id",
        "chrome not reachable",
        "session deleted",
        "disconnected",
        "no such window",
        "target window already closed",
        "failed to decode response",
        "connection refused",
    )
    return any(marker in message for marker in markers)


def header_value(headers: object, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def clear_performance_logs(driver: webdriver.Chrome) -> None:
    try:
        driver.get_log("performance")
    except Exception:
        pass


def extract_redirects(driver: webdriver.Chrome) -> list[RedirectHop]:
    try:
        entries = driver.get_log("performance")
    except Exception:
        return []

    hops: list[RedirectHop] = []
    seen: set[tuple[int, str]] = set()
    for entry in entries:
        try:
            message = json.loads(entry.get("message", "")).get("message", {})
        except Exception:
            continue
        method = message.get("method")
        params = message.get("params", {})
        if method == "Network.requestWillBeSent":
            if params.get("type") != "Document":
                continue
            redirect = params.get("redirectResponse")
            if not redirect:
                continue
            try:
                status = int(redirect.get("status") or 0)
            except Exception:
                continue
            if not 300 <= status < 400 or status == 304:
                continue
            from_url = str(redirect.get("url") or "")
            to_url = str(params.get("request", {}).get("url") or "")
            location = header_value(redirect.get("headers", {}), "location") or to_url
            key = (status, from_url)
            if key in seen:
                continue
            seen.add(key)
            hops.append(RedirectHop(status=status, url=from_url, location=location or to_url))
        elif method == "Network.responseReceived":
            if params.get("type") != "Document":
                continue
            response = params.get("response", {})
            try:
                status = int(response.get("status") or 0)
            except Exception:
                continue
            if not 300 <= status < 400 or status == 304:
                continue
            from_url = str(response.get("url") or "")
            location = header_value(response.get("headers", {}), "location")
            if not location:
                continue
            key = (status, from_url)
            if key in seen:
                continue
            seen.add(key)
            hops.append(RedirectHop(status=status, url=from_url, location=location))
    return hops


def dismiss_alerts(driver: webdriver.Chrome) -> None:
    for _ in range(3):
        try:
            driver.switch_to.alert.dismiss()
        except Exception:
            break


def wait_for_page(driver: webdriver.Chrome, args: argparse.Namespace) -> None:
    try:
        WebDriverWait(driver, min(3.0, max(1.0, args.timeout / 5)), poll_frequency=0.05).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except Exception:
        pass
    try:
        WebDriverWait(driver, 1.0, poll_frequency=0.05).until(
            lambda d: d.execute_script("return !document.fonts || document.fonts.status === 'loaded'")
        )
    except Exception:
        pass


def is_browser_error_page(driver: webdriver.Chrome) -> bool:
    try:
        return bool(
            driver.execute_script(
                "return !!(document.querySelector('#main-frame-error, .neterror, #error-information-popup-container'));"
            )
        )
    except Exception:
        return False


def auto_scroll(driver: webdriver.Chrome, args: argparse.Namespace) -> None:
    try:
        driver.execute_async_script(
            """
            const delay = arguments[0];
            const settle = arguments[1];
            const maxSteps = arguments[2];
            const done = arguments[arguments.length - 1];
            if (document.documentElement.scrollHeight <= window.innerHeight + 3) {
                done();
                return;
            }
            let steps = 0;
            function step() {
                window.scrollBy(0, Math.max(350, window.innerHeight * 0.9));
                steps += 1;
                const atEnd = window.scrollY + window.innerHeight
                    >= document.documentElement.scrollHeight - 3;
                if (atEnd || steps >= maxSteps) {
                    window.scrollTo(0, 0);
                    window.setTimeout(done, settle);
                } else {
                    window.setTimeout(step, delay);
                }
            }
            step();
            """,
            30,
            120,
            50,
        )
    except Exception:
        pass


def layout_size(driver: webdriver.Chrome, args: argparse.Namespace) -> tuple[int, int]:
    try:
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        width = int(math.ceil(float(content.get("width", args.width))))
        height = int(math.ceil(float(content.get("height", args.height))))
        return max(1, width), max(1, height)
    except Exception:
        try:
            width = int(driver.execute_script("return Math.max(document.documentElement.scrollWidth, window.innerWidth);"))
            height = int(driver.execute_script("return Math.max(document.documentElement.scrollHeight, window.innerHeight);"))
            return max(1, width), max(1, height)
        except Exception:
            return args.width, args.height


def validate_png(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        size = image.size
        image.verify()
    if size[0] < 1 or size[1] < 1:
        raise RuntimeError("la imagen generada está vacía")
    return int(size[0]), int(size[1])


def take_screenshot(driver: webdriver.Chrome, path: Path, args: argparse.Namespace) -> tuple[int, int, bool, str]:
    content_width, content_height = layout_size(driver, args)
    target_width = args.width
    if args.full_width:
        target_width = min(max(args.width, content_width), args.max_width)
    target_height = min(max(1, content_height), args.max_height)

    heights: list[int] = []
    for candidate in (target_height, 16000, 12000, 8000, args.height):
        candidate = max(1, min(candidate, target_height))
        if candidate not in heights:
            heights.append(candidate)

    last_error: BaseException | None = None
    for height in heights:
        try:
            clip = {
                "x": 0,
                "y": 0,
                "width": target_width,
                "height": height,
                "scale": 1,
            }
            response = driver.execute_cdp_cmd(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": clip,
                },
            )
            data = base64.b64decode(response["data"])
            path.write_bytes(data)
            width, saved_height = validate_png(path)
            truncated = saved_height < content_height or width < content_width
            return width, saved_height, truncated, "full"
        except Exception as exc:
            last_error = exc

    try:
        driver.save_screenshot(str(path))
        width, height = validate_png(path)
        truncated = height < content_height or width < content_width
        return width, height, truncated, "viewport"
    except Exception as exc:
        last_error = exc

    raise RuntimeError(f"no se pudo generar la captura: {clean_error(last_error or RuntimeError('error desconocido'))}")


def load_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if mono:
        names = ["DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"]
    elif bold:
        names = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
    else:
        names = ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    roots = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts"),
    ]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.exists():
                try:
                    return ImageFont.truetype(str(candidate), size)
                except Exception:
                    pass
            for found in root.rglob(name):
                try:
                    return ImageFont.truetype(str(found), size)
                except Exception:
                    pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def make_placeholder(path: Path, url: str, error: str, args: argparse.Namespace, index: int) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        width, height = args.width, args.height
        image = Image.new("RGB", (width, height), "#0b1220")
        draw = ImageDraw.Draw(image)
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = (
                int(11 + 7 * ratio),
                int(18 + 10 * ratio),
                int(32 + 14 * ratio),
            )
            draw.line((0, y, width, y), fill=color)

        draw.rectangle((0, 0, width, 8), fill="#ef4444")
        draw.rounded_rectangle((56, 54, width - 56, height - 54), radius=22, outline="#263245", width=2)

        title_font = load_font(52, bold=True)
        label_font = load_font(20, bold=True)
        body_font = load_font(26)
        mono_font = load_font(22, mono=True)
        small_font = load_font(18)

        draw.text((88, 92), "ERROR DE CAPTURA", font=label_font, fill="#fca5a5")
        draw.text((88, 128), "No se pudo renderizar esta URL", font=title_font, fill="#f8fafc")
        draw.text(
            (88, 202),
            "Se generó esta imagen de respaldo para garantizar una captura por cada enlace.",
            font=body_font,
            fill="#94a3b8",
        )

        box_left, box_top, box_right, box_bottom = 88, 270, width - 88, height - 150
        draw.rounded_rectangle((box_left, box_top, box_right, box_bottom), radius=16, fill="#0f172a", outline="#334155", width=1)
        draw.text((box_left + 26, box_top + 22), "URL", font=label_font, fill="#38bdf8")

        url_width = box_right - box_left - 52
        url_lines = textwrap.wrap(url, width=max(20, int(url_width / 12)), break_long_words=True, break_on_hyphens=False)
        y = box_top + 58
        for line in url_lines[:5]:
            draw.text((box_left + 26, y), line, font=mono_font, fill="#e2e8f0")
            y += 30
        if len(url_lines) > 5:
            draw.text((box_left + 26, y), "...", font=mono_font, fill="#e2e8f0")
            y += 30

        y += 24
        draw.text((box_left + 26, y), "DETALLE", font=label_font, fill="#f87171")
        y += 34
        detail_width = box_right - box_left - 52
        detail = error or "Error de navegación sin detalle."
        detail_lines = textwrap.wrap(detail, width=max(20, int(detail_width / 14)), break_long_words=True, break_on_hyphens=False)
        for line in detail_lines[:4]:
            draw.text((box_left + 26, y), line, font=small_font, fill="#cbd5e1")
            y += 24

        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        draw.text((88, height - 105), f"Captura #{index:03d}  |  {stamp}", font=small_font, fill="#64748b")
        image.save(path, "PNG")
        return validate_png(path)
    except Exception:
        try:
            path.write_bytes(FALLBACK_PNG)
            return validate_png(path)
        except Exception:
            path.write_bytes(FALLBACK_PNG)
            return 1, 1


def capture_one(index: int, url: str, out_dir: Path, args: argparse.Namespace, pool: DriverPool) -> Capture:
    started = time.monotonic()
    filename = safe_filename(index, url)
    path = out_dir / filename
    attempts = 0
    last_error = ""
    final_url = url
    title = ""

    for attempt in range(1, args.retries + 1):
        attempts = attempt
        try:
            driver = pool.get()
        except Exception as exc:
            last_error = f"No se pudo iniciar Chromium: {clean_error(exc)}"
            if attempt < args.retries:
                time.sleep(min(1.5 * attempt, 5.0))
            continue

        navigation_error = ""
        partial = False
        try:
            driver.get("about:blank")
            try:
                driver.delete_all_cookies()
            except Exception:
                pass
            clear_performance_logs(driver)
            driver.get(url)
        except TimeoutException:
            partial = True
            navigation_error = f"Timeout de navegación después de {args.timeout:g}s."
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except WebDriverException as exc:
            partial = True
            navigation_error = clean_error(exc)
            if session_dead(exc):
                pool.reset()
                last_error = navigation_error
                if attempt < args.retries:
                    time.sleep(min(1.5 * attempt, 5.0))
                continue
        except Exception as exc:
            partial = True
            navigation_error = clean_error(exc)

        try:
            current_url = driver.current_url
            if not current_url or current_url == "about:blank":
                raise RuntimeError(navigation_error or "La página no cargó.")
            dismiss_alerts(driver)
            wait_for_page(driver, args)
            if is_browser_error_page(driver):
                raise RuntimeError(navigation_error or "Chromium mostró una página de error.")
            if not args.no_scroll:
                auto_scroll(driver, args)
            if args.wait:
                time.sleep(args.wait)
            final_url = driver.current_url or url
            title = driver.title or ""
            width, height, truncated, method = take_screenshot(driver, path, args)
            redirects = extract_redirects(driver)
            return Capture(
                index=index,
                url=url,
                final_url=final_url,
                title=title,
                file=filename,
                status="parcial" if partial else "ok",
                attempts=attempt,
                duration=round(time.monotonic() - started, 3),
                width=width,
                height=height,
                truncated=truncated,
                placeholder=False,
                method=method,
                error=navigation_error,
                redirects=redirects,
            )
        except Exception as exc:
            last_error = clean_error(exc)
            if session_dead(exc):
                pool.reset()
            if attempt < args.retries:
                time.sleep(min(0.8 * attempt, 3.0))

    width, height = make_placeholder(path, url, last_error or "Error desconocido.", args, index)
    return Capture(
        index=index,
        url=url,
        final_url=final_url,
        title=title,
        file=filename,
        status="error",
        attempts=attempts,
        duration=round(time.monotonic() - started, 3),
        width=width,
        height=height,
        truncated=False,
        placeholder=True,
        method="placeholder",
        error=last_error or "Error desconocido.",
    )


def render_card(item: Capture) -> str:
    host = urlparse(item.url).netloc or item.url
    status_label = STATUS_LABEL.get(item.status, item.status.upper())
    card_classes = f"card status-{item.status}"
    title = item.title or host
    meta_parts = [
        f"{item.width}x{item.height} px",
        f"{item.duration:.1f} s",
        f"{item.attempts} intento{'s' if item.attempts != 1 else ''}",
    ]
    if item.truncated:
        meta_parts.append("recortada")
    if item.placeholder:
        meta_parts.append("respaldo")
    if item.redirects:
        meta_parts.append(f"{len(item.redirects)} redirección{'es' if len(item.redirects) != 1 else ''} 3xx")
    meta = " &middot; ".join(html.escape(part) for part in meta_parts)
    error_html = ""
    if item.error:
        error_html = f'<p class="error" title="{html.escape(item.error, quote=True)}">{html.escape(item.error)}</p>'
    redirect_html = ""
    if item.redirects:
        codes = " &rarr; ".join(
            f'<span class="code">{hop.status}</span>' for hop in item.redirects
        )
        chain = " | ".join(f"{hop.status} {hop.url} -> {hop.location}" for hop in item.redirects)[:1200]
        redirect_html = (
            f'<p class="redirect redirect-3xx" title="{html.escape(chain, quote=True)}">'
            f"Redirección 3xx {codes} &rarr; {html.escape(item.final_url)}</p>"
        )
    elif item.final_url and item.final_url != item.url:
        redirect_html = (
            f'<p class="redirect" title="{html.escape(item.final_url, quote=True)}">'
            f"Redirige a: {html.escape(item.final_url)}</p>"
        )
    search_text = html.escape(
        f"{item.url} {item.title} {host} {item.status} {' '.join(str(hop.status) for hop in item.redirects)}".lower(),
        quote=True,
    )
    return (
        f"""
        <article class="{card_classes}" data-status="{item.status}" data-search="{search_text}">
          <a class="shot" href="{html.escape(item.file, quote=True)}" data-lightbox="1" aria-label="Ampliar captura de {html.escape(item.url, quote=True)}">
            <img src="{html.escape(item.file, quote=True)}" alt="Captura de {html.escape(item.url, quote=True)}" loading="lazy" decoding="async">
            <span class="badge badge-{item.status}">{status_label}</span>
            <span class="number">#{item.index:03d}</span>
          </a>
          <div class="card-body">
            <div class="host">{html.escape(host)}</div>
            <a class="page-title" href="{html.escape(item.url, quote=True)}" target="_blank" rel="noopener noreferrer" title="{html.escape(title, quote=True)}">{html.escape(title)}</a>
            <a class="url" href="{html.escape(item.url, quote=True)}" target="_blank" rel="noopener noreferrer" title="{html.escape(item.url, quote=True)}">{html.escape(item.url)}</a>
            {redirect_html}
            <p class="meta">{meta}</p>
            {error_html}
            <div class="actions">
              <a href="{html.escape(item.file, quote=True)}" data-lightbox="1">Ampliar captura</a>
              <a href="{html.escape(item.url, quote=True)}" target="_blank" rel="noopener noreferrer">Abrir sitio</a>
            </div>
          </div>
        </article>
        """
    )



def generate_index(
    out_dir: Path,
    captures: list[Capture],
    started_at: datetime,
    elapsed: float,
    live: bool = False,
    running: bool = False,
    cards_html: list[str] | None = None,
) -> Path:
    total = len(captures)
    ok_count = sum(1 for item in captures if item.status == "ok")
    partial_count = sum(1 for item in captures if item.status == "parcial")
    error_count = sum(1 for item in captures if item.status == "error")
    redirect_count = sum(1 for item in captures if item.redirects)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    cards: list[str] = cards_html if cards_html is not None else [render_card(item) for item in captures]
    title_text = "Capturas en vivo" if live else "Capturas de URLs"
    live_badge = '<span class="live-status" id="liveStatus">EN VIVO</span>' if live else ""
    subtitle_text = (
        "Las capturas aparecen aquí en tiempo real, en el mismo orden en que terminan. "
        "La página se actualiza sola mientras la herramienta sigue trabajando; puedes abrir "
        "cualquier captura y hacer zoom sin perder la actualización."
        if live
        else "Galería generada automáticamente. Cada enlace tiene su propia imagen; si un sitio no responde, se crea una imagen de respaldo para no perder ninguna URL. Las redirecciones 3xx se siguen y se muestran con su código y destino final. Haz clic en una captura para verla ampliada y hacer zoom sin salir de esta página."
    )
    live_config = json.dumps({"enabled": live, "running": running}, ensure_ascii=False)
    document = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title_text)}</title>
<link rel="icon" href="data:,">
<style>
:root {{
  color-scheme: dark;
  --bg: #070b13;
  --panel: #0d1422;
  --panel-2: #111b2c;
  --border: #223047;
  --text: #e8eef8;
  --muted: #8fa0b8;
  --accent: #38bdf8;
  --ok: #22c55e;
  --partial: #f59e0b;
  --error: #ef4444;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(56,189,248,.12), transparent 60%),
    radial-gradient(900px 500px at 100% 0%, rgba(168,85,247,.10), transparent 60%),
    var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  min-height: 100vh;
}}
a {{ color: inherit; }}
.hero {{
  max-width: 1600px;
  margin: 0 auto;
  padding: 58px 34px 24px;
}}
.eyebrow {{
  color: var(--accent);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  margin-bottom: 12px;
}}
.live-status {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-left: 10px;
  padding: 5px 9px;
  border: 1px solid rgba(34,197,94,.45);
  border-radius: 999px;
  background: rgba(34,197,94,.12);
  color: #86efac;
  font-size: 10px;
  letter-spacing: .12em;
}}
.live-status::before {{
  content: "";
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 0 rgba(34,197,94,.7);
  animation: live-pulse 1.3s infinite;
}}
.live-status.finished {{
  border-color: var(--border);
  background: var(--panel);
  color: var(--muted);
}}
.live-status.finished::before {{ animation: none; background: var(--muted); }}
@keyframes live-pulse {{
  0% {{ box-shadow: 0 0 0 0 rgba(34,197,94,.65); }}
  70% {{ box-shadow: 0 0 0 7px rgba(34,197,94,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0); }}
}}
h1 {{
  margin: 0;
  font-size: clamp(36px, 5vw, 72px);
  line-height: .98;
  letter-spacing: -.04em;
}}
.subtitle {{
  margin: 18px 0 0;
  color: var(--muted);
  font-size: 16px;
  max-width: 800px;
  line-height: 1.6;
}}
.stats {{
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 28px;
}}
.stat {{
  border: 1px solid var(--border);
  background: rgba(13,20,34,.8);
  border-radius: 14px;
  padding: 14px 18px;
  min-width: 130px;
}}
.stat span {{ display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .1em; }}
.stat strong {{ display: block; margin-top: 4px; font-size: 26px; }}
.stat.ok strong {{ color: var(--ok); }}
.stat.partial strong {{ color: var(--partial); }}
.stat.error strong {{ color: var(--error); }}
.toolbar {{
  max-width: 1600px;
  margin: 0 auto;
  padding: 16px 34px 8px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}}
.search {{
  flex: 1;
  min-width: 240px;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 13px 16px;
  font: inherit;
  outline: none;
}}
.search:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(56,189,248,.12); }}
.filters {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.filter {{
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 999px;
  padding: 10px 14px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}}
.filter.active {{ background: var(--accent); color: #021018; border-color: var(--accent); font-weight: 800; }}
.count {{ color: var(--muted); font-size: 13px; margin-left: auto; }}
.grid {{
  max-width: 1600px;
  margin: 0 auto;
  padding: 18px 34px 80px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 20px;
}}
.card {{
  background: linear-gradient(180deg, rgba(17,27,44,.92), rgba(10,16,27,.96));
  border: 1px solid var(--border);
  border-radius: 18px;
  overflow: hidden;
  box-shadow: 0 18px 45px rgba(0,0,0,.22);
  display: flex;
  flex-direction: column;
  min-width: 0;
}}
.card.status-error {{ border-color: rgba(239,68,68,.45); }}
.card.status-parcial {{ border-color: rgba(245,158,11,.38); }}
.card.hidden {{ display: none; }}
.shot {{
  position: relative;
  display: block;
  background: #060a10;
  border-bottom: 1px solid var(--border);
  aspect-ratio: 16 / 10;
  overflow: hidden;
}}
.shot img {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top center;
  display: block;
  transition: transform .25s ease;
}}
.shot:hover img {{ transform: scale(1.025); }}
.badge {{
  position: absolute;
  top: 12px;
  left: 12px;
  color: #03110b;
  font-size: 11px;
  font-weight: 900;
  letter-spacing: .08em;
  padding: 6px 9px;
  border-radius: 999px;
}}
.badge-ok {{ background: var(--ok); }}
.badge-parcial {{ background: var(--partial); color: #231500; }}
.badge-error {{ background: var(--error); color: #fff; }}
.number {{
  position: absolute;
  top: 12px;
  right: 12px;
  background: rgba(3,7,14,.82);
  border: 1px solid rgba(255,255,255,.14);
  padding: 6px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 800;
  color: #dbeafe;
}}
.card-body {{ padding: 16px 18px 18px; display: flex; flex-direction: column; gap: 8px; flex: 1; }}
.host {{ color: var(--accent); font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .09em; }}
.page-title {{
  font-size: 17px;
  font-weight: 750;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.page-title:hover {{ color: var(--accent); }}
.url {{
  color: var(--muted);
  font-size: 12px;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.url:hover {{ text-decoration: underline; }}
.redirect, .error {{
  margin: 0;
  font-size: 12px;
  line-height: 1.45;
  color: #cbd5e1;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.redirect-3xx {{ color: #fcd34d; }}
.code {{
  display: inline-block;
  background: rgba(245,158,11,.16);
  border: 1px solid rgba(245,158,11,.45);
  color: #fcd34d;
  border-radius: 999px;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 800;
  line-height: 1.4;
}}
.error {{ color: #fca5a5; }}
.meta {{ margin: 0; color: var(--muted); font-size: 12px; }}
.actions {{ display: flex; gap: 10px; margin-top: auto; padding-top: 8px; }}
.actions a {{
  flex: 1;
  text-align: center;
  text-decoration: none;
  font-size: 12px;
  font-weight: 750;
  padding: 9px 10px;
  border-radius: 9px;
  background: var(--panel-2);
  border: 1px solid var(--border);
}}
.actions a:first-child {{ background: var(--accent); border-color: var(--accent); color: #021018; }}
.actions a:hover {{ filter: brightness(1.1); }}
.empty {{ display: none; max-width: 1600px; margin: 30px auto; padding: 0 34px; color: var(--muted); }}
.empty.visible {{ display: block; }}
.shot {{ cursor: zoom-in; }}
.lightbox {{
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: none;
  flex-direction: column;
  background: rgba(2, 6, 12, .97);
  backdrop-filter: blur(10px);
  overscroll-behavior: contain;
}}
.lightbox.open {{ display: flex; }}
body.lightbox-open {{ overflow: hidden; }}
.lb-bar {{
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border);
  background: rgba(7, 11, 19, .94);
}}
.lb-info {{ min-width: 0; flex: 1; }}
.lb-title {{
  font-size: 15px;
  font-weight: 800;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.lb-url {{
  margin-top: 3px;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.lb-tools {{ display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }}
.lb-count {{ color: var(--muted); font-size: 12px; padding: 0 5px; }}
.lb-zoom {{ min-width: 52px; text-align: center; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
.lb-tools button {{
  appearance: none;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 8px 11px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}}
.lb-tools button:hover {{ border-color: var(--accent); color: var(--accent); }}
.lb-tools button:disabled {{ opacity: .35; cursor: default; border-color: var(--border); color: var(--text); }}
.lb-tools .lb-close {{ background: rgba(239, 68, 68, .16); border-color: rgba(239, 68, 68, .5); color: #fecaca; }}
.lb-tools .lb-close:hover {{ background: var(--error); border-color: var(--error); color: #fff; }}
.lb-stage {{
  flex: 1;
  overflow: auto;
  padding: 18px 72px 28px;
  text-align: center;
  -webkit-overflow-scrolling: touch;
}}
.lb-stage img {{
  display: inline-block;
  max-width: none;
  height: auto;
  margin: 0 auto;
  background: #fff;
  box-shadow: 0 22px 70px rgba(0, 0, 0, .55);
  user-select: none;
  -webkit-user-drag: none;
}}
.lb-nav {{
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  z-index: 2;
  width: 44px;
  height: 68px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: rgba(13, 20, 34, .86);
  color: var(--text);
  font-size: 25px;
  cursor: pointer;
}}
.lb-nav:hover {{ border-color: var(--accent); color: var(--accent); }}
.lb-nav:disabled {{ opacity: .25; cursor: default; }}
.lb-prev {{ left: 12px; }}
.lb-next {{ right: 12px; }}
footer {{ max-width: 1600px; margin: 0 auto; padding: 0 34px 38px; color: #64748b; font-size: 12px; }}
@media (max-width: 760px) {{
  .lb-bar {{ align-items: flex-start; padding: 10px 12px; }}
  .lb-tools {{ gap: 5px; }}
  .lb-tools button {{ padding: 7px 9px; font-size: 12px; }}
  .lb-stage {{ padding: 12px; }}
  .lb-nav {{ display: none; }}
}}
@media (max-width: 640px) {{
  .hero, .toolbar, .grid, footer {{ padding-left: 16px; padding-right: 16px; }}
  .hero {{ padding-top: 36px; }}
  .count {{ margin-left: 0; width: 100%; }}
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<header class="hero">
  <div class="eyebrow">Informe de capturas &middot; por h4cksec{live_badge}</div>
  <h1>{html.escape(title_text)}</h1>
  <p class="subtitle">{html.escape(subtitle_text)}</p>
  <div class="stats">
    <div class="stat"><span>Total</span><strong id="statTotal">{total}</strong></div>
    <div class="stat ok"><span>OK</span><strong id="statOk">{ok_count}</strong></div>
    <div class="stat partial"><span>Parciales</span><strong id="statPartial">{partial_count}</strong></div>
    <div class="stat error"><span>Errores</span><strong id="statError">{error_count}</strong></div>
    <div class="stat"><span>Redirecciones</span><strong id="statRedirect">{redirect_count}</strong></div>
    <div class="stat"><span>Duración</span><strong id="statElapsed">{elapsed:.1f}s</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <input class="search" id="search" type="search" placeholder="Buscar por URL, dominio o título..." autocomplete="off">
    <div class="filters" id="filters">
      <button class="filter active" data-filter="all">Todas</button>
      <button class="filter" data-filter="ok">OK</button>
      <button class="filter" data-filter="parcial">Parciales</button>
      <button class="filter" data-filter="error">Errores</button>
    </div>
    <div class="count" id="count">{total} capturas</div>
  </div>
  <section class="grid" id="grid">
{''.join(cards)}
  </section>
  <p class="empty" id="empty">Esperando capturas...</p>
</main>
<footer>Autor: h4cksec &middot; Generado el {html.escape(generated)} &middot; Inicio: {html.escape(started_at.strftime('%Y-%m-%d %H:%M:%S'))} &middot; Todas las URLs tienen imagen.</footer>
<div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Visor de capturas" aria-hidden="true">
  <div class="lb-bar">
    <div class="lb-info">
      <div class="lb-title" id="lbTitle"></div>
      <div class="lb-url" id="lbUrl"></div>
    </div>
    <div class="lb-tools">
      <span class="lb-count" id="lbCount"></span>
      <button type="button" id="lbPrevTop" title="Captura anterior (flecha izquierda)" aria-label="Captura anterior">&larr;</button>
      <button type="button" id="lbNextTop" title="Captura siguiente (flecha derecha)" aria-label="Captura siguiente">&rarr;</button>
      <button type="button" id="lbZoomOut" title="Alejar (tecla -)" aria-label="Alejar">&minus;</button>
      <span class="lb-zoom" id="lbZoom">100%</span>
      <button type="button" id="lbZoomIn" title="Acercar (tecla +)" aria-label="Acercar">+</button>
      <button type="button" id="lbFit" title="Ajustar la captura al ancho (tecla 0)">Ajustar</button>
      <button type="button" id="lbActual" title="Ver al tamaño real (tecla 1)">100%</button>
      <button type="button" class="lb-close" id="lbClose" title="Cerrar (Esc)" aria-label="Cerrar">Cerrar</button>
    </div>
  </div>
  <button type="button" class="lb-nav lb-prev" id="lbPrev" title="Captura anterior (flecha izquierda)" aria-label="Captura anterior">&larr;</button>
  <button type="button" class="lb-nav lb-next" id="lbNext" title="Captura siguiente (flecha derecha)" aria-label="Captura siguiente">&rarr;</button>
  <div class="lb-stage" id="lbStage">
    <img id="lbImage" alt="" draggable="false">
  </div>
</div>
<script>
let cards = Array.from(document.querySelectorAll('.card'));
const liveConfig = {live_config};
const liveStatus = document.getElementById('liveStatus');
const statTotal = document.getElementById('statTotal');
const statOk = document.getElementById('statOk');
const statPartial = document.getElementById('statPartial');
const statError = document.getElementById('statError');
const statRedirect = document.getElementById('statRedirect');
const statElapsed = document.getElementById('statElapsed');
const grid = document.getElementById('grid');
let renderedCards = cards.length;
let liveTimer = null;
const search = document.getElementById('search');
const count = document.getElementById('count');
const empty = document.getElementById('empty');
const filters = Array.from(document.querySelectorAll('.filter'));
const lightbox = document.getElementById('lightbox');
const lbImage = document.getElementById('lbImage');
const lbStage = document.getElementById('lbStage');
const lbTitle = document.getElementById('lbTitle');
const lbUrl = document.getElementById('lbUrl');
const lbCount = document.getElementById('lbCount');
const lbZoom = document.getElementById('lbZoom');
const lbPrev = document.getElementById('lbPrev');
const lbNext = document.getElementById('lbNext');
const lbPrevTop = document.getElementById('lbPrevTop');
const lbNextTop = document.getElementById('lbNextTop');
const lbZoomOut = document.getElementById('lbZoomOut');
const lbZoomIn = document.getElementById('lbZoomIn');
const lbFit = document.getElementById('lbFit');
const lbActual = document.getElementById('lbActual');
const lbClose = document.getElementById('lbClose');
let activeFilter = 'all';
let currentCard = null;
let currentScale = 1;
let naturalWidth = 0;
let loadToken = 0;
let lastFocus = null;

function applyFilters() {{
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  cards.forEach((card) => {{
    const matchesText = !query || (card.dataset.search || '').includes(query);
    const matchesStatus = activeFilter === 'all' || card.dataset.status === activeFilter;
    const show = matchesText && matchesStatus;
    card.classList.toggle('hidden', !show);
    if (show) visible += 1;
  }});
  count.textContent = visible + ' de ' + cards.length + ' capturas';
  empty.textContent = (!cards.length && liveConfig.enabled && liveConfig.running)
    ? 'Esperando capturas...'
    : 'No hay capturas que coincidan con el filtro.';
  empty.classList.toggle('visible', visible === 0);
  updateNav();
}}
search.addEventListener('input', applyFilters);
filters.forEach((button) => {{
  button.addEventListener('click', () => {{
    activeFilter = button.dataset.filter;
    filters.forEach((item) => item.classList.toggle('active', item === button));
    applyFilters();
  }});
}});

function visibleCards() {{
  return cards.filter((card) => !card.classList.contains('hidden'));
}}

function clamp(value, minimum, maximum) {{
  return Math.min(maximum, Math.max(minimum, value));
}}

function updateNav() {{
  const available = visibleCards();
  const index = currentCard ? available.indexOf(currentCard) : -1;
  lbCount.textContent = index >= 0 ? (index + 1) + ' / ' + available.length : '';
  const disabled = available.length < 2;
  [lbPrev, lbNext, lbPrevTop, lbNextTop].forEach((button) => {{
    button.disabled = disabled;
  }});
}}

function setScale(nextScale, preserveCenter) {{
  if (!naturalWidth) return;
  const oldWidth = lbImage.offsetWidth || naturalWidth * currentScale;
  const oldHeight = lbImage.offsetHeight || 1;
  const centerX = (lbStage.scrollLeft + lbStage.clientWidth / 2) / Math.max(1, oldWidth);
  const centerY = (lbStage.scrollTop + lbStage.clientHeight / 2) / Math.max(1, oldHeight);
  currentScale = clamp(nextScale, 0.05, 8);
  lbImage.style.width = Math.max(1, Math.round(naturalWidth * currentScale)) + 'px';
  lbZoom.textContent = Math.round(currentScale * 100) + '%';
  if (preserveCenter) {{
    requestAnimationFrame(() => {{
      lbStage.scrollLeft = Math.max(0, centerX * lbImage.offsetWidth - lbStage.clientWidth / 2);
      lbStage.scrollTop = Math.max(0, centerY * lbImage.offsetHeight - lbStage.clientHeight / 2);
    }});
  }}
}}

function fitImage() {{
  if (!naturalWidth) return;
  const style = getComputedStyle(lbStage);
  const horizontalPadding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const available = Math.max(120, lbStage.clientWidth - horizontalPadding - 4);
  setScale(Math.min(1, available / naturalWidth), false);
  lbStage.scrollLeft = 0;
  lbStage.scrollTop = 0;
}}

function zoomBy(factor) {{
  if (!naturalWidth) return;
  setScale(currentScale * factor, true);
}}

function openFromLink(link) {{
  const card = link.closest('.card');
  if (card) openLightbox(card);
}}

function openLightbox(card) {{
  const shot = card.querySelector('.shot');
  const preview = shot ? shot.querySelector('img') : null;
  if (!preview) return;
  const wasOpen = lightbox.classList.contains('open');
  currentCard = card;
  if (!wasOpen) lastFocus = document.activeElement;
  const title = card.querySelector('.page-title');
  const url = card.querySelector('.url');
  lbTitle.textContent = title ? title.textContent.trim() : (preview.alt || '');
  lbUrl.textContent = url ? url.textContent.trim() : '';
  lbImage.alt = preview.alt || lbTitle.textContent;
  lbImage.style.width = 'auto';
  lbZoom.textContent = '100%';
  naturalWidth = 0;
  currentScale = 1;
  lightbox.classList.add('open');
  lightbox.setAttribute('aria-hidden', 'false');
  document.body.classList.add('lightbox-open');
  updateNav();
  const token = ++loadToken;
  const ready = () => {{
    if (token !== loadToken) return;
    naturalWidth = lbImage.naturalWidth || preview.naturalWidth || 0;
    if (naturalWidth) fitImage();
  }};
  lbImage.onload = ready;
  lbImage.src = preview.currentSrc || preview.src;
  if (lbImage.complete && lbImage.naturalWidth) ready();
  lbClose.focus();
}}

function closeLightbox() {{
  if (!lightbox.classList.contains('open')) return;
  loadToken += 1;
  lightbox.classList.remove('open');
  lightbox.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('lightbox-open');
  lbImage.onload = null;
  lbImage.removeAttribute('src');
  lbImage.style.width = 'auto';
  naturalWidth = 0;
  currentCard = null;
  if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
}}

function navigate(step) {{
  const available = visibleCards();
  if (!available.length) return;
  let index = currentCard ? available.indexOf(currentCard) : -1;
  if (index < 0) index = 0;
  index = (index + step + available.length) % available.length;
  openLightbox(available[index]);
}}

document.addEventListener('click', (event) => {{
  const target = event.target instanceof Element ? event.target : null;
  const link = target ? target.closest('[data-lightbox]') : null;
  if (!link) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openFromLink(link);
}});
[lbPrev, lbPrevTop].forEach((button) => button.addEventListener('click', () => navigate(-1)));
[lbNext, lbNextTop].forEach((button) => button.addEventListener('click', () => navigate(1)));
lbZoomIn.addEventListener('click', () => zoomBy(1.25));
lbZoomOut.addEventListener('click', () => zoomBy(1 / 1.25));
lbFit.addEventListener('click', fitImage);
lbActual.addEventListener('click', () => setScale(1, true));
lbClose.addEventListener('click', closeLightbox);
lbStage.addEventListener('click', (event) => {{
  if (event.target === lbStage) closeLightbox();
}});
lbStage.addEventListener('wheel', (event) => {{
  if (!event.ctrlKey) return;
  event.preventDefault();
  zoomBy(event.deltaY < 0 ? 1.15 : 1 / 1.15);
}}, {{ passive: false }});
document.addEventListener('keydown', (event) => {{
  if (!lightbox.classList.contains('open')) return;
  if (event.key === 'Escape') {{
    event.preventDefault();
    closeLightbox();
  }} else if (event.key === 'ArrowLeft') {{
    event.preventDefault();
    navigate(-1);
  }} else if (event.key === 'ArrowRight') {{
    event.preventDefault();
    navigate(1);
  }} else if (event.key === '+' || event.key === '=') {{
    event.preventDefault();
    zoomBy(1.25);
  }} else if (event.key === '-' || event.key === '_') {{
    event.preventDefault();
    zoomBy(1 / 1.25);
  }} else if (event.key === '0') {{
    event.preventDefault();
    fitImage();
  }} else if (event.key === '1') {{
    event.preventDefault();
    setScale(1, true);
  }}
}});
function setLiveState(running) {{
  liveConfig.running = running;
  if (!liveStatus) return;
  liveStatus.textContent = running ? 'EN VIVO' : 'FINALIZADO';
  liveStatus.classList.toggle('finished', !running);
}}

function updateStats(stats) {{
  if (!stats) return;
  if (statTotal) statTotal.textContent = stats.total;
  if (statOk) statOk.textContent = stats.ok;
  if (statPartial) statPartial.textContent = stats.partial;
  if (statError) statError.textContent = stats.error;
  if (statRedirect) statRedirect.textContent = stats.redirect;
  if (statElapsed) statElapsed.textContent = Number(stats.elapsed || 0).toFixed(1) + 's';
}}

function appendCards(htmlList) {{
  if (!Array.isArray(htmlList) || !htmlList.length) return;
  const nearBottom = (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 300);
  const template = document.createElement('template');
  let added = 0;
  htmlList.forEach((cardHtml) => {{
    template.innerHTML = cardHtml;
    const card = template.content.firstElementChild;
    if (!card) return;
    grid.appendChild(card);
    cards.push(card);
    added += 1;
  }});
  applyFilters();
  if (added && nearBottom && !lightbox.classList.contains('open')) {{
    requestAnimationFrame(() => window.scrollTo({{ top: document.documentElement.scrollHeight, behavior: 'smooth' }}));
  }}
}}

async function pollLive() {{
  if (!liveConfig.enabled) return;
  window.clearTimeout(liveTimer);
  try {{
    const response = await fetch('live.json?t=' + Date.now(), {{ cache: 'no-store' }});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    let missing = false;
    if (Array.isArray(data.cards) && data.cards.length > renderedCards) {{
      const newHtml = [];
      let next = renderedCards;
      while (next < data.cards.length) {{
        try {{
          const cardResponse = await fetch(data.cards[next], {{ cache: 'no-store' }});
          if (!cardResponse.ok) throw new Error('HTTP ' + cardResponse.status);
          newHtml.push(await cardResponse.text());
          next += 1;
        }} catch (error) {{
          break;
        }}
      }}
      if (newHtml.length) appendCards(newHtml);
      renderedCards = next;
      missing = renderedCards < data.cards.length;
    }}
    updateStats(data.stats);
    setLiveState(Boolean(data.running));
    if (data.running || missing) liveTimer = window.setTimeout(pollLive, 2000);
  }} catch (error) {{
    if (liveStatus) {{
      liveStatus.textContent = 'RECONECTANDO...';
      liveStatus.classList.remove('finished');
    }}
    liveTimer = window.setTimeout(pollLive, 2500);
  }}
}}

let cardScriptBuffer = [];
window.__captureLiveCard = function(cardHtml) {{
  cardScriptBuffer.push(cardHtml);
}};

function loadScript(src) {{
  return new Promise((resolve, reject) => {{
    const script = document.createElement('script');
    script.src = src + (src.includes('?') ? '&' : '?') + 't=' + Date.now();
    script.async = true;
    script.onload = () => {{ script.remove(); resolve(); }};
    script.onerror = () => {{ script.remove(); reject(new Error('No se pudo cargar ' + src)); }};
    document.head.appendChild(script);
  }});
}}

async function handleLiveScript(data) {{
  const cardList = Array.isArray(data.cards) ? data.cards : [];
  updateStats(data.stats);
  setLiveState(Boolean(data.running));
  cardScriptBuffer = [];
  let next = renderedCards;
  let failed = false;
  while (next < cardList.length) {{
    try {{
      await loadScript(cardList[next]);
      next += 1;
    }} catch (error) {{
      failed = true;
      break;
    }}
  }}
  if (cardScriptBuffer.length) appendCards(cardScriptBuffer);
  renderedCards = next;
  const missing = renderedCards < cardList.length;
  if (data.running || missing || failed) {{
    liveTimer = window.setTimeout(pollLiveScript, failed ? 2500 : 2000);
  }}
}}

window.__captureLiveUpdate = function(data) {{
  if (!liveConfig.enabled) return;
  handleLiveScript(data).catch(() => {{
    liveTimer = window.setTimeout(pollLiveScript, 2500);
  }});
}};

function pollLiveScript() {{
  if (!liveConfig.enabled) return;
  window.clearTimeout(liveTimer);
  loadScript('live.js').catch(() => {{
    if (liveStatus) {{
      liveStatus.textContent = 'RECONECTANDO...';
      liveStatus.classList.remove('finished');
    }}
    liveTimer = window.setTimeout(pollLiveScript, 2500);
  }});
}}

function startLiveUpdates() {{
  if (!liveConfig.enabled) return;
  if (location.protocol === 'file:') {{
    pollLiveScript();
    return;
  }}
  pollLive();
}}

applyFilters();
startLiveUpdates();
</script>
</body>
</html>
"""
    index_path = out_dir / "index.html"
    atomic_write_text(index_path, document)
    return index_path


class LiveRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


class LiveServer:
    def __init__(self, directory: Path) -> None:
        handler = partial(LiveRequestHandler, directory=str(directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.started = False

    def start(self) -> None:
        self.thread.start()
        self.started = True

    def close(self) -> None:
        if self.started:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)


class LiveWriter:
    def __init__(
        self,
        out_dir: Path,
        started_at: datetime,
        start_clock: float,
    ) -> None:
        self.out_dir = out_dir
        self.started_at = started_at
        self.start_clock = start_clock
        self.lock = threading.Lock()
        self.captures: list[Capture] = []
        self.cards: list[str] = []
        self.card_files: list[str] = []
        self.card_scripts: list[str] = []
        self.card_dir = out_dir / ".live"
        self.server: LiveServer | None = None
        self.last_index_write = 0.0

    @property
    def url(self) -> str:
        return self.server.url if self.server is not None else ""

    def elapsed(self) -> float:
        return time.monotonic() - self.start_clock

    def stats(self) -> dict[str, object]:
        return {
            "total": len(self.captures),
            "ok": sum(1 for item in self.captures if item.status == "ok"),
            "partial": sum(1 for item in self.captures if item.status == "parcial"),
            "error": sum(1 for item in self.captures if item.status == "error"),
            "redirect": sum(1 for item in self.captures if item.redirects),
            "elapsed": round(self.elapsed(), 3),
            "running": True,
        }

    def _write_live(self, running: bool) -> None:
        base = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "running": running,
            "stats": {**self.stats(), "running": running},
        }
        json_payload = {**base, "cards": self.card_files}
        js_payload = {**base, "cards": self.card_scripts}
        atomic_write_text(
            self.out_dir / "live.json",
            json.dumps(json_payload, ensure_ascii=False, separators=(",", ":")),
        )
        atomic_write_text(
            self.out_dir / "live.js",
            "window.__captureLiveUpdate && window.__captureLiveUpdate("
            + json.dumps(js_payload, ensure_ascii=False, separators=(",", ":"))
            + ");\n",
        )

    def _write_index(self, running: bool) -> None:
        generate_index(
            self.out_dir,
            self.captures,
            self.started_at,
            self.elapsed(),
            live=True,
            running=running,
            cards_html=self.cards,
        )
        self.last_index_write = time.monotonic()

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.card_dir.is_dir():
            shutil.rmtree(self.card_dir)
        elif self.card_dir.exists():
            self.card_dir.unlink()
        self.card_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._write_live(running=True)
            self._write_index(running=True)
        try:
            self.server = LiveServer(self.out_dir)
            self.server.start()
        except Exception as exc:
            if self.server is not None:
                self.server.close()
                self.server = None
            console_print(
                f"Advertencia: no se pudo iniciar la vista en vivo: {clean_error(exc)}",
                to_stderr=True,
            )

    def add(self, capture: Capture) -> None:
        card = render_card(capture)
        card_name = f".live/{capture.index:06d}.html"
        script_name = f".live/{capture.index:06d}.js"
        atomic_write_text(self.out_dir / card_name, card)
        atomic_write_text(
            self.out_dir / script_name,
            "window.__captureLiveCard && window.__captureLiveCard("
            + json.dumps(card, ensure_ascii=False)
            + ");\n",
        )
        with self.lock:
            self.captures.append(capture)
            self.cards.append(card)
            self.card_files.append(card_name)
            self.card_scripts.append(script_name)
            self._write_live(running=True)
            now = time.monotonic()
            if now - self.last_index_write >= 1.0:
                self._write_index(running=True)

    def snapshot(self) -> list[Capture]:
        with self.lock:
            return list(self.captures)

    def publish_final(self) -> None:
        with self.lock:
            self._write_live(running=False)
            self._write_index(running=False)

    def count(self) -> int:
        with self.lock:
            return len(self.captures)

    def close(self, keep_alive: float = 1.5) -> None:
        if self.server is None:
            return
        if keep_alive > 0 and self.count() > 0:
            time.sleep(keep_alive)
        self.server.close()
        self.server = None


class StreamingCaptures:
    def __init__(
        self,
        out_dir: Path,
        args: argparse.Namespace,
        pool: DriverPool,
        writer: LiveWriter,
    ) -> None:
        self.out_dir = out_dir
        self.args = args
        self.pool = pool
        self.writer = writer
        self.executor = ThreadPoolExecutor(max_workers=args.workers)
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.futures: list[object] = []
        self.next_index = 0

    def submit(self, url: str) -> bool:
        normalized = normalize_url(url)
        if not normalized:
            return False
        with self.lock:
            if normalized in self.seen:
                return False
            self.seen.add(normalized)
            self.next_index += 1
            index = self.next_index
        future = self.executor.submit(self._capture, index, normalized)
        with self.lock:
            self.futures.append(future)
        return True

    def _capture(self, index: int, url: str) -> Capture:
        try:
            capture = capture_one(index, url, self.out_dir, self.args, self.pool)
        except Exception as exc:
            filename = safe_filename(index, url)
            path = self.out_dir / filename
            error = clean_error(exc)
            width, height = make_placeholder(path, url, error, self.args, index)
            capture = Capture(
                index=index,
                url=url,
                final_url=url,
                title="",
                file=filename,
                status="error",
                attempts=0,
                duration=0.0,
                width=width,
                height=height,
                truncated=False,
                placeholder=True,
                method="placeholder",
                error=error,
            )
        verify_images(self.out_dir, [capture], self.args)
        self.writer.add(capture)
        label = STATUS_LABEL.get(capture.status, capture.status.upper())
        redirect_note = ""
        if capture.redirects:
            codes = " -> ".join(str(hop.status) for hop in capture.redirects)
            redirect_note = f"  [3xx: {codes} -> {capture.final_url}]"
        console_print(
            f"[{capture.index:03d}] {label:<8} {capture.duration:6.1f}s  "
            f"{capture.url}{redirect_note}"
        )
        return capture

    def submitted(self) -> int:
        with self.lock:
            return self.next_index

    def wait_for_all(self) -> None:
        while True:
            with self.lock:
                pending = [future for future in self.futures if not future.done()]
            if not pending:
                break
            wait(pending, timeout=0.5)
        self.executor.shutdown(wait=True)


def write_reports(
    out_dir: Path,
    captures: list[Capture],
    started_at: datetime,
    elapsed: float,
    discoveries: list[Discovery] | None = None,
) -> None:
    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 3),
        "total": len(captures),
        "ok": sum(1 for item in captures if item.status == "ok"),
        "parcial": sum(1 for item in captures if item.status == "parcial"),
        "error": sum(1 for item in captures if item.status == "error"),
        "redirected": sum(1 for item in captures if item.redirects),
        "all_urls_have_image": all((out_dir / item.file).exists() for item in captures),
        "captures": [asdict(item) for item in captures],
    }
    if discoveries:
        manifest["discovery"] = [asdict(item) for item in discoveries]
    atomic_write_text(
        out_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )

    fields = [
        "index",
        "url",
        "final_url",
        "title",
        "file",
        "status",
        "attempts",
        "duration",
        "width",
        "height",
        "truncated",
        "placeholder",
        "method",
        "error",
        "redirects",
    ]
    with (out_dir / "report.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in captures:
            row = asdict(item)
            row["redirects"] = json.dumps(row["redirects"], ensure_ascii=False)
            writer.writerow(row)


def verify_images(out_dir: Path, captures: list[Capture], args: argparse.Namespace) -> None:
    for item in captures:
        path = out_dir / item.file
        if path.exists() and path.stat().st_size > 0:
            continue
        width, height = make_placeholder(
            path,
            item.url,
            f"La imagen no se generó correctamente. {item.error}".strip(),
            args,
            item.index,
        )
        item.status = "error"
        item.placeholder = True
        item.method = "placeholder"
        item.width = width
        item.height = height
        item.truncated = False
        if not item.error:
            item.error = "La imagen no se generó correctamente."


def preflight(args: argparse.Namespace) -> None:
    if shutil.which("chromium") is None and shutil.which("chromium-browser") is None:
        raise RuntimeError("No se encontró Chromium en el sistema.")
    if shutil.which("chromedriver") is None:
        raise RuntimeError("No se encontró chromedriver en el sistema.")


def main() -> int:
    if len(sys.argv) == 1 and sys.stdin.isatty():
        sys.argv.append("-h")
    args = parse_args()
    wordlist: Path | None = None
    if args.discover:
        if shutil.which("gobuster") is None:
            print("Error: no se encontró gobuster en el sistema.", file=sys.stderr)
            return 2
        try:
            wordlist = resolve_wordlist(args)
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    try:
        preflight(args)
    except Exception as exc:
        print(f"Error: no se pudo iniciar Chromium/chromedriver: {clean_error(exc)}", file=sys.stderr)
        return 2

    try:
        input_urls, source = load_urls(args, allow_default=not bool(args.discover))
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not input_urls and not args.discover:
        print("Error: no se encontró ninguna URL válida en la entrada.", file=sys.stderr)
        return 2

    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().astimezone()
    start_clock = time.monotonic()
    writer = LiveWriter(out_dir, started_at, start_clock)
    writer.start()

    source_parts: list[str] = []
    if args.discover:
        target_word = "objetivo" if len(args.discover) == 1 else "objetivos"
        source_parts.append(f"gobuster ({len(args.discover)} {target_word})")
    if source and source != "sin entrada":
        source_parts.append(source)
    source_label = ", ".join(source_parts) or "argumentos"

    tool_name = Path(sys.argv[0]).name
    if tool_name.endswith(".py"):
        tool_name = tool_name[:-3]
    console_print(tool_name)
    console_print("Autor: h4cksec")
    console_print(f"Entrada: {source_label}")
    console_print(f"Salida: {out_dir}")
    if writer.url:
        console_print(f"Vista en vivo: {writer.url}")
        console_print("El index se actualiza solo: abre esa dirección mientras se ejecuta.")
    else:
        console_print("Vista en vivo no disponible; abre index.html (se recargará solo).")
    browser_word = "navegador" if args.workers == 1 else "navegadores"
    attempt_word = "intento" if args.retries == 1 else "intentos"
    console_print(
        f"Capturando con {args.workers} {browser_word}, "
        f"{args.retries} {attempt_word} por URL..."
    )
    if input_urls:
        console_print(f"URLs iniciales: {len(input_urls)}")
    console_print("")

    pool = DriverPool(args)
    runner = StreamingCaptures(out_dir, args, pool, writer)
    discoveries: list[Discovery] = []
    discovery_error = ""
    try:
        for url in input_urls:
            runner.submit(url)
        if args.discover:
            if wordlist is None:
                discovery_error = "No se indicó una lista de palabras para gobuster."
            else:
                try:
                    _, discoveries = discover_urls(args, wordlist, on_route=runner.submit)
                except RuntimeError as exc:
                    discovery_error = clean_error(exc)
        runner.wait_for_all()
    finally:
        pool.close_all()

    if discovery_error:
        console_print(f"Error: {discovery_error}", to_stderr=True)

    captures = writer.snapshot()
    if not captures:
        console_print("Error: no se encontró ninguna URL válida en la entrada.", to_stderr=True)
        writer.close(keep_alive=0)
        return 2

    captures.sort(key=lambda item: item.index)
    verify_images(out_dir, captures, args)
    elapsed = time.monotonic() - start_clock

    writer.publish_final()
    index_path = generate_index(out_dir, captures, started_at, elapsed)
    write_reports(out_dir, captures, started_at, elapsed, discoveries)

    ok_count = sum(1 for item in captures if item.status == "ok")
    partial_count = sum(1 for item in captures if item.status == "parcial")
    error_count = sum(1 for item in captures if item.status == "error")
    redirect_count = sum(1 for item in captures if item.redirects)
    console_print("")
    console_print(
        f"Total: {len(captures)} | OK: {ok_count} | Parciales: {partial_count} | "
        f"Errores: {error_count} | Redirecciones 3xx: {redirect_count}"
    )
    console_print(f"Índice: {index_path}")
    console_print(f"Imágenes: {out_dir}")
    if error_count:
        console_print(
            "Las URLs fallidas tienen una imagen de respaldo para garantizar una captura por enlace."
        )
    writer.close(keep_alive=1.5)

    if discovery_error:
        return 2
    if args.strict and error_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
