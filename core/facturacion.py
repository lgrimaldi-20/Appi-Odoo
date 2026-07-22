"""
Sincronizacion de FACTURAS (account.move) - Fase 3.

Wrapper de negocio sobre el orquestador generico. Crea la factura en Odoo como
borrador y la postea (action_post) para generar los asientos contables, con
idempotencia garantizada por el state store.
"""

from core.sincronizador import ResultadoSync, sincronizar_entidad
from odoo_universal import OdooUniversalAPI

ENTIDAD = "factura"


def crear_factura(registro: dict, odoo: OdooUniversalAPI) -> ResultadoSync:
    """
    Sincroniza una factura de origen hacia Odoo (crea + postea).
    Idempotente: reenviar la misma factura devuelve el id_odoo ya asignado.
    """
    return sincronizar_entidad(ENTIDAD, registro, odoo)
