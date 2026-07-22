"""
Rollback logico / compensacion (Fase 5).

Odoo no ofrece transacciones distribuidas: si la factura se crea y postea pero
un paso posterior (pago o conciliacion) falla, hay que COMPENSAR dejando el
sistema en un estado consistente y auditable.

cancelar_factura() lleva un account.move de vuelta a un estado no contable:
  - button_draft  -> pasa de "posteado" a "borrador"
  - button_cancel -> lo marca como "cancelado"

Toda compensacion se registra en el state store (sync_log) para intervencion
humana. Un fallo al compensar NO se relanza como excepcion dura: se deja un log
de nivel ERROR muy explicito, porque el registro original ya era un error.
"""

import logging

from core import state_store
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")


def cancelar_factura(
    id_odoo: int,
    odoo: OdooUniversalAPI,
    entidad: str = "factura",
    id_origen: str = "",
    motivo: str = "",
) -> bool:
    """
    Compensa una factura ya creada en Odoo (draft + cancel).

    Devuelve True si la compensacion se aplico, False si fallo (en cuyo caso
    queda un log ERROR para intervencion manual). No relanza excepciones.
    """
    ref = id_origen or str(id_odoo)
    try:
        # Primero a borrador (revierte los asientos), luego cancelar.
        odoo.execute("account.move", "button_draft", [id_odoo])
        odoo.execute("account.move", "button_cancel", [id_odoo])
        state_store.log(
            entidad, "rollback", "OK", ref,
            f"Factura id_odoo={id_odoo} cancelada. Motivo: {motivo}",
        )
        logger.info("ROLLBACK_OK | entidad=%s ref=%s id_odoo=%s", entidad, ref, id_odoo)
        return True
    except OdooExecutionError as e:
        # No se pudo compensar: requiere intervencion humana.
        state_store.log(
            entidad, "rollback", "ERROR", ref,
            f"NO se pudo cancelar id_odoo={id_odoo}: {e}. INTERVENCION MANUAL. "
            f"Motivo original: {motivo}",
        )
        logger.error(
            "ROLLBACK_FALLO | entidad=%s ref=%s id_odoo=%s error=%s",
            entidad, ref, id_odoo, e,
        )
        return False
