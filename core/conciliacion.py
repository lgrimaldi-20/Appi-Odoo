"""
Conciliacion contable factura <-> pago (Fase 4).

En Odoo, un pago y una factura no se relacionan con un simple campo: se deben
CONCILIAR sus apuntes contables (account.move.line). Este modulo cruza la linea
por cobrar de la factura con la linea equivalente del pago.

Estrategia (Opcion B del diseno, la mas robusta via JSON-RPC):
  1. Localiza en la factura (account.move) su linea receivable/payable no conciliada.
  2. Localiza en el pago (account.payment) su apunte equivalente.
  3. Llama a reconcile() sobre ambas account.move.line.

Idempotencia: el cruce se registra en el state store como entidad "conciliacion"
con id_origen = "<factura_id>:<pago_id>", de modo que no se concilia dos veces.

Las cuentas por cobrar/pagar se identifican por account.account.account_type:
  - asset_receivable  (facturas de cliente / cobros)
  - liability_payable (facturas de proveedor / pagos)
"""

from core import state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI

ENTIDAD = "conciliacion"

# Tipos de cuenta conciliables en Odoo.
TIPOS_CONCILIABLES = ("asset_receivable", "liability_payable")


class ConciliacionError(Exception):
    """Fallo al conciliar factura y pago."""
    pass


def _lineas_conciliables(odoo: OdooUniversalAPI, move_id: int) -> list[dict]:
    """
    Devuelve las account.move.line de un asiento (factura o pago) que estan en
    una cuenta por cobrar/pagar y aun no estan conciliadas.
    """
    # parent_state='posted': en Odoo 19 reconcile() rechaza apuntes en borrador,
    # asi que se filtran aqui para dar un error claro ("verifica que esten
    # posteados") en vez de que falle el reconcile mas adelante.
    dominio = [
        ["move_id", "=", move_id],
        ["account_id.account_type", "in", list(TIPOS_CONCILIABLES)],
        ["parent_state", "=", "posted"],
        ["reconciled", "=", False],
    ]
    lineas = odoo.execute(
        "account.move.line",
        "search_read",
        dominio,
        fields=["id", "account_id", "balance"],
    )
    return lineas or []


def conciliar(
    factura_id_odoo: int,
    pago_id_odoo: int,
    odoo: OdooUniversalAPI,
    factura_id_origen: str = "",
    pago_id_origen: str = "",
) -> dict:
    """
    Concilia una factura con un pago en Odoo.

    Parametros:
      factura_id_odoo / pago_id_odoo - IDs en Odoo (account.move de cada uno).
      odoo                           - conector del tenant.
      factura_id_origen/pago_id_origen - IDs de origen, para la clave idempotente
                                         y la auditoria (opcionales).

    Devuelve un dict con las lineas conciliadas. Idempotente: si ya se concilio
    esta pareja, devuelve el resultado previo sin volver a tocar Odoo.
    Lanza ConciliacionError si no encuentra lineas conciliables o Odoo falla.
    """
    clave = f"{factura_id_origen or factura_id_odoo}:{pago_id_origen or pago_id_odoo}"

    # Idempotencia: no re-conciliar. (La conciliacion no tiene un unico id_odoo,
    # asi que se comprueba el estado del mapeo directamente, no ya_procesado.)
    previo = state_store.buscar_mapeo(ENTIDAD, clave)
    if previo is not None and previo.estado == EstadoSync.PROCESADO.value:
        state_store.log(ENTIDAD, "idempotente", "OK", clave, "Ya conciliado")
        return {"conciliado": True, "idempotente": True, "clave": clave}

    state_store.registrar_mapeo(
        ENTIDAD, clave, model_odoo="account.move.line", estado=EstadoSync.PROCESANDO
    )

    # 1 y 2. Lineas conciliables de factura y pago. En un account.payment posteado
    # el apunte contable vive tambien en account.move (move_id del pago).
    try:
        lineas_factura = _lineas_conciliables(odoo, factura_id_odoo)
        pago_move_id = _move_id_de_pago(odoo, pago_id_odoo)
        lineas_pago = _lineas_conciliables(odoo, pago_move_id)
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "buscar_lineas", "ERROR", clave, str(e))
        raise ConciliacionError(f"Error al buscar apuntes: {e}") from e

    if not lineas_factura or not lineas_pago:
        msg = (
            f"Sin apuntes conciliables (factura={len(lineas_factura)}, "
            f"pago={len(lineas_pago)}). Verifica que ambos esten posteados."
        )
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=msg)
        state_store.log(ENTIDAD, "buscar_lineas", "ERROR", clave, msg)
        raise ConciliacionError(msg)

    line_ids = [l["id"] for l in lineas_factura] + [l["id"] for l in lineas_pago]

    # 3. reconcile() sobre el conjunto de apuntes.
    try:
        odoo.execute("account.move.line", "reconcile", line_ids)
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, clave, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "reconcile", "ERROR", clave, str(e))
        raise ConciliacionError(f"Error al conciliar: {e}") from e

    state_store.marcar_estado(ENTIDAD, clave, EstadoSync.PROCESADO)
    state_store.log(ENTIDAD, "reconcile", "OK", clave, f"apuntes={line_ids}")
    return {
        "conciliado": True,
        "idempotente": False,
        "clave": clave,
        "apuntes": line_ids,
    }


def _move_id_de_pago(odoo: OdooUniversalAPI, pago_id_odoo: int) -> int:
    """
    Obtiene el account.move asociado a un account.payment.
    (El apunte contable del pago vive en su move_id.)
    """
    datos = odoo.execute(
        "account.payment", "read", [pago_id_odoo], fields=["move_id"]
    )
    if not datos or not datos[0].get("move_id"):
        raise ConciliacionError(
            f"El pago {pago_id_odoo} no tiene asiento contable (move_id)."
        )
    move_id = datos[0]["move_id"]
    # move_id llega como [id, "nombre"] en Odoo (campo many2one).
    return move_id[0] if isinstance(move_id, (list, tuple)) else move_id
