"""Configuracion global de pytest: los tests deben ser hermeticos.

Problema que este archivo corrige
---------------------------------
El `.env` de produccion activa la capa oficial de Alpaca
(`HUARIZO_ALPACA_TOOL=mcp`), que es un requisito del concurso. `app.py`
construye `ALPACA_TOOLS` **al importarse**, asi que con esa variable puesta
toda la suite pasaba a llamar al MCP server real durante los tests.

La consecuencia no era solo lentitud. En `_run_one_options_cycle` la capa MCP
tiene precedencia sobre `alpaca_service.get_account()`, asi que los tests que
mockean la cuenta para provocar una pausa por perdida diaria quedaban
silenciosamente anulados: el MCP devolvia la cuenta real (sin perdida), el
ciclo no se saltaba y terminaba tomando la rama de apertura. El test pasaba o
fallaba segun el estado de una cuenta viva.

Por eso aqui se fuerza la capa a `none`: los tests ejercitan la logica
determinista, y la integracion MCP se cubre aparte en `test_alpaca_tools.py`,
que sustituye el transporte (sin red y sin subprocesos).

Debe fijarse ANTES de importar `app`. `load_dotenv()` usa `override=False`,
asi que una variable ya presente en el entorno gana y el `.env` no la pisa.
"""
import os

# Debe ejecutarse en la importacion de conftest (pytest la carga antes que
# cualquier modulo de test), no en un fixture: para cuando un fixture corra,
# `app` ya fue importado y `ALPACA_TOOLS` ya existe.
os.environ["HUARIZO_ALPACA_TOOL"] = "none"

# El autopilot tampoco debe arrancar por accidente dentro de un test.
os.environ.pop("HUARIZO_AUTOPILOT_ENABLED", None)

# La ruta de LLM de Vertex se apaga aqui, y no por higiene: es la unica que
# **factura** (Application Default Credentials contra un proyecto con billing
# habilitado). Si la maquina de quien corre los tests tiene Vertex activo en su
# entorno —o si algún dia la suite llega a cargar el `.env`—, cualquier test que
# pase por `_generate_llm_thesis` con Vertex disponible saldria a la red y
# consumiria credito real. `HUARIZO_LLM_VERTEX=0` lo cierra por la via oficial,
# igual que `HUARIZO_ALPACA_TOOL=none` cierra la via de Alpaca.
os.environ["HUARIZO_LLM_VERTEX"] = "0"
os.environ.pop("HUARIZO_LLM_VERTEX_MODEL", None)
os.environ.pop("GOOGLE_VERTEX_PROJECT", None)
os.environ.pop("GOOGLE_VERTEX_LOCATION", None)
os.environ.pop("GEMINI_API_KEY", None)
