"""
Router de ASIENTOS CONTABLES: crear y eliminar account.move de tipo 'entry'.

  POST   /asientos   -> crea (y por defecto postea) un asiento contable (idempotente)
  DELETE /asientos   -> elimina un asiento (draft + unlink si estaba posteado)

A diferencia de facturas/pagos, el asiento no pasa por el sincronizador generico:
tiene su propio flujo en core/asientos.py (resuelve cuentas por codigo y valida
que el asiento cuadre antes de tocar Odoo).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.asientos import AsientoError, crear_asiento, eliminar_asiento
from core.seguridad import LIMITE_NEGOCIO, limiter, resolver_tenant, verify_api_key
from core.state_store import ReservaOcupada
from odoo_universal import OdooConnectionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

router = APIRouter(tags=["Asientos"], dependencies=[Depends(verify_api_key)])


class AsientoRequest(BaseModel):
    """Registro de asiento contable de la base de datos de origen."""
    registro: dict = Field(
        ...,
        description=(
            "Datos del asiento: asiento_id, diario_codigo, fecha, referencia, "
            "postear (bool), y lineas=[{cuenta_codigo, debe, haber, concepto}]. "
            "La suma de 'debe' debe igualar la de 'haber'."
        ),
    )
    tenant: str = "default"


class EliminarAsientoRequest(BaseModel):
    """Identifica el asiento a eliminar por id de origen o por id de Odoo."""
    asiento_id: str | None = Field(None, description="ID de origen del asiento.")
    id_odoo: int | None = Field(None, description="ID del account.move en Odoo.")
    tenant: str = "default"


@router.post("/asientos")
@limiter.limit(LIMITE_NEGOCIO)
def crear(req: AsientoRequest, request: Request):
    """
    Crea un asiento contable en Odoo (account.move tipo 'entry').

    Por defecto lo postea; con `"postear": false` en el registro lo deja en
    borrador. Idempotente por `asiento_id`: reenviar el mismo no lo crea dos veces.
    """
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)
    try:
        return crear_asiento(req.registro, odoo)
    except ReservaOcupada as e:
        # Otra peticion con el mismo id de origen esta en curso -> 409.
        logger.info("RESERVA_OCUPADA | %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except AsientoError as e:
        logger.warning("ASIENTO_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("ASIENTO_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")


@router.delete("/asientos")
@limiter.limit(LIMITE_NEGOCIO)
def eliminar(req: EliminarAsientoRequest, request: Request):
    """
    Elimina un asiento contable de Odoo. Se identifica por `asiento_id`
    (id de origen) o por `id_odoo`. Si estaba posteado, se pasa a borrador y
    luego se elimina.
    """
    odoo: OdooUniversalAPI = resolver_tenant(req.tenant)
    try:
        return eliminar_asiento(req.asiento_id, odoo, id_odoo=req.id_odoo)
    except AsientoError as e:
        logger.warning("ASIENTO_ELIMINAR_ERROR | %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except OdooConnectionError as e:
        logger.error("ASIENTO_ELIMINAR_CONEXION | %s", e)
        raise HTTPException(status_code=503, detail=f"Error de conexion con Odoo: {e}")
