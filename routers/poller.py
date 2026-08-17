"""
Router del POLLER: dispara una pasada bajo demanda (modo pull).

  POST /poller/ejecutar  -> lee un lote de la cola del cliente y lo sincroniza ya.

Complementa al schedule automatico de Celery Beat: util para forzar una pasada
desde el panel o para pruebas, sin esperar al siguiente tick. La logica es la
misma (core.poller.procesar_lote); aqui solo se envuelve con auth y el mapeo de
errores HTTP habitual.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.poller import procesar_lote
from core.poller_source import polling_habilitado
from core.seguridad import resolver_tenant, verify_api_key
from odoo_universal import OdooConnectionError

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Poller"], dependencies=[Depends(verify_api_key)])


class EjecutarPollerRequest(BaseModel):
    """Parametros de una pasada manual del poller."""
    tenant: str = "default"
    limite: int = Field(50, ge=1, le=500, description="Maximo de filas a procesar.")


@router.post("/poller/ejecutar")
def ejecutar(req: EjecutarPollerRequest):
    """
    Ejecuta una pasada del poller de forma sincrona y devuelve el resumen
    (leidas, procesadas, con_error).

    Requiere el modo pull configurado (SOURCE_DATABASE_URL); si no, responde 400.
    """
    if not polling_habilitado():
        raise HTTPException(
            status_code=400,
            detail=(
                "Modo pull no configurado: define SOURCE_DATABASE_URL con la "
                "base de datos del cliente."
            ),
        )
    # Valida el tenant (400 si no existe) antes de tocar la cola.
    resolver_tenant(req.tenant)
    try:
        resultado = procesar_lote(tenant=req.tenant, limite=req.limite)
        return asdict(resultado)
    except OdooConnectionError as e:
        logger.error("POLLER_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
