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
    model_odoo: str = "account.move",
    campo_odoo: str = "amount_total",
) -> dict:
    """
    Lee el importe calculado por Odoo y lo compara con el total de origen.

    model_odoo/campo_odoo permiten validar otras entidades: account.move guarda
    el importe en amount_total, pero account.payment NO tiene ese campo — usa
    `amount`. Se parametriza en vez de asumir la factura.

    Devuelve un dict con ambos totales y la diferencia si cuadran.
    Lanza DescuadreError si la diferencia supera la tolerancia.
    """
    esperado = _a_decimal(total_origen)

    datos = odoo.execute(
        model_odoo, "read", [factura_id_odoo], fields=[campo_odoo]
    )
    if not datos:
        raise DescuadreError(esperado, None, None)

    real = _a_decimal(datos[0].get(campo_odoo, 0))
    diferencia = abs(esperado - real)

    if diferencia > tolerancia:
        raise DescuadreError(esperado, real, diferencia)

    return {
        "total_origen": float(esperado),
        "total_odoo": float(real),
        "diferencia": float(diferencia),
        "cuadra": True,
    }
