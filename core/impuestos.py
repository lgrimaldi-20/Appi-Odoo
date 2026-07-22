"""
Validacion de impuestos y totales (Fase 4).

El total de la factura en la DB de origen debe coincidir AL CENTIMO con el total
que calcula Odoo (que aplica sus propios impuestos, redondeos y reglas fiscales).
Un descuadre indica un mapeo de impuestos incorrecto y NO debe darse por bueno.

verificar_total() compara ambos totales con una tolerancia configurable (por
defecto 1 centimo, para absorber redondeos de coma flotante) y lanza
DescuadreError si no cuadran.
"""

from decimal import Decimal, InvalidOperation

from odoo_universal import OdooUniversalAPI

# Tolerancia por defecto: 1 centimo.
TOLERANCIA = Decimal("0.01")


class DescuadreError(Exception):
    """El total de origen no coincide con el total calculado por Odoo."""
    def __init__(self, total_origen, total_odoo, diferencia):
        self.total_origen = total_origen
        self.total_odoo = total_odoo
        self.diferencia = diferencia
        super().__init__(
            f"Descuadre de totales: origen={total_origen} vs Odoo={total_odoo} "
            f"(diferencia={diferencia})"
        )


def _a_decimal(valor) -> Decimal:
    """Convierte a Decimal de forma segura (via str para evitar ruido binario)."""
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise DescuadreError(valor, None, None) from e


def verificar_total(
    total_origen,
    factura_id_odoo: int,
    odoo: OdooUniversalAPI,
    tolerancia: Decimal = TOLERANCIA,
) -> dict:
    """
    Lee amount_total de la factura en Odoo y lo compara con el total de origen.

    Devuelve un dict con ambos totales y la diferencia si cuadran.
    Lanza DescuadreError si la diferencia supera la tolerancia.
    """
    esperado = _a_decimal(total_origen)

    datos = odoo.execute(
        "account.move", "read", [factura_id_odoo], fields=["amount_total"]
    )
    if not datos:
        raise DescuadreError(esperado, None, None)

    real = _a_decimal(datos[0].get("amount_total", 0))
    diferencia = abs(esperado - real)

    if diferencia > tolerancia:
        raise DescuadreError(esperado, real, diferencia)

    return {
        "total_origen": float(esperado),
        "total_odoo": float(real),
        "diferencia": float(diferencia),
        "cuadra": True,
    }
