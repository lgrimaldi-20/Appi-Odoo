"""
Middleware API-Odoo
Expone un endpoint universal para conectar agentes externos, Excel y scripts
con un servidor Odoo via JSON-RPC.
"""

import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# El .env se carga ANTES de importar los modulos del proyecto: core.state_store
# lee DATABASE_URL en tiempo de import (a nivel de modulo), asi que si se
# cargara despues se quedaria con el valor por defecto y el .env se ignoraria.
load_dotenv()

from odoo_universal import (  # noqa: E402
    OdooConnectionError,
    OdooExecutionError,
    OdooUniversalAPI,
    register_tenant,
    get_tenant,
)
from core.state_store import buscar_mapeo, init_db  # noqa: E402

# ---------------------------------------------------------------------------
# Configuracion inicial
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("api-odoo")

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

app = FastAPI(
    title="API-Odoo Middleware",
    description="Middleware universal para conectar Odoo con agentes externos.",
    version="1.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Base de datos de control (state store). Crea las tablas si no existen.
init_db()
logger.info("Base de datos de control inicializada.")

# Routers de negocio (facturas, pagos). Endpoints con estado e idempotencia.
# panel: dashboard de observabilidad (lectura de sync_map/sync_log).
# asientos: crear/eliminar asientos contables (account.move tipo 'entry').
from routers import asientos, conciliacion, facturas, inventario, pagos, panel, poller  # noqa: E402  (import tras crear la app)

app.include_router(facturas.router)
app.include_router(pagos.router)
app.include_router(conciliacion.router)
app.include_router(inventario.router)
app.include_router(asientos.router)
app.include_router(poller.router)  # /poller/ejecutar (pasada manual, protegido)
app.include_router(panel.router)   # /panel (HTML)
app.include_router(panel.datos)    # /panel/api/* (JSON, protegido)

# ---------------------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------------------

API_KEY = os.getenv("API_KEY", "")
if not API_KEY:
    logger.warning("API_KEY no configurada - el endpoint /odoo NO esta protegido")

_raw_models = os.getenv("ALLOWED_MODELS", "")
_raw_methods = os.getenv("ALLOWED_METHODS", "")

ALLOWED_MODELS: set[str] = {m.strip() for m in _raw_models.split(",") if m.strip()}
ALLOWED_METHODS: set[str] = {m.strip() for m in _raw_methods.split(",") if m.strip()}

# ---------------------------------------------------------------------------
# Conexion Odoo principal (tenant "default")
# ---------------------------------------------------------------------------

try:
    _default_odoo = OdooUniversalAPI(
        url=os.getenv("ODOO_URL", ""),
        db=os.getenv("ODOO_DB", ""),
        username=os.getenv("ODOO_USERNAME", ""),
        password=os.getenv("ODOO_PASSWORD", ""),
    )
    register_tenant("default", _default_odoo)
    logger.info("Conexion con Odoo establecida correctamente.")
except OdooConnectionError as e:
    logger.error("No se pudo conectar a Odoo al iniciar: %s", e)
    _default_odoo = None

# ---------------------------------------------------------------------------
# Seguridad: dependencia de API Key
# ---------------------------------------------------------------------------


def verify_api_key(x_api_key: Optional[str] = Depends(api_key_header)):
    """
    Verifica la cabecera X-Api-Key.
    Si API_KEY no esta configurada, pasa sin validar (modo desarrollo).
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API Key invalida o ausente.")


# ---------------------------------------------------------------------------
# Modelos de peticion / respuesta
# ---------------------------------------------------------------------------


class OdooRequest(BaseModel):
    model: str
    method: str
    args: list = []
    kwargs: dict = {}
    tenant: str = "default"

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if ALLOWED_MODELS and v not in ALLOWED_MODELS:
            raise ValueError(
                f"Modelo '{v}' no permitido. Permitidos: {sorted(ALLOWED_MODELS)}"
            )
        return v

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if ALLOWED_METHODS and v not in ALLOWED_METHODS:
            raise ValueError(
                f"Metodo '{v}' no permitido. Permitidos: {sorted(ALLOWED_METHODS)}"
            )
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Sistema"])
def health_check():
    """
    Verifica el estado del servicio y la conexion con Odoo.
    No requiere API Key - pensado para monitores y load balancers.
    """
    odoo_ok = _default_odoo is not None and _default_odoo.uid is not None
    status = "ok" if odoo_ok else "degradado"
    return {
        "status": status,
        "odoo_conectado": odoo_ok,
        # Version del servidor Odoo detectada en el arranque. Util para verificar
        # de un vistazo contra que version se esta hablando (17/18/19).
        "odoo_version": getattr(_default_odoo, "version", None) if odoo_ok else None,
        "modelos_permitidos": sorted(ALLOWED_MODELS) if ALLOWED_MODELS else "todos",
        "metodos_permitidos": sorted(ALLOWED_METHODS) if ALLOWED_METHODS else "todos",
    }


@app.get(
    "/estado/{entidad}/{id_origen}",
    tags=["Sistema"],
    dependencies=[Depends(verify_api_key)],
)
def estado_sincronizacion(entidad: str, id_origen: str):
    """
    Consulta el estado de sincronizacion de un registro de origen en la base de
    datos de control (state store). Util para seguimiento e idempotencia.

    Devuelve 404 si el registro nunca se ha intentado sincronizar.
    """
    mapa = buscar_mapeo(entidad, id_origen)
    if mapa is None:
        raise HTTPException(
            status_code=404,
            detail=f"Sin registro de sincronizacion para {entidad}/{id_origen}.",
        )
    return {
        "entidad": mapa.entidad,
        "id_origen": mapa.id_origen,
        "model_odoo": mapa.model_odoo,
        "id_odoo": mapa.id_odoo,
        "estado": mapa.estado,
        "error": mapa.error,
        "actualizado": mapa.actualizado.isoformat() if mapa.actualizado else None,
    }


@app.post("/odoo", tags=["Odoo"], dependencies=[Depends(verify_api_key)])
@limiter.limit("60/minute")
def odoo_proxy(req: OdooRequest, request: Request):
    """
    Endpoint universal para ejecutar operaciones sobre modelos de Odoo.

    Requiere cabecera: X-Api-Key: <tu_clave>

    Ejemplo de peticion:
    ```json
    {
      "model": "account.move",
      "method": "search_read",
      "args": [[["state", "=", "posted"]]],
      "kwargs": {"fields": ["name", "amount_total"], "limit": 5}
    }
    ```
    """
    inicio = time.time()
    ip = request.client.host if request.client else "desconocida"

    logger.info(
        "PETICION | ip=%s tenant=%s model=%s method=%s",
        ip, req.tenant, req.model, req.method,
    )

    try:
        odoo = get_tenant(req.tenant)
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"Tenant '{req.tenant}' no configurado.",
        )

    try:
        result = odoo.execute(req.model, req.method, *req.args, **req.kwargs)
    except OdooConnectionError as e:
        logger.error("ERROR_CONEXION | model=%s method=%s error=%s", req.model, req.method, e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
    except OdooExecutionError as e:
        logger.warning("ERROR_ODOO | model=%s method=%s error=%s", req.model, req.method, e)
        raise HTTPException(status_code=422, detail=f"Error de Odoo: {e}")
    except Exception as e:
        logger.exception("ERROR_INESPERADO | model=%s method=%s", req.model, req.method)
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")

    duracion = round((time.time() - inicio) * 1000)
    logger.info(
        "OK | ip=%s model=%s method=%s duracion_ms=%d",
        ip, req.model, req.method, duracion,
    )
    return {"result": result}
