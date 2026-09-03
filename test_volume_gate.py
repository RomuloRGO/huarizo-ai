"""Confirmación por volumen: es un freno, no un factor del score.

Por qué existe este archivo
--------------------------
El 2026-09-03 se midió que el `volume` del CONTRATO de opción es 0 en todos
los contratos del plan gratuito (0 de 3.648 en SPY, 7-45 DTE). La única señal
de participación real es la del SUBYACENTE, que Alpaca sí entrega completa.

Se decidió usarla como CONFIRMACIÓN con three niveles, no como quinto factor
del score:

* `strong`   -> ratio >= 1.2 vs la media de 20 sesiones
* `neutral`  -> 0.5 <= ratio < 1.2
* `weak`     -> ratio < 0.5  (volumen muerto)
* `unknown`  -> no medible   (feed degradado, o pocas velas)

`strong` y `neutral` dejan pasar la entrada autónoma; `weak` y `unknown` la
bloquean. Este archivo fija esas dos propiedades que importan:

1. **Falla cerrado.** Si el volumen no se puede medir, no se opera. Vale más
   perder una entrada que operar contra un feed que devuelve basura.
2. **No mueve el score.** Dos análisis idénticos salvo el volumen deben dar el
   mismo `confidence_score`. Si algún día alguien "lo integra mejor" metiendo
   el volumen en la suma, este test se lo va a decir.

Todo es sintético: sin red, sin cuenta viva, sin reloj real.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import technical_engine as te
from agent_engine import HuarizoAgent
from technical_engine import (VOLUME_MA_WINDOW, VOLUME_MIN_RATIO,
                              VOLUME_STRONG_RATIO, compute_technical_indicators,
                              volume_allows_entry)

N_BARS = 60          # > VOLUME_MA_WINDOW para que la media exista en la última vela
BASE_VOLUME = 1_000_000.0


def _bars(last_volume=BASE_VOLUME, with_volume=True, n=N_BARS):
    """Velas sintéticas en ligera tendencia alcista.

    Se generan `n` velas con volumen constante y se reemplaza la ÚLTIMA por
    `last_volume`; la media de 20 sesiones queda ~1.000.000, así que el ratio
    observado es básicamente `last_volume / 1.000.000` con el arrastre de la
    propia última vela (1/20 del promedio).
    """
    # tz-aware UTC, como devuelve Alpaca de verdad (`2026-09-03 04:00:00+00:00`).
    # Si la columna fuera naive, mover la fecha de la última vela a hoy lanzaba
    # TypeError al mezclar naive con aware, y el test no probaría el feed real.
    idx = pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC")
    close = pd.Series([100.0 + i * 0.5 for i in range(n)], index=idx)
    frame = pd.DataFrame({
        # `t` no es decorativa: technical_engine (línea 201) y pattern_engine
        # (línea 60) la leen para fechar la última vela. Sin ella el análisis
        # muere con KeyError, igual que cualquier feed real de Alpaca.
        "t": idx,
        "Open": close - 0.2,
        "High": close + 0.6,
        "Low": close - 0.6,
        "Close": close,
    }, index=idx)
    if with_volume:
        vol = pd.Series([BASE_VOLUME] * n, index=idx, dtype=float)
        vol.iloc[-1] = float(last_volume)
        frame["Volume"] = vol
    return frame


NY = "America/New_York"


def _hoy_en_ny() -> pd.Timestamp:
    """Medianoche de HOY en Nueva York, expresada en UTC (04:00Z en verano)."""
    return pd.Timestamp.now(tz=NY).normalize().tz_convert("UTC")


def _fechar_ultima_barra(frame: pd.DataFrame, cuando: pd.Timestamp) -> pd.DataFrame:
    """Mueve sólo la fecha de la última vela. No toca precios ni volumen.

    `t` se usa únicamente para fechar (technical_engine:240, pattern_engine:60),
    así que moverla no altera ningún otro indicador.
    """
    frame.loc[frame.index[-1], "t"] = cuando
    return frame


def _sesion_en_curso(volumen_parcial: float) -> pd.DataFrame:
    """Libro normal cuya última vela es la de HOY, todavía acumulando volumen."""
    return _fechar_ultima_barra(_bars(volumen_parcial), _hoy_en_ny())


# ── El freno: `volume_allows_entry` ───────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"level": "strong", "ratio": 1.8},
    {"level": "neutral", "ratio": 0.9},
    # Sin `ratio`: el freno sólo mira el nivel.
    {"level": "neutral"},
])
def test_el_freno_deja_pasar_strong_y_neutral(payload):
    assert volume_allows_entry(payload) is True


@pytest.mark.parametrize("payload", [
    {"level": "weak", "ratio": 0.2},
    {"level": "unknown", "ratio": None},
    {"level": "unknown"},
    # Un nivel que no exista tampoco confirma: el freno no es una lista negra.
    {"level": "muy_alto"},
    {"ratio": 5.0},          # sin nivel
])
def test_el_freno_bloquea_weak_unknown_y_desconocidos(payload):
    assert volume_allows_entry(payload) is False


@pytest.mark.parametrize("payload", [
    None,
    {},
    "neutral",               # no es un dict
    42,
    [],
])
def test_el_freno_falla_cerrado_si_no_hay_medicion(payload):
    """Sin dato no se confirma. Nunca: es la propiedad de la que depende todo."""
    assert volume_allows_entry(payload) is False


# ── El indicador: niveles y umbrales ──────────────────────────────────────────

@pytest.mark.parametrize("multiplier,expected", [
    (3.0, "strong"),     # muy por encima de 1.2
    (1.25, "strong"),    # justo encima del umbral
    (1.0, "neutral"),    # volumen normal
    (0.55, "neutral"),   # justo encima del piso
    (0.4, "weak"),       # justo debajo del piso
    (0.05, "weak"),      # volumen muerto
])
def test_el_nivel_sale_del_ratio_contra_la_media_de_20(multiplier, expected):
    tech = compute_technical_indicators(_bars(BASE_VOLUME * multiplier))
    vol = tech["volume"]

    assert vol["level"] == expected
    assert vol["ratio"] is not None
    assert vol["ma_20"] is not None
    # Coherencia interna: el nivel y el flag de entrada nunca se contradicen.
    assert vol["confirms_entry"] is volume_allows_entry(vol)


def test_los_umbrales_estan_ordenados():
    """Si alguien invierte los umbrales, `neutral` deja de existir."""
    assert 0 < VOLUME_MIN_RATIO < VOLUME_STRONG_RATIO
    assert VOLUME_MA_WINDOW > 1


def test_sin_columna_de_volumen_el_analisis_no_explota():
    """Feed degradado: antes esto lanzaba KeyError y tumbaba el análisis entero.

    Ahora cae a una serie de NaN, el nivel queda en `unknown` y el freno falla
    cerrado. El análisis técnico sigue siendo utilizable.
    """
    tech = compute_technical_indicators(_bars(with_volume=False))

    assert tech["available"] is True
    assert tech["volume"]["level"] == "unknown"
    assert tech["volume"]["ratio"] is None
    assert tech["volume"]["confirms_entry"] is False


def test_volumen_en_cero_no_se_confunde_con_dato_faltante():
    """Volumen 0 es un dato real y medible: ratio 0 -> `weak`, no `unknown`.

    Importa porque 0 es justo el valor que devuelve el plan gratuito para el
    volumen del CONTRATO. Si el subyacente llegara así, hay que bloquear, no
    encogerse de hombros.
    """
    frame = _bars()
    # La media debe existir para que el ratio sea 0 y no None: se apaga sólo el
    # último día, con 19 sesiones normales detrás.
    frame.loc[frame.index[-1], "Volume"] = 0.0

    tech = compute_technical_indicators(frame)
    vol = tech["volume"]

    assert vol["level"] == "weak"
    assert vol["ratio"] == 0.0
    assert vol["confirms_entry"] is False


def test_volumen_parcialmente_nulo_queda_en_unknown():
    """Feed a medias: la media de 20 no se puede calcular => sin veredicto.

    Es el caso más traicionero, porque el volumen SÍ está presente y con
    valores plausibles: sólo falta la cola reciente. Se bloquea igual.
    """
    frame = _bars()
    frame.loc[frame.index[-5:], "Volume"] = float("nan")

    tech = compute_technical_indicators(frame)
    vol = tech["volume"]

    assert tech["available"] is True
    assert vol["ratio"] is None
    assert vol["level"] == "unknown"
    assert vol["confirms_entry"] is False


# ── La sesión en curso se descarta ────────────────────────────────────────────
# BUG REAL, medido el 2026-09-03. Alpaca devuelve la barra del día en curso con
# el volumen acumulado sólo hasta el instante de la consulta. Comparar eso
# contra la media de 20 sesiones COMPLETAS no mide participación: mide la hora
# del día. SPY, con exactamente los mismos datos:
#
#   08:00 Perú (pre-apertura, última barra cerrada 900.232) -> ratio 0.764 neutral
#   10:00 ET   (sesión, barra parcial 198.662)              -> ratio 0.175 weak
#
# El gate bloqueó los tres candidatos del primer ciclo autónomo (SPY 0.158,
# QQQ 0.105, AAPL 0.117) por un artefacto del reloj. Y lo habría hecho CADA
# mañana. Estos tests son los que impiden que vuelva.


def test_la_barra_de_hoy_se_descarta_y_la_de_ayer_no():
    """`drop_current_session` quita la vela de hoy y respeta la de ayer."""
    frame = _bars()
    volumen = frame["Volume"].astype(float)

    hoy = te.drop_current_session(volumen, _fechar_ultima_barra(frame.copy(),
                                                                _hoy_en_ny()))
    ayer = te.drop_current_session(volumen, _fechar_ultima_barra(
        frame.copy(), _hoy_en_ny() - pd.Timedelta(days=1)))

    assert len(hoy) == len(volumen) - 1
    assert len(ayer) == len(volumen)


def test_sin_fecha_conocible_se_devuelve_la_serie_intacta():
    """Si no se puede fechar, mejor un ratio dudoso que romper el análisis."""
    frame = _bars()
    volumen = frame["Volume"].astype(float)

    # Se compara por valor, no por identidad: `volumen.iloc[:20]` devuelve un
    # objeto distinto en cada llamada, así que `is` fallaría sin decir nada útil.
    assert te.drop_current_session(
        volumen, frame.drop(columns=["t"])).equals(volumen)

    corto = volumen.iloc[:VOLUME_MA_WINDOW]
    assert te.drop_current_session(corto, frame).equals(corto)


def test_una_barra_parcial_de_hoy_no_bloquea_la_entrada():
    """El caso exacto del bug: todo normal, salvo que la última vela es de hoy.

    Detrás de la barra parcial hay 19 sesiones a 1.000.000, así que la
    participación real es normalísima. Antes del arreglo el ratio caía a ~0.21
    y la entrada se bloqueaba; ahora se compara la última sesión CERRADA.
    """
    tech = compute_technical_indicators(_sesion_en_curso(BASE_VOLUME * 0.2))
    vol = tech["volume"]

    assert vol["partial_session_dropped"] is True
    assert vol["ratio"] == 1.0
    assert vol["level"] == "neutral"
    assert vol["confirms_entry"] is True


def test_si_la_sesion_esta_cerrada_el_volumen_bajo_si_bloquea():
    """Guarda contra pasarse de listo: descartar de más sería peor que de menos.

    Misma caída de volumen, pero en una sesión YA CERRADA. Ahí sí es una señal
    real de que no hay participación, y tiene que bloquear.
    """
    frame = _fechar_ultima_barra(_bars(BASE_VOLUME * 0.2),
                                 _hoy_en_ny() - pd.Timedelta(days=1))

    vol = compute_technical_indicators(frame)["volume"]

    assert vol["partial_session_dropped"] is False
    assert vol["level"] == "weak"
    assert vol["confirms_entry"] is False


@pytest.mark.parametrize("fraccion", [0.2, 0.6, 0.95])
def test_el_veredicto_no_cambia_con_la_hora_del_dia(fraccion):
    """La propiedad de fondo: el mismo día, a tres horas distintas, mismo fallo.

    La barra parcial va creciendo: 200.000 a media mañana, 950.000 casi al
    cierre. Si el ratio la tomara en cuenta, el gate diría `weak` y luego
    `neutral`, y el bot abriría o no según A QUÉ HORA se le ocurriera mirar.
    """
    vol = compute_technical_indicators(
        _sesion_en_curso(BASE_VOLUME * fraccion))["volume"]

    assert vol["level"] == "neutral", (
        f"con la sesión {fraccion:.0%} acumulada el nivel salió "
        f"{vol['level']} (ratio {vol['ratio']}): el gate está midiendo la hora "
        f"del día en lugar de la participación")
    assert vol["confirms_entry"] is True


# ── La propiedad que define la decisión: no mueve el score ────────────────────

class _FakeService:
    """Sólo lo que `analyze` pide. Nada de red."""

    def __init__(self, frame):
        self._frame = frame

    def get_stock_bars(self, ticker, days=250):
        return self._frame.copy()

    def get_stock_snapshot(self, ticker):
        return {"change_pct": 0.0, "price": float(self._frame["Close"].iloc[-1])}

    def get_stock_news(self, ticker, limit=4):
        return []

    def get_dividends(self, ticker, days=365):
        return []


def test_el_volumen_no_mueve_el_confidence_score():
    """Dos libros idénticos salvo el volumen => el mismo score y la misma señal.

    Este es el test que sostiene la decisión del 2026-09-03: el volumen es un
    freno sobre la entrada autónoma, NO un quinto factor. Si un día entra en
    la suma de 50 + tendencia ±20 + estocástico ±15 + RSI ±10 + vela ±10, este
    test falla y nadie lo coló por descuido.
    """
    agent = HuarizoAgent()

    muerto = agent.analyze("SYNTH", _FakeService(_bars(BASE_VOLUME * 0.05)))
    vivo = agent.analyze("SYNTH", _FakeService(_bars(BASE_VOLUME * 5.0)))

    assert muerto["available"] is True and vivo["available"] is True
    # Los dos niveles son los que queríamos comparar, no dos `unknown`.
    assert muerto["volume_confirmation"]["level"] == "weak"
    assert vivo["volume_confirmation"]["level"] == "strong"

    assert muerto["confidence_score"] == vivo["confidence_score"]
    assert muerto["signal"] == vivo["signal"]


def test_el_volumen_viaja_en_el_payload_del_analisis():
    """`analyze` lo expone arriba para que el ciclo no tenga que recalcular."""
    analysis = HuarizoAgent().analyze("SYNTH", _FakeService(_bars(BASE_VOLUME * 3.0)))

    conf = analysis["volume_confirmation"]
    assert conf["level"] == "strong"
    assert conf["confirms_entry"] is True
    assert conf["ratio"] == analysis["technical"]["volume"]["ratio"]
