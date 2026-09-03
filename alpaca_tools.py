"""Capa de herramientas OFICIAL de Alpaca (MCP Server / CLI).

Por que existe
--------------
Las reglas del hackathon exigen usar el MCP Server de Alpaca o el CLI de
Alpaca. Hasta aqui la app solo hablaba con Alpaca por el SDK REST
(`alpaca_service.py`), asi que la entrada era, en el mejor de los casos,
cuestionable. Este modulo es esa capa de herramientas.

Se usa el SDK oficial `mcp` (v2.0.0, ya presente en Anaconda) con transporte
stdio. No se usa `fastmcp`: no esta en el runtime de la app y meterlo
arrastraria un arbol de dependencias enorme tres dias antes del cierre.
Cero dependencias nuevas.

Reglas de seguridad de este archivo
-----------------------------------
1. **Credenciales solo por el entorno del subproceso hijo**, nunca por argv
   (argv es visible en la lista de procesos).
2. **Ninguna credencial aparece en un log, un `repr` o un mensaje de
   excepcion.** Los secretos viven en una clausura, no en atributos
   inspeccionables, y todo mensaje de error pasa por `_scrub`.
3. **Toolsets de solo lectura** por defecto: `place_*`, `cancel_*`,
   `close_*`, `replace_*` y `update_account_config` ni siquiera se registran
   en el servidor. Las ordenes siguen saliendo por la via autorizada de
   `submit_authorized_option_order` (Task 3).
4. **Fail-closed**: `HUARIZO_ALPACA_TOOL` vale `none` por defecto. Sin esa
   variable la capa esta apagada y no cambia nada. Si esta activa y no puede
   entregar un snapshot utilizable, lanza `ToolUnavailable` y el orquestador
   se pausa.
5. **Una cuenta bloqueada no se ignora.** Si `trading_blocked`,
   `account_blocked`, `trade_suspended_by_user` es verdadero o `status` no es
   `ACTIVE`, el snapshot se marca `usable=False` y el facade lo rechaza.
   Reventar la cuenta del concurso es peor que no operar.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Herramienta correcta segun el spike. NO es `get_account`.
TOOL_GET_ACCOUNT = "get_account_info"

# Toolsets de SOLO LECTURA. Omitir `trading` es la barrera mas fuerte: el
# servidor ni registra las herramientas que mueven dinero.
READ_ONLY_TOOLSETS = "account,assets,stock-data,options-data,news"

VALID_METHODS = ("mcp", "cli", "none")

# Nombres del SDK -> nombres que esperan el MCP server y el CLI.
_ENV_MAP: Tuple[Tuple[str, str], ...] = (
    ("APCA_API_KEY_ID", "ALPACA_API_KEY"),
    ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"),
    ("APCA_PAPER", "ALPACA_PAPER_TRADE"),
)

# Campos de dinero obligatorios. Si falta alguno no se puede medir el riesgo.
_REQUIRED_MONEY = ("equity", "cash", "buying_power")
# Campos de dinero opcionales: si no vienen, no se inventan.
_OPTIONAL_MONEY = ("last_equity", "options_buying_power",
                   "long_market_value", "portfolio_value")
# Banderas de bloqueo reales del payload de Alpaca.
_BLOCK_FLAGS = ("trading_blocked", "account_blocked", "trade_suspended_by_user")


class ToolUnavailable(RuntimeError):
    """La capa de herramientas no pudo entregar un snapshot utilizable.

    El orquestador debe tratarla como una senal de parada, no como un aviso.
    """


def _finite(value: Any) -> Optional[float]:
    """Convierte el string de dinero de Alpaca a float. None si no sirve."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _scrub(text: Any, secrets: Iterable[str]) -> str:
    """Sustituye cualquier secreto conocido antes de que salga del modulo."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***REDACTED***")
    return out


def _child_env(api_key: str, secret_key: str, paper: bool,
               toolsets: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Entorno para el subproceso hijo, con las llaves renombradas.

    El hijo lee `ALPACA_*`; el proyecto guarda `APCA_*` en `.env`. Se mapean
    aqui, en la frontera, sin renombrar nada en `.env` (alpaca_service.py y
    sus tests dependen de los nombres originales).
    """
    env = dict(os.environ if base is None else base)
    env["ALPACA_API_KEY"] = api_key
    env["ALPACA_SECRET_KEY"] = secret_key
    env["ALPACA_PAPER_TRADE"] = "true" if paper else "false"
    env["ALPACA_TOOLSETS"] = toolsets
    env.setdefault("ALPACA_MCP_USER_AGENT", "huarizo-ai/1.0")
    return env


