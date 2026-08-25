"""
Router de FACTURAS: POST /facturas.

Recibe un registro de factura de la DB de origen, lo sincroniza con Odoo
(crea account.move + action_post) de forma idempotente y devuelve el id_odoo.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.facturacion import crear_factura
from core.seguridad import resolver_tenant, verify_api_key
from core.state_store import ReservaOcupada
from core.sincronizador import SincronizacionError
from core.tasks import sincronizar_factura_task
from odoo_universal import OdooConnectionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Facturacion"], dependencies=[Depends(verify_api_key)])


class FacturaRequest(BaseModel):
    """Registro de documento (factura, abono...) de la base de datos de origen."""
    registro: dict = Field(..., description="Datos crudos del documento de origen.")
    # Tipo de documento. Decide el move_type en Odoo y, con el, el SIGNO del
    # apunte contable: una devolucion enviada como "factura" dejaria al cliente
    # debiendo el importe en vez de abonarselo.
    tipo: str = Field(
        "factura",
        description=(
            "Tipo de documento: factura (venta) | nota_credito (devolucion a "
            "cliente) | factura_proveedor (compra) | nota_debito (devolucion a "
            "proveedor). Por defecto, factura."
        ),
    )
    tenant: str = "default"
    # Si es True, encola en background y devuelve 202 en vez de procesar inline.
    procesar_async: bool = Field(False, alias="async")

    model_config = {"populate_by_name": True}


@router.post("/facturas")
def sincronizar_factura(req: FacturaRequest):
    """
    Sincroniza una factura hacia Odoo (crea borrador + postea).

    Idempotente: si la factura ya fue procesada, devuelve el id_odoo existente
    con `idempotente: true` y no vuelve a tocar Odoo.

    Con `"async": true` encola la tarea y responde 202 con un task_id; el
    resultado se consulta luego en GET /estado/factura/{id_origen}.
    """
    # Valida que el tenant exista antes de encolar (falla rapido con 400).
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)

    if req.procesar_async:
        task = sincronizar_factura_task.delay(req.registro, req.tenant, req.tipo)
        return {"encolado": True, "task_id": task.id, "tenant": req.tenant}

    try:
        resultado = crear_factura(req.registro, odoo, entidad=req.tipo)
    except ReservaOcupada as e:
        # Otra peticion con el mismo id_origen esta procesando ahora mismo.
        # 409 (y no 500): el cliente puede reintentar en unos segundos.
        logger.info("RESERVA_OCUPADA | %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except SincronizacionError as e:
        logger.warning("FACTURA_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("FACTURA_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")

    return {
        "id_origen": resultado.id_origen,
        "id_odoo": resultado.id_odoo,
        "estado": resultado.estado,
        "idempotente": resultado.idempotente,
    }
