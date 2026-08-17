"""
Tareas Celery (Fase 5): procesamiento en segundo plano con reintentos y rollback.

Politica de reintentos:
  - OdooConnectionError (red/Odoo caido) -> se REINTENTA con backoff exponencial.
  - SincronizacionError / OdooExecutionError (datos) -> NO se reintenta (fallaria
    igual); el estado queda ERROR en el state store.

Tarea compuesta (procesar_venta): factura -> pago -> conciliacion, con ROLLBACK
logico de la factura si el pago o la conciliacion fallan.

Las tareas resuelven el tenant por nombre (no reciben el conector, que no es
serializable) usando get_tenant, poblado al arrancar la app / el worker.
"""

import logging
from dataclasses import asdict

from core.celery_app import celery_app
from core.conciliacion import ConciliacionError, conciliar
from core.facturacion import crear_factura
from core.inventario import ajustar_stock
from core.pagos import crear_pago
from core.poller import procesar_lote
from core.rollback import cancelar_factura
from core.sincronizador import SincronizacionError
from odoo_universal import OdooConnectionError, get_tenant

logger = logging.getLogger("api-odoo")

# Reintentos solo ante fallos de conexion.
_REINTENTOS_MAX = 5
_BACKOFF_BASE = 2  # segundos: 2, 4, 8, 16, 32...


@celery_app.task(
    bind=True,
    autoretry_for=(OdooConnectionError,),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def sincronizar_factura_task(self, registro: dict, tenant: str = "default") -> dict:
    """Crea+postea una factura en background. Reintenta si Odoo esta caido."""
    odoo = get_tenant(tenant)
    resultado = crear_factura(registro, odoo)
    return asdict(resultado)


@celery_app.task(
    bind=True,
    autoretry_for=(OdooConnectionError,),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def sincronizar_pago_task(self, registro: dict, tenant: str = "default") -> dict:
    """Crea+postea un pago en background. Reintenta si Odoo esta caido."""
    odoo = get_tenant(tenant)
    resultado = crear_pago(registro, odoo)
    return asdict(resultado)


@celery_app.task(
    bind=True,
    autoretry_for=(OdooConnectionError,),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def ajustar_stock_task(self, registro: dict, tenant: str = "default") -> dict:
    """Aplica un ajuste de existencias en background. Idempotente por ajuste_id."""
    odoo = get_tenant(tenant)
    return ajustar_stock(registro, odoo)


@celery_app.task(
    bind=True,
    autoretry_for=(OdooConnectionError,),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def poller_task(self, tenant: str = "default", limite: int = 50) -> dict:
    """
    Pasada del poller (modo pull): lee la cola de la DB del cliente y sincroniza
    hacia Odoo. Ejecutada periodicamente por Celery Beat (ver celery_app).

    Reintenta el lote completo si Odoo esta caido (OdooConnectionError); los
    fallos de datos por fila no abortan la pasada (quedan ERROR en la cola).
    """
    resultado = procesar_lote(tenant=tenant, limite=limite)
    return asdict(resultado)


@celery_app.task(bind=True)
def procesar_venta_task(
    self,
    factura: dict,
    pago: dict,
    tenant: str = "default",
) -> dict:
    """
    Flujo completo con ROLLBACK logico: factura -> pago -> conciliacion.

    Si el pago o la conciliacion fallan por un error de DATOS despues de crear la
    factura, se compensa la factura (cancelar_factura) para no dejar un asiento
    contable huerfano. Los fallos de conexion se dejan propagar para que Celery
    reintente el flujo completo (las etapas ya hechas son idempotentes).
    """
    odoo = get_tenant(tenant)

    # 1. Factura (idempotente).
    res_factura = crear_factura(factura, odoo)
    factura_id_odoo = res_factura.id_odoo

    # 2. Pago (idempotente). Rollback de la factura si falla por datos.
    try:
        res_pago = crear_pago(pago, odoo)
    except SincronizacionError as e:
        cancelar_factura(
            factura_id_odoo, odoo,
            id_origen=res_factura.id_origen,
            motivo=f"Fallo al crear el pago: {e}",
        )
        raise

    # 3. Conciliacion. Rollback de la factura si falla por datos.
    try:
        res_conc = conciliar(
            factura_id_odoo, res_pago.id_odoo, odoo,
            factura_id_origen=res_factura.id_origen,
            pago_id_origen=res_pago.id_origen,
        )
    except ConciliacionError as e:
        cancelar_factura(
            factura_id_odoo, odoo,
            id_origen=res_factura.id_origen,
            motivo=f"Fallo al conciliar: {e}",
        )
        raise

    return {
        "factura": asdict(res_factura),
        "pago": asdict(res_pago),
        "conciliacion": res_conc,
    }
