"""
Sincronizacion de PAGOS (account.payment) - Fase 3.

Wrapper de negocio sobre el orquestador generico. Crea el pago en Odoo y lo
postea (action_post) para asentarlo en la contabilidad, con idempotencia
garantizada por el state store.
"""

from core.rollback import compensar_descuadre
from core.sincronizador import ResultadoSync, SincronizacionError, sincronizar_entidad
from odoo_universal import OdooUniversalAPI

ENTIDAD = "pago"


def crear_pago(registro: dict, odoo: OdooUniversalAPI) -> ResultadoSync:
    """
    Sincroniza un pago de origen hacia Odoo (crea + postea).
    Idempotente: reenviar el mismo pago devuelve el id_odoo ya asignado.

    Si el importe no cuadra con el que registro Odoo, el pago ya esta posteado:
    se cancela para no dejarlo disponible para conciliar contra una factura.
    """
    try:
        return sincronizar_entidad(ENTIDAD, registro, odoo)
    except SincronizacionError as e:
        extra = compensar_descuadre(
            e, odoo, entidad=ENTIDAD,
            id_origen=str(registro.get("pago_id", "")),
            origen="la peticion",
        )
        if extra:
            raise SincronizacionError(
                str(e) + extra, id_odoo=e.id_odoo, descuadre=True
            ) from e
        raise
