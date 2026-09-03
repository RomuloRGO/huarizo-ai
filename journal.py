"""Journal de trades de opciones para Huarizo AI.

Persistencia JSON simple con lock de thread (Flask + exit manager concurrentes).
Sin DB: respeta el protocolo de aislamiento del subproyecto.
"""

import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

JOURNAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_journal.json")

#: Reintentos del renombrado. En Windows `os.replace` falla si el destino está
#: abierto en lectura por CUALQUIER handle — incluido un antivirus escaneando el
#: archivo — así que un bloqueo momentáneo no puede costarnos la escritura.
#: 6 intentos con backoff lineal = ~210 ms como máximo.
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF_S = 0.01

#: `RLock`, no `Lock`: `add_entry` y `update_entry` ya sostienen el lock cuando
#: llaman a `load_journal`, y ahora `load_journal` lo vuelve a tomar para que
#: nadie reemplace el archivo mientras se lee. Con un `Lock` a secas eso sería
#: un auto-deadlock.
_lock = threading.RLock()


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _read_raw(path: str) -> List[Dict[str, Any]]:
    """Lee y parsea SIN lock. Para uso interno: quien llama ya lo sostiene."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[journal] Error leyendo {path}: {e}")
        return []


def load_journal(path: str = JOURNAL_PATH) -> List[Dict[str, Any]]:
    """Lee el journal completo. Archivo ausente o corrupto -> [].

    Toma `_lock`, y no es redundante con que la escritura sea atómica: en
    Windows `os.replace` falla con `PermissionError` si el archivo de destino
    está abierto por otro hilo. Una lectura concurrente no se corrompería —
    **tumbaría la escritura**, que es peor. Leyendo bajo el mismo lock, cuando
    alguien va a reemplazar el archivo no queda ningún handle abierto.
    """
    with _lock:
        return _read_raw(path)


def save_journal(entries: List[Dict[str, Any]], path: str = JOURNAL_PATH) -> None:
    with _lock:
        _write(entries, path)


def add_entry(entry: Dict[str, Any], path: str = JOURNAL_PATH) -> Dict[str, Any]:
    """Agrega una entrada con id secuencial y status 'open'. Thread-safe."""
    with _lock:
        entries = _read_raw(path)
        new_id = max((_safe_int(e.get("id"), 0) for e in entries), default=0) + 1
        record = {"id": new_id, "status": "open", **entry}
        entries.append(record)
        _write(entries, path)
        return record


def update_entry(journal_id: int, updates: Dict[str, Any],
                 path: str = JOURNAL_PATH) -> Optional[Dict[str, Any]]:
    """Actualiza la entrada cuyo id coincida. None si no existe. Thread-safe."""
    with _lock:
        entries = _read_raw(path)
        jid = _safe_int(journal_id, -1)
        for e in entries:
            if _safe_int(e.get("id"), -1) == jid:
                e.update(updates)
                _write(entries, path)
                return e
    return None


def open_entries(path: str = JOURNAL_PATH) -> List[Dict[str, Any]]:
    """Retorna solo las entradas con status 'open'."""
    return [e for e in load_journal(path) if e.get("status") == "open"]


def realized_pnl(path: str = JOURNAL_PATH) -> float:
    """Suma pnl_usd de entradas cerradas (ignora valores no numericos)."""
    total = 0.0
    for e in load_journal(path):
        if e.get("status") != "closed":
            continue
        try:
            total += float(e.get("pnl_usd", 0.0))
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _write(entries: List[Dict[str, Any]], path: str) -> None:
    """Serializa el journal a JSON de forma ATOMICA (caller debe sostener _lock).

    Antes esto era `open(path, "w")` + `json.dump`: esa forma **trunca el
    archivo a cero** y lo va rellenando por partes, asi que cualquier lectura
    concurrente puede encontrar un archivo vacio o a medio escribir. Y como
    `load_journal` se traga el error de parseo y devuelve `[]`, el fallo no
    parecia un fallo: una posicion abierta simplemente desaparecia del mapa del
    bot, disfrazada de "entrada no encontrada".

    Medido en la suite el 2026-09-02, en una sola corrida:
      `Expecting value: line 1 column 1 (char 0)`   <- archivo truncado a cero
      `Extra data: line 9 column 4 (char 117)`      <- escrituras entremezcladas

    Escribir a un temporal y renombrar hace el cambio indivisible: quien lea ve
    el archivo viejo completo o el nuevo completo, nunca algo intermedio. Es el
    mismo patron que ya usan `execution_ledger`, `risk_gate`,
    `account_onboarding` y `llm_provider`; el journal era el unico que no.

    El renombrado se reintenta porque en **Windows** `os.replace` lanza
    `PermissionError` si el destino esta abierto en lectura, aunque sea por un
    handle que no es nuestro: un antivirus escaneando, un editor, una herramienta
    de sincronizacion. Sin reintento ese bloqueo momentaneo se comeria la
    escritura. Con reintento se reintenta y, si de veras esta bloqueado, falla
    EN VOZ ALTA en vez de perder la entrada en silencio.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".journal_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)

        ultimo: Optional[BaseException] = None
        for intento in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                ultimo = e
                time.sleep(_REPLACE_BACKOFF_S * (intento + 1))
        raise ultimo  # type: ignore[misc]
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
