"""
Ajuste de existencias en Odoo (stock.quant).

Permite fijar, incrementar o decrementar la cantidad disponible de un producto
en una ubicacion. NO escribe la cantidad "a mano": usa el mecanismo oficial de
Odoo de ajuste de inventario, que deja rastro contable y trazable:

  1. Localiza (o crea) el stock.quant del par (producto, ubicacion).
  2. Escribe inventory_quantity = cantidad contada.
  3. Llama a action_apply_inventory() -> Odoo genera el stock.move del ajuste.

Modos de operacion (campo "modo" del registro de origen):
  - fijar      : la cantidad final sera exactamente "cantidad" (conteo real).
  - incrementar: suma "cantidad" a la existencia actual (entrada).
  - decrementar: resta "cantidad" a la existencia actual (salida).

Idempotencia: cada ajuste se registra en el state store con entidad "ajuste_stock"
e id_origen propio (campo "ajuste_id"), de modo que reenviar el mismo ajuste no
lo aplica dos veces. Esto es critico: un ajuste incremental repetido descuadraria
el inventario.
"""

import logging

from core import state_store
from core.models_db import EstadoSync
from odoo_universal import OdooExecutionError, OdooUniversalAPI

logger = logging.getLogger("api-odoo")

ENTIDAD = "ajuste_stock"

MODOS = ("fijar", "incrementar", "decrementar")


class InventarioError(Exception):
    """Fallo al ajustar existencias (producto/ubicacion no hallados, datos o Odoo)."""
    pass


def _resolver_producto(odoo: OdooUniversalAPI, registro: dict) -> int:
    """
    Localiza el product.product por referencia interna (default_code) o por id.
    La referencia es lo habitual en una DB de origen; el id se acepta si viene.
    """
    if registro.get("producto_id_odoo"):
        return int(registro["producto_id_odoo"])

    referencia = registro.get("producto_ref")
    if not referencia:
        raise InventarioError(
            "Falta 'producto_ref' (referencia interna) o 'producto_id_odoo'."
        )

    encontrados = odoo.execute(
        "product.product",
        "search_read",
        [["default_code", "=", referencia]],
        fields=["id"],
        limit=1,
    )
    if not encontrados:
        raise InventarioError(
            f"No existe en Odoo un product.product con default_code='{referencia}'."
        )
    return encontrados[0]["id"]


def _resolver_ubicacion(odoo: OdooUniversalAPI, registro: dict) -> int:
    """
    Localiza la ubicacion de stock. Si no se indica, usa la primera ubicacion
    interna disponible (el almacen por defecto de la compania).
    """
    if registro.get("ubicacion_id_odoo"):
        return int(registro["ubicacion_id_odoo"])

    nombre = registro.get("ubicacion")
    if nombre:
        encontradas = odoo.execute(
            "stock.location",
            "search_read",
            [["complete_name", "=", nombre], ["usage", "=", "internal"]],
            fields=["id"],
            limit=1,
        )
        if not encontradas:
            raise InventarioError(f"No existe la ubicacion interna '{nombre}'.")
        return encontradas[0]["id"]

    # Sin ubicacion explicita: primera interna (almacen por defecto).
    internas = odoo.execute(
        "stock.location",
        "search_read",
        [["usage", "=", "internal"]],
        fields=["id"],
        limit=1,
    )
    if not internas:
        raise InventarioError("No hay ninguna ubicacion interna configurada en Odoo.")
    return internas[0]["id"]


def _cantidad_actual(odoo: OdooUniversalAPI, producto_id: int, ubicacion_id: int):
    """
    Devuelve (quant_id, cantidad_actual). quant_id es None si aun no existe
    un quant para ese par producto/ubicacion (existencia cero).
    """
    quants = odoo.execute(
        "stock.quant",
        "search_read",
        [["product_id", "=", producto_id], ["location_id", "=", ubicacion_id]],
        fields=["id", "quantity"],
        limit=1,
    )
    if not quants:
        return None, 0.0
    return quants[0]["id"], float(quants[0].get("quantity") or 0.0)


