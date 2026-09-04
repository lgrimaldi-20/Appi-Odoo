"""
Tareas Celery (Fase 5): procesamiento en segundo plano con reintentos y rollback.

Politica de reintentos:
  - OdooConnectionError (red/Odoo caido) -> se REINTENTA con backoff exponencial.
  - SincronizacionError / OdooExecutionError (datos) -> NO se reintenta (fallaria
    igual); el estado queda ERROR en el state store.

Tarea compuesta (procesar_venta): factura -> pago -> conciliacion, con ROLLBACK
logico de la factura si el pago o la conciliacion fallan.

Las tareas resuelven el tenant por nombre (no reciben el conector, que no es
serializable) con asegurar_tenant, que lo registra si aun no lo estaba.
"""

import logging
from dataclasses import asdict

from core.celery_app import celery_app
from core.conciliacion import ConciliacionError, conciliar
from core.facturacion import crear_factura
from core.inventario import ajustar_stock
from core.pagos import crear_pago
from core.ingesta_smartier import IngestaError, ingerir_notas_entrega
from core.maestros_smartier import MaestrosError, sincronizar_clientes
from core.poller import procesar_lote
from core.rollback import cancelar_factura
from core.sincronizador import SincronizacionError
from core.tenants import asegurar_tenant
from odoo_universal import OdooConnectionError

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
    odoo = asegurar_tenant(tenant)
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
    odoo = asegurar_tenant(tenant)
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
    odoo = asegurar_tenant(tenant)
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


@celery_app.task(
    bind=True,
    autoretry_for=(IngestaError,),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def ingesta_smartier_task(self, limite: int = 200) -> dict:
    """
    Pasada de INGESTA: lee notas de entrega de Smartier y las encola.

    Es la mitad izquierda del flujo (Smartier -> cola); el poller se encarga
    despues de llevar la cola a Odoo. Reintenta con espera creciente si la API
    de Smartier falla (red, 429 agotado o 5xx).
    """
    resultado = ingerir_notas_entrega(limite=limite)
    return asdict(resultado)


@celery_app.task(
    bind=True,
    # Se reintenta ante fallo de RED (Smartier u Odoo caidos). Los rechazos de
    # datos de un cliente concreto no llegan aqui: se capturan por cliente y
    # quedan en ERROR, para no abortar la pasada entera por uno malo.
    autoretry_for=(MaestrosError, OdooConnectionError),
    retry_backoff=_BACKOFF_BASE,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=_REINTENTOS_MAX,
)
def sincronizar_maestros_task(self, tenant: str = "default",
                              limite: int = 200) -> dict:
    """
    Pasada de DATOS MAESTROS: crea o actualiza en Odoo los clientes de Smartier.

    Va ANTES que la ingesta de notas en el calendario de Beat, y esa relacion
    de orden es lo que da sentido a la tarea: una nota de entrega necesita que
    su cliente exista en Odoo para resolver el partner_id. Sin esto, la primera
    nota de un cliente nuevo quedaba en ERROR hasta que alguien se acordaba de
    ejecutar el script a mano.
    """
    odoo = asegurar_tenant(tenant)
    resultado = sincronizar_clientes(odoo, limite=limite)
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
    odoo = asegurar_tenant(tenant)

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
