"""
Router de INVENTARIO: ajuste y consulta de existencias.

  POST /stock/ajustar   -> aplica un ajuste de existencias (idempotente)
  POST /stock/consultar -> consulta la existencia actual (solo lectura)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.inventario import InventarioError, ajustar_stock, consultar_stock
from core.seguridad import resolver_tenant, verify_api_key
from core.tasks import ajustar_stock_task
from odoo_universal import OdooConnectionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Inventario"], dependencies=[Depends(verify_api_key)])


class AjusteStockRequest(BaseModel):
    """Registro de ajuste de existencias de la base de datos de origen."""
    registro: dict = Field(
        ...,
        description=(
            "Datos del ajuste: ajuste_id, producto_ref, cantidad, "
            "modo (fijar|incrementar|decrementar), ubicacion, motivo."
        ),
    )
    tenant: str = "default"
    procesar_async: bool = Field(False, alias="async")

    model_config = {"populate_by_name": True}


class ConsultaStockRequest(BaseModel):
    """Consulta de existencias de un producto."""
    registro: dict = Field(
        ..., description="Debe traer producto_ref (o producto_id_odoo) y opcionalmente ubicacion."
    )
    tenant: str = "default"


@router.post("/stock/ajustar")
def ajustar(req: AjusteStockRequest):
    """
    Aplica un ajuste de existencias en Odoo (stock.quant + action_apply_inventory).

    Idempotente por `ajuste_id`: reenviar el mismo ajuste no lo aplica dos veces.
    Con `"async": true` encola la tarea y responde con un task_id.
    """
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)

    if req.procesar_async:
        task = ajustar_stock_task.delay(req.registro, req.tenant)
        return {"encolado": True, "task_id": task.id, "tenant": req.tenant}

    try:
        return ajustar_stock(req.registro, odoo)
    except InventarioError as e:
        logger.warning("STOCK_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("STOCK_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")


@router.post("/stock/consultar")
def consultar(req: ConsultaStockRequest):
    """Consulta la existencia actual de un producto en una ubicacion."""
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)
    try:
        return consultar_stock(req.registro, odoo)
    except InventarioError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
