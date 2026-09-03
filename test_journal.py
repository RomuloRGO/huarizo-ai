"""Tests de persistencia del journal de opciones (JSON con lock)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from journal import (
    load_journal, save_journal, add_entry, update_entry, open_entries, realized_pnl,
)


@pytest.fixture
def jpath(tmp_path):
    return str(tmp_path / "journal_test.json")


def test_load_archivo_inexistente_retorna_lista_vacia(jpath):
    assert load_journal(jpath) == []


def test_load_json_corrupto_retorna_lista_vacia(jpath):
    with open(jpath, "w", encoding="utf-8") as f:
        f.write("{no es json")
    assert load_journal(jpath) == []


def test_add_entry_asigna_id_secuencial(jpath):
    e1 = add_entry({"ticker": "AAPL", "occ": "X1"}, path=jpath)
    e2 = add_entry({"ticker": "NVDA", "occ": "X2"}, path=jpath)
    assert e1["id"] == 1 and e2["id"] == 2
    assert e1["status"] == "open"


def test_update_entry_cierra_posicion(jpath):
    e = add_entry({"ticker": "AAPL"}, path=jpath)
    upd = update_entry(e["id"], {"status": "closed", "pnl_usd": 55.5}, path=jpath)
    assert upd["status"] == "closed" and upd["pnl_usd"] == 55.5
    assert update_entry(999, {}, path=jpath) is None


def test_open_entries_y_realized_pnl(jpath):
    e1 = add_entry({"ticker": "A"}, path=jpath)
    e2 = add_entry({"ticker": "B"}, path=jpath)
    update_entry(e2["id"], {"status": "closed", "pnl_usd": 100.0}, path=jpath)
    e3 = add_entry({"ticker": "C"}, path=jpath)
    update_entry(e3["id"], {"status": "closed", "pnl_usd": -40.0}, path=jpath)
    assert [x["ticker"] for x in open_entries(jpath)] == ["A"]
    assert realized_pnl(jpath) == 60.0


def test_realized_pnl_ignora_pnl_no_numerico(jpath):
    e = add_entry({"ticker": "X"}, path=jpath)
    update_entry(e["id"], {"status": "closed", "pnl_usd": "n/a"}, path=jpath)
    assert realized_pnl(jpath) == 0.0


def test_save_load_roundtrip(jpath):
    save_journal([{"id": 7, "ticker": "T"}], path=jpath)
    assert load_journal(jpath)[0]["id"] == 7


def test_add_entry_tolerante_a_ids_malformados_existentes(jpath):
    save_journal([{"id": "abc", "ticker": "Z"}, {"id": "2", "ticker": "Y"}], path=jpath)
    e = add_entry({"ticker": "W"}, path=jpath)
    assert e["id"] == 3


# ── Escritura atómica ────────────────────────────────────────────────────────
#
# `_write` era `open(path, "w")` + `json.dump`: trunca a cero y rellena por
# partes, así que una lectura concurrente podía ver un archivo vacío o a medio
# escribir. Como `load_journal` se traga el error y devuelve `[]`, el síntoma
# NO era un error: era una posición abierta que desaparecía del mapa del bot.
# Observado en la suite el 2026-09-02:
#   Expecting value: line 1 column 1 (char 0)   -> truncado a cero
#   Extra data: line 9 column 4 (char 117)      -> escrituras entremezcladas

def test_escritura_concurrente_nunca_deja_un_archivo_ilegible(jpath):
    """Lectores y escritores a la vez: el journal NUNCA se ve vacío ni a medias.

    Los lectores van por `load_journal`, que es como lo lee la app. Durante la
    prueba el journal siempre tiene filas, así que ver `[]` o una fila sin
    `ticker` significa que se leyó un archivo truncado o a medio escribir.
    """
    import threading

    save_journal([{"id": 1, "ticker": "SEED"}], path=jpath)
    lecturas_malas = []
    fallos_escritura = []
    stop = threading.Event()

    def escritor(n):
        try:
            for i in range(50):
                # Filas de largo variable: es lo que deja el residuo "Extra data"
                # cuando un escritor más corto pisa a uno más largo.
                filas = [{"id": k, "ticker": f"T{n}-{i}-{k}" * (n + 1)}
                         for k in range(1, 8 + n)]
                save_journal(filas, path=jpath)
        except Exception as e:
            fallos_escritura.append(f"escritor {n}: {type(e).__name__}: {e}")

    def lector():
        while not stop.is_set():
            entradas = load_journal(jpath)
            if not entradas:
                lecturas_malas.append("journal vacío en medio de la tormenta")
                return
            if any("ticker" not in e for e in entradas):
                lecturas_malas.append("fila incompleta")
                return

    lectores = [threading.Thread(target=lector) for _ in range(4)]
    escritores = [threading.Thread(target=escritor, args=(n,)) for n in range(3)]
    for t in lectores + escritores:
        t.start()
    for t in escritores:
        t.join()
    stop.set()
    for t in lectores:
        t.join()

    assert lecturas_malas == [], lecturas_malas[:3]
    # En Windows `os.replace` lanza PermissionError si el destino está abierto;
    # por eso las lecturas también van bajo el lock, no sólo las escrituras.
    assert fallos_escritura == [], fallos_escritura[:3]


def test_el_archivo_final_es_un_journal_valido(jpath):
    """Tras la tormenta, lo que queda en disco es una escritura completa."""
    import threading

    def escritor(n):
        for i in range(30):
            save_journal([{"id": k, "ticker": f"T{n}-{i}"} for k in range(1, 10)],
                         path=jpath)

    hilos = [threading.Thread(target=escritor, args=(n,)) for n in range(3)]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join()

    entradas = load_journal(jpath)
    assert isinstance(entradas, list)
    assert len(entradas) == 9  # una escritura completa, no un frankenstein
    assert all("ticker" in e for e in entradas)


def test_incluso_quien_no_pasa_por_el_lock_ve_un_archivo_valido(jpath):
    """La atomicidad protege también al lector externo.

    Otro proceso —o una herramienta— no puede tomar nuestro lock, así que para
    él la única garantía es que el archivo NUNCA esté a medio escribir. Eso lo
    da el renombrado atómico, no el lock. Con la escritura anterior
    (`open(path,"w")`) este test ve archivos truncados a cero.
    """
    import threading
    import time

    save_journal([{"id": 1, "ticker": "SEED"}], path=jpath)
    corruptas = []
    stop = threading.Event()

    def escritor(n):
        for i in range(40):
            filas = [{"id": k, "ticker": f"T{n}-{i}-{k}" * (n + 1)}
                     for k in range(1, 8 + n)]
            try:
                save_journal(filas, path=jpath)
            except PermissionError:
                # Windows: el lector externo tenía el archivo abierto justo en
                # el renombrado. Se reintentó y se agotaron los intentos; es un
                # fallo ruidoso, no una escritura perdida en silencio.
                pass

    def lector_externo():
        """No usa `load_journal`: lee el archivo a pelo, como un tercero."""
        while not stop.is_set():
            try:
                with open(jpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (FileNotFoundError, PermissionError):
                # En Windows, abrir justo durante un `os.replace` puede dar
                # PermissionError. Es transitorio y se reintenta; LO QUE NO
                # PUEDE PASAR es leer un archivo a medio escribir.
                time.sleep(0.001)
                continue
            except Exception as e:
                corruptas.append(f"{type(e).__name__}: {e}")
                return
            if not data or any("ticker" not in e for e in data):
                corruptas.append("journal vacío o incompleto")
                return
            time.sleep(0.001)

    hilos = [threading.Thread(target=lector_externo) for _ in range(3)]
    hilos += [threading.Thread(target=escritor, args=(n,)) for n in range(2)]
    for t in hilos:
        t.start()
    for t in hilos[3:]:
        t.join()
    stop.set()
    for t in hilos[:3]:
        t.join()

    assert corruptas == [], corruptas[:3]


def test_no_quedan_temporales_sueltos(jpath):
    """El temporal se renombra o se borra: nunca ensucia el directorio."""
    save_journal([{"id": 1, "ticker": "A"}], path=jpath)
    sueltos = [p for p in os.listdir(os.path.dirname(jpath))
               if p.startswith(".journal_")]
    assert sueltos == []
