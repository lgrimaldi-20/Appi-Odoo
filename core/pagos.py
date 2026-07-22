"""
Sincronizacion de PAGOS (account.payment) - Fase 3.

Wrapper de negocio sobre el orquestador generico. Crea el pago en Odoo y lo
postea (action_post) para asentarlo en la contabilidad, con idempotencia
garantizada por el state store.
"""

from core.sincronizador import ResultadoSync, sincronizar_entidad
from odoo_universal import OdooUniversalAPI

ENTIDAD = "pago"


def crear_pago(registro: dict, odoo: OdooUniversalAPI) -> ResultadoSync:
    """
    Sincroniza un pago de origen hacia Odoo (crea + postea).
    Idempotente: reenviar el mismo pago devuelve el id_odoo ya asignado.
    """
    return sincronizar_entidad(ENTIDAD, registro, odoo)
