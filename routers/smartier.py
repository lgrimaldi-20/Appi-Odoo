"""
Router de INGESTA desde Smartier (modo pull, lado lectura).

  POST /smartier/ingerir     -> lee notas de entrega de Smartier y las encola.
  GET  /smartier/diagnostico -> radiografia de lo que hay en Smartier.

Complementa al schedule de Celery Beat: util para forzar una pasada sin esperar
al tick, y para comprobar desde el panel que la API responde y con que datos.
La logica vive en core.ingesta_smartier; aqui solo se envuelve con auth y el
mapeo de errores HTTP.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.ingesta_smartier import IngestaError, diagnostico, ingerir_notas_entrega
from core.limites import limitar
from core.maestros_smartier import MaestrosError, sincronizar_clientes
from core.seguridad import resolver_tenant, verify_api_key
from core.smartier_client import smartier_habilitado
from odoo_universal import OdooConnectionError

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Smartier"], dependencies=[Depends(verify_api_key)])


class IngerirRequest(BaseModel):
    """Parametros de una pasada manual de ingesta."""
    limite: int = Field(200, ge=1, le=1000, description="Maximo de notas a leer.")
    desde: str | None = Field(
        None,
        description="Fecha ISO desde la que leer. Si se omite, usa la marca de agua.",
    )


@router.post("/smartier/ingerir")
@limitar("6/minute")
def ingerir(req: IngerirRequest, request: Request):
    """
    Ejecuta una pasada de ingesta de forma sincrona y devuelve el resumen
    (leidas, encoladas, omitidas, marca_agua).

    Requiere SMARTIER_BASE_URL y SMARTIER_API_KEY; si faltan, responde 400.
    """
    if not smartier_habilitado():
        raise HTTPException(
            status_code=400,
            detail=(
                "Smartier no configurado: define SMARTIER_BASE_URL y "
                "SMARTIER_API_KEY en el entorno."
            ),
        )
    try:
        return asdict(ingerir_notas_entrega(limite=req.limite, desde=req.desde))
    except IngestaError as e:
        logger.error("INGESTA_SMARTIER | %s", e)
        raise HTTPException(status_code=502, detail=str(e))


class MaestrosRequest(BaseModel):
    """Parametros de una pasada manual de datos maestros."""
    tenant: str = Field("default", description="Tenant de Odoo destino.")
    limite: int = Field(200, ge=1, le=1000,
                        description="Maximo de clientes a leer.")


@router.post("/smartier/maestros")
@limitar("6/minute")
def sincronizar_maestros(req: MaestrosRequest, request: Request):
    """
    Crea o actualiza en Odoo los clientes de Smartier, de forma sincrona.

    Es la pasada que Beat ejecuta sola cada 15 minutos; este endpoint sirve
    para forzarla, tipicamente cuando acaban de dar de alta un cliente y no se
    quiere esperar al siguiente tick para poder facturarle.
    """
    if not smartier_habilitado():
        raise HTTPException(
            status_code=400,
            detail=(
                "Smartier no configurado: define SMARTIER_BASE_URL y "
                "SMARTIER_API_KEY en el entorno."
            ),
        )
    odoo = resolver_tenant(req.tenant)
    try:
        return asdict(sincronizar_clientes(odoo, limite=req.limite))
    except MaestrosError as e:
        logger.error("MAESTROS_SMARTIER | %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except OdooConnectionError as e:
        logger.error("MAESTROS_SMARTIER | Odoo no disponible: %s", e)
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/smartier/diagnostico")
def ver_diagnostico():
    """
    Cuenta lo que hay en Smartier (clientes, productos, notas) y avisa de los
    datos que faltarian para poder facturar en Odoo, como los RIF ausentes.
    No encola nada.
    """
    if not smartier_habilitado():
        return {"habilitado": False}
    try:
        return diagnostico()
    except Exception as e:  # noqa: BLE001 - el diagnostico nunca debe tumbar la API
        logger.error("DIAGNOSTICO_SMARTIER | %s", e)
        raise HTTPException(status_code=502, detail=str(e))
