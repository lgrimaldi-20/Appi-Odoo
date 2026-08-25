"""
Sincronizacion de FACTURAS (account.move) - Fase 3.

Wrapper de negocio sobre el orquestador generico. Crea la factura en Odoo como
borrador y la postea (action_post) para generar los asientos contables, con
idempotencia garantizada por el state store.
"""

from core.mapper import cargar_config
from core.sincronizador import ResultadoSync, SincronizacionError, sincronizar_entidad
from odoo_universal import OdooUniversalAPI

ENTIDAD = "factura"

# Entidades que este endpoint acepta: los cuatro tipos de documento de
# account.move. Se listan de forma explicita (y no se admite cualquier entidad
# del YAML) para que /facturas no pueda usarse como puerta trasera hacia otras
# entidades, p.ej. crear pagos.
ENTIDADES_DOCUMENTO = (
    "factura",              # out_invoice - venta
    "nota_credito",         # out_refund  - devolucion a cliente
    "factura_proveedor",    # in_invoice  - compra
    "nota_debito",          # in_refund   - devolucion a proveedor
)


def crear_factura(
    registro: dict, odoo: OdooUniversalAPI, entidad: str = ENTIDAD
) -> ResultadoSync:
    """
    Sincroniza un documento de origen hacia Odoo (crea + postea).

    `entidad` elige el tipo de documento (ver ENTIDADES_DOCUMENTO). Por defecto
    "factura" (venta), para no romper a quien ya llama sin indicar tipo.

    Idempotente: reenviar el mismo documento devuelve el id_odoo ya asignado.
    """
    if entidad not in ENTIDADES_DOCUMENTO:
        raise SincronizacionError(
            f"Tipo de documento '{entidad}' no admitido. "
            f"Validos: {', '.join(ENTIDADES_DOCUMENTO)}."
        )
    # Se comprueba que la entidad exista en el YAML antes de reservar, para dar
    # un error claro si el mapeo se elimina o se renombra.
    if entidad not in cargar_config():
        raise SincronizacionError(
            f"El tipo '{entidad}' no tiene mapeo definido en mappings.yaml."
        )
    return sincronizar_entidad(entidad, registro, odoo)
