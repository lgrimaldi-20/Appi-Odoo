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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.ingesta_smartier import IngestaError, diagnostico, ingerir_notas_entrega
from core.seguridad import verify_api_key
from core.smartier_client import smartier_habilitado

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
def ingerir(req: IngerirRequest):
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
