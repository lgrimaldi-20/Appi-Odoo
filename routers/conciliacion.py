"""
Router de CONCILIACION: POST /conciliar.

Cruza (concilia) una factura con un pago en Odoo a partir de sus IDs de Odoo.
Idempotente: una pareja ya conciliada devuelve idempotente=true sin tocar Odoo.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.conciliacion import ConciliacionError, conciliar
from core.seguridad import resolver_tenant, verify_api_key
from odoo_universal import OdooConnectionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Conciliacion"], dependencies=[Depends(verify_api_key)])


class ConciliacionRequest(BaseModel):
    """IDs (en Odoo) de la factura y el pago a conciliar."""
    factura_id_odoo: int = Field(..., description="ID de la factura (account.move) en Odoo.")
    pago_id_odoo: int = Field(..., description="ID del pago (account.payment) en Odoo.")
    factura_id_origen: str = ""
    pago_id_origen: str = ""
    tenant: str = "default"


@router.post("/conciliar")
def conciliar_factura_pago(req: ConciliacionRequest):
    """
    Concilia una factura con un pago (cruza sus apuntes contables).
    """
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)
    try:
        resultado = conciliar(
            req.factura_id_odoo,
            req.pago_id_odoo,
            odoo,
            factura_id_origen=req.factura_id_origen,
            pago_id_origen=req.pago_id_origen,
        )
    except ConciliacionError as e:
        logger.warning("CONCILIACION_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("CONCILIACION_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")

    return resultado