def ajustar_stock(registro: dict, odoo: OdooUniversalAPI) -> dict:
    """
    Aplica un ajuste de existencias segun el registro de origen.

    Campos del registro:
      ajuste_id        (obligatorio) identificador unico del ajuste en origen.
      producto_ref     referencia interna del producto (o producto_id_odoo).
      cantidad         (obligatorio) numero de unidades.
      modo             fijar | incrementar | decrementar (por defecto: fijar).
      ubicacion        nombre completo de la ubicacion (opcional).
      motivo           texto libre para la auditoria (opcional).

    Devuelve un dict con el resultado. Idempotente por ajuste_id.
    Lanza InventarioError ante datos invalidos o fallo de Odoo.
    """
    ajuste_id = registro.get("ajuste_id")
    if not ajuste_id:
        raise InventarioError("Falta 'ajuste_id' (identificador unico del ajuste).")
    ajuste_id = str(ajuste_id)

    modo = (registro.get("modo") or "fijar").lower()
    if modo not in MODOS:
        raise InventarioError(f"Modo '{modo}' invalido. Use uno de: {list(MODOS)}.")

    if registro.get("cantidad") is None:
        raise InventarioError("Falta 'cantidad'.")
    try:
        cantidad = float(registro["cantidad"])
    except (TypeError, ValueError) as e:
        raise InventarioError(f"'cantidad' no es numerica: {registro['cantidad']}") from e

    if cantidad < 0:
        raise InventarioError("'cantidad' no puede ser negativa; use el campo 'modo'.")

    # Idempotencia: un ajuste ya aplicado no se repite.
    previo = state_store.buscar_mapeo(ENTIDAD, ajuste_id)
    if previo is not None and previo.estado == EstadoSync.PROCESADO.value:
        state_store.log(ENTIDAD, "idempotente", "OK", ajuste_id, "Ajuste ya aplicado")
        return {
            "ajuste_id": ajuste_id,
            "aplicado": True,
            "idempotente": True,
            "quant_id": previo.id_odoo,
        }

    state_store.registrar_mapeo(
        ENTIDAD, ajuste_id,
        model_odoo="stock.quant",
        estado=EstadoSync.PROCESANDO,
        hash_payload=state_store.calcular_hash(registro),
    )

    try:
        producto_id = _resolver_producto(odoo, registro)
        ubicacion_id = _resolver_ubicacion(odoo, registro)
        quant_id, actual = _cantidad_actual(odoo, producto_id, ubicacion_id)

        # Cantidad final segun el modo.
        if modo == "fijar":
            final = cantidad
        elif modo == "incrementar":
            final = actual + cantidad
        else:  # decrementar
            final = actual - cantidad
            if final < 0:
                raise InventarioError(
                    f"Stock insuficiente: actual={actual}, se intenta retirar {cantidad}."
                )

        # Escribe la cantidad contada y aplica el ajuste (genera el movimiento).
        if quant_id is None:
            quant_id = odoo.execute(
                "stock.quant",
                "create",
                {
                    "product_id": producto_id,
                    "location_id": ubicacion_id,
                    "inventory_quantity": final,
                },
            )
        else:
            odoo.execute(
                "stock.quant", "write", [quant_id], {"inventory_quantity": final}
            )

        odoo.execute("stock.quant", "action_apply_inventory", [quant_id])

    except InventarioError as e:
        state_store.marcar_estado(ENTIDAD, ajuste_id, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "ajustar", "ERROR", ajuste_id, str(e))
        raise
    except OdooExecutionError as e:
        state_store.marcar_estado(ENTIDAD, ajuste_id, EstadoSync.ERROR, error=str(e))
        state_store.log(ENTIDAD, "ajustar", "ERROR", ajuste_id, str(e))
        raise InventarioError(f"Error de Odoo al ajustar stock: {e}") from e

    state_store.registrar_mapeo(
        ENTIDAD, ajuste_id,
        model_odoo="stock.quant", id_odoo=quant_id,
        estado=EstadoSync.PROCESADO,
    )
    state_store.log(
        ENTIDAD, "ajustar", "OK", ajuste_id,
        f"producto={producto_id} ubicacion={ubicacion_id} {actual} -> {final} "
        f"(modo={modo}). Motivo: {registro.get('motivo', '')}",
    )
    logger.info(
        "STOCK_AJUSTE | ajuste=%s producto=%s %s->%s modo=%s",
        ajuste_id, producto_id, actual, final, modo,
    )

    return {
        "ajuste_id": ajuste_id,
        "aplicado": True,
        "idempotente": False,
        "quant_id": quant_id,
        "producto_id_odoo": producto_id,
        "ubicacion_id_odoo": ubicacion_id,
        "cantidad_anterior": actual,
        "cantidad_final": final,
        "modo": modo,
    }


def consultar_stock(registro: dict, odoo: OdooUniversalAPI) -> dict:
    """
    Consulta la existencia actual de un producto en una ubicacion.
    No modifica nada (util para verificar antes/despues de un ajuste).
    """
    producto_id = _resolver_producto(odoo, registro)
    ubicacion_id = _resolver_ubicacion(odoo, registro)
    quant_id, actual = _cantidad_actual(odoo, producto_id, ubicacion_id)
    return {
        "producto_id_odoo": producto_id,
        "ubicacion_id_odoo": ubicacion_id,
        "quant_id": quant_id,
        "cantidad": actual,
    }