def _parse_mcp_result(result: Any) -> Optional[Dict[str, Any]]:
    """Saca el payload de un `CallToolResult`.

    Devuelve None si no se puede leer (no lanza): un payload ilegible NO es
    transitorio, reintentarlo solo quemaria tiempo.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    if getattr(result, "isError", False):
        return None
    chunks: List[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text)
    if not chunks:
        return None
    try:
        payload = json.loads("\n".join(chunks))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class AlpacaTools:
    """Fachada sincrona sobre el MCP server (o el CLI) de Alpaca.

    Las vistas de Flask son sincronas y MCP es asincrono, asi que el puente es
    **un hilo propio con un unico event loop**, arrancado de forma perezosa y
    con candado. Las llamadas se envian con `run_coroutine_threadsafe`. Nunca
    se llama a `asyncio.run()` desde varios hilos a la vez: eso crearia un loop
    por peticion.

    La sesion MCP se reutiliza: arrancar `alpaca-mcp-server` cuesta 2-4 s
    (banner + registro de 72 herramientas + handshake), asi que abrirla por
    llamada dentro de un ciclo de 60 s es inviable.
    """

    def __init__(self, method: Optional[str] = None,
                 server_command: Optional[str] = None,
                 server_args: Optional[Sequence[str]] = None,
                 cli_command: Optional[str] = None,
                 api_key: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 paper: Optional[bool] = None,
                 toolsets: str = READ_ONLY_TOOLSETS,
                 retries: int = 3, backoff: float = 0.4,
                 timeout: Optional[float] = None) -> None:
        raw = method if method is not None else os.getenv("HUARIZO_ALPACA_TOOL", "none")
        self.method = str(raw or "none").strip().lower()

        self.server_command = (server_command
                               or os.getenv("HUARIZO_ALPACA_MCP_COMMAND", "uvx"))
        self.server_args = list(server_args if server_args is not None
                                else _mcp_default_args())
        self.cli_command = cli_command or os.getenv("HUARIZO_ALPACA_CLI", "alpaca")
        self.toolsets = toolsets
        self.retries = max(1, int(retries))
        self.backoff = backoff
        # 2026-09-03: el timeout era fijo en 30 s y no se podia mover sin tocar
        # el codigo. `alpaca-mcp-server` tarda mas que eso en arrancar en frio
        # (banner de fastmcp + registro de 72 herramientas + handshake), y cada
        # intento fallido dispara un `safety_pause` PERSISTENTE: el bot quedaba
        # congelado sin que nadie lo hubiera pausado. Se vuelve configurable.
        self.timeout = float(timeout if timeout is not None
                             else os.getenv("HUARIZO_ALPACA_MCP_TIMEOUT", "30")
                             or 30)

        # Los secretos se capturan en la clausura, no en atributos: asi no
        # aparecen en `vars(self)`, en un `repr` ni en un volcado de estado.
        key = api_key if api_key is not None else os.getenv("APCA_API_KEY_ID", "")
        secret = secret_key if secret_key is not None else os.getenv("APCA_API_SECRET_KEY", "")
        paper_raw = paper if paper is not None else os.getenv("APCA_PAPER", "True")
        paper_flag = str(paper_raw).strip().lower() in ("1", "true", "yes", "on")
        # `paper` NO es un secreto, asi que si se guarda como atributo: sin el,
        # nadie puede verificar desde afuera contra que entorno apunta la capa
        # MCP/CLI y un `APCA_PAPER=false` pasaria desapercibido.
        self.paper = paper_flag

        self._build_env = lambda base=None: _child_env(key, secret, paper_flag,
                                                       self.toolsets, base)
        self._scrub = lambda text: _scrub(text, (key, secret))
        self._has_credentials = lambda: bool(key) and bool(secret)
        self._sleep = time.sleep

        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._mcp_session: Any = None
        self._stack: Optional[AsyncExitStack] = None
        self._owner_task: Any = None
        self._stop_event: Any = None
        self._session_lock: Any = None

    # ── Configuracion del subproceso ────────────────────────────────────────

    def build_child_env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Entorno completo del hijo. Las llaves van AQUI, nunca en argv."""
        return self._build_env(base)

    def server_params(self):
        """`StdioServerParameters` para `mcp.client.stdio.stdio_client`."""
        from mcp import StdioServerParameters  # import local: no ensucia el arranque

        return StdioServerParameters(
            command=self.server_command,
            args=list(self.server_args),
            env=self.build_child_env(),
        )

    # ── API publica ─────────────────────────────────────────────────────────

    def get_account_snapshot(self) -> Dict[str, Any]:
        """Snapshot de cuenta utilizable, o `ToolUnavailable`.

        Nunca devuelve una cuenta bloqueada ni un payload a medias: o sirve
        para medir el riesgo, o se lanza.
        """
        if self.method == "none":
            raise ToolUnavailable(
                "alpaca tool layer disabled (HUARIZO_ALPACA_TOOL=none)")
        if self.method not in VALID_METHODS:
            raise ToolUnavailable(
                f"unknown alpaca tool method {self.method!r} "
                f"(expected one of {', '.join(VALID_METHODS)})")
        if not self._has_credentials():
            raise ToolUnavailable(
                "missing alpaca credentials for the tool layer "
                "(APCA_API_KEY_ID / APCA_API_SECRET_KEY)")

        last: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                payload = self._call_tool(TOOL_GET_ACCOUNT, {})
            except ToolUnavailable:
                raise  # error de la herramienta: no es transitorio, no reintentar
            except Exception as exc:  # fallo de transporte: si lo es
                last = exc
                if attempt < self.retries:
                    self._sleep(self.backoff * attempt)
                continue

            snapshot = self.normalize_snapshot(payload)
            if snapshot is None:
                raise ToolUnavailable(
                    "alpaca tool returned an unusable account payload")
            if not snapshot.get("usable"):
                raise ToolUnavailable(
                    "alpaca account is not usable: "
                    f"{snapshot.get('blocked_reason') or 'blocked'}")
            return snapshot

        raise ToolUnavailable(self._scrub(
            f"alpaca tool unreachable after {self.retries} attempt(s): "
            f"{type(last).__name__}: {last}"))

    def normalize_snapshot(self, raw: Any) -> Optional[Dict[str, Any]]:
        """Normaliza el payload crudo. Pura y sin excepciones.

        Devuelve None si no sirve. Desenvuelve el guard anti-inyeccion
        (`{"_alpaca_mcp_security": {...}, "data": {...}}`) sin destruirlo: el
        wrapper se conserva para quien tenga que mostrarle la salida a un
        modelo.
        """
        data = raw
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return None

        snapshot: Dict[str, Any] = {}
        for field in _REQUIRED_MONEY:
            value = _finite(data.get(field))
            if value is None:
                return None  # sin equity/cash/buying_power no se mide el riesgo
            snapshot[field] = value
        for field in _OPTIONAL_MONEY:
            value = _finite(data.get(field))
            if value is not None:
                snapshot[field] = value

        reasons: List[str] = []
        for flag in _BLOCK_FLAGS:
            if data.get(flag) is True:
                reasons.append(flag)
        status = data.get("status")
        if isinstance(status, str) and status.strip().upper() != "ACTIVE":
            reasons.append(f"status={status.strip()}")

        snapshot["usable"] = not reasons
        snapshot["blocked_reason"] = ", ".join(reasons)
        snapshot["status"] = status.strip() if isinstance(status, str) else ""
        snapshot["source"] = self.method
        return snapshot

    def shutdown(self) -> None:
        """Cierra la sesion y detiene el hilo del loop. Seguro si esta vacio."""
        with self._lock:
            loop = self._loop
            self._loop = None
        if loop is None:
            return

        async def _close() -> None:
            # El stack se cierra DENTRO de la tarea duena: `stop.set()` la
            # despierta y ella sale de su propio `async with`. Cerrar el stack
            # desde aqui dejaria el subproceso huerfano.
            stop, owner = self._stop_event, self._owner_task
            if stop is not None:
                stop.set()
            if owner is not None:
                await asyncio.wait_for(owner, timeout=10.0)
            self._mcp_session = None
            self._stack = None
            self._owner_task = None
            self._stop_event = None

        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=15.0)
        except Exception as exc:
            print(f"[alpaca_tools] shutdown warning: {self._scrub(exc)}")
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
        with self._lock:
            self._thread = None
            self._mcp_session = None

    # ── Puente sincrono -> asincrono ────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Un solo hilo, un solo loop, creado de forma perezosa y con candado."""
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop

            ready = threading.Event()
            box: Dict[str, Any] = {}

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                box["loop"] = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            thread = threading.Thread(target=_runner,
                                      name="huarizo-alpaca-tools",
                                      daemon=True)
            self._thread = thread
            thread.start()
            ready.wait(timeout=15.0)
            self._loop = box.get("loop")
            if self._loop is None:
                raise ToolUnavailable("could not start the alpaca tool event loop")
            return self._loop

    def _call_tool(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Any:
        if self.method == "cli":
            return self._run_cli(self._cli_command(tool))
        if self.method != "mcp":
            raise ToolUnavailable(f"unsupported method {self.method!r}")
        loop = self._ensure_loop()

        async def _runner() -> Any:
            result = self._call_mcp(tool, args or {})
            if inspect.isawaitable(result):
                result = await result
            return result

        future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        return future.result(timeout=self.timeout)

    # ── Costuras (los tests las reemplazan; aqui vive el codigo real) ───────

    async def _open_session(self, stack: AsyncExitStack) -> Any:
        """Abre la sesion stdio dentro del stack de la tarea duena.

        Se llama UNA sola vez por vida de la capa: arrancar
        `alpaca-mcp-server` cuesta segundos y no se puede repetir por llamada.
        El stack lo abre y lo cierra la MISMA tarea (`_session_owner`): anyio
        se queja —con razon— si un cancel scope se cierra desde otra tarea.
        """
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        read, write = await stack.enter_async_context(
            stdio_client(self.server_params()))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _session_owner(self, ready: "asyncio.Event",
                             stop: "asyncio.Event") -> None:
        """Tarea unica que posee la sesion y su contexto de cancelacion."""
        async with AsyncExitStack() as stack:
            self._stack = stack
            self._mcp_session = await self._open_session(stack)
            ready.set()
            await stop.wait()
        self._mcp_session = None
        self._stack = None

    async def _ensure_session(self) -> Any:
        if self._mcp_session is not None:
            return self._mcp_session
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._mcp_session is not None:
                return self._mcp_session
            ready = asyncio.Event()
            self._stop_event = asyncio.Event()
            self._owner_task = asyncio.get_running_loop().create_task(
                self._session_owner(ready, self._stop_event))
            await asyncio.wait_for(ready.wait(), timeout=self.timeout)
            if self._mcp_session is None:
                raise ToolUnavailable("alpaca mcp session did not start")
            return self._mcp_session

    async def _call_mcp(self, tool: str, args: Dict[str, Any]) -> Any:
        """Llama una herramienta MCP y devuelve el payload ya parseado."""
        session = await self._ensure_session()
        result = await session.call_tool(tool, args)
        return _parse_mcp_result(result)

    # ── Camino CLI (alpha, solo lectura) ────────────────────────────────────

    @staticmethod
    def _cli_command(tool: str) -> List[str]:
        if tool == TOOL_GET_ACCOUNT:
            return ["account", "get"]
        raise ToolUnavailable(f"no read-only cli mapping for tool {tool!r}")

    def _run_cli(self, argv: Sequence[str]) -> Any:
        """Ejecuta el CLI. SOLO subcomandos de lectura.

        El CLI es Alpha Preview y **no pide confirmacion**: `order cancel-all`
        y `position close-all` ejecutan de inmediato. Por eso el allowlist es
        de lectura y no se puede saltar.
        """
        parts = [str(a) for a in argv]
        lowered = [p.lower() for p in parts]
        allowed = (
            (len(lowered) >= 2 and lowered[0] == "account"
             and lowered[1] in ("get", "config"))
            or (len(lowered) == 1 and lowered[0] == "doctor")
        )
        if not allowed:
            raise ToolUnavailable(
                f"alpaca cli command not allowed (read-only only): {' '.join(parts)}")

        try:
            proc = subprocess.run([self.cli_command] + parts,
                                  env=self.build_child_env(),
                                  capture_output=True, text=True,
                                  timeout=self.timeout)
        except OSError as exc:
            raise ToolUnavailable(
                self._scrub(f"alpaca cli could not run: {type(exc).__name__}: {exc}"))

        if proc.returncode != 0:
            raise ToolUnavailable(self._scrub(
                f"alpaca cli exited {proc.returncode}: "
                f"{(getattr(proc, 'stderr', '') or '')[:200]}"))
        try:
            return json.loads(proc.stdout or "")
        except ValueError:
            return None

    def __repr__(self) -> str:
        return f"<AlpacaTools method={self.method!r} retries={self.retries}>"


def _mcp_default_args() -> List[str]:
    """`uvx alpaca-mcp-server` — sobreescribible con HUARIZO_ALPACA_MCP_ARGS.

    La distincion entre "variable ausente" y "variable vacia" NO es cosmetic.
    Con `if custom:` (lo que habia antes), definir `HUARIZO_ALPACA_MCP_ARGS=`
    para decir "el ejecutable no necesita argumentos" caia en el default y
    producia `alpaca-mcp-server.exe alpaca-mcp-server`: el servidor recibia un
    subcomando inexistente, colgaba, y tras 3 reintentos el ciclo se ponia en
    safety pause. Vacio debe significar vacio.
    """
    custom = os.getenv("HUARIZO_ALPACA_MCP_ARGS")
    if custom is not None:
        return [a for a in custom.split() if a]
    return ["alpaca-mcp-server"]
