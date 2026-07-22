"""
Tests de validacion de totales / impuestos (Fase 4).
"""

from unittest.mock import MagicMock

import pytest

from core import impuestos
from core.impuestos import DescuadreError, verificar_total


def _odoo_con_total(total):
    odoo = MagicMock()
    odoo.execute.return_value = [{"amount_total": total}]
    return odoo


class TestVerificarTotal:
    def test_totales_iguales_cuadra(self):
        odoo = _odoo_con_total(121.00)
        res = verificar_total(121.00, 42, odoo)
        assert res["cuadra"] is True
        assert res["diferencia"] == 0.0

    def test_diferencia_dentro_de_tolerancia(self):
        # 1 centimo exacto: dentro de tolerancia por defecto.
        odoo = _odoo_con_total(121.01)
        res = verificar_total(121.00, 42, odoo)
        assert res["cuadra"] is True

    def test_descuadre_supera_tolerancia_lanza_error(self):
        odoo = _odoo_con_total(150.00)
        with pytest.raises(DescuadreError) as exc:
            verificar_total(121.00, 42, odoo)
        assert exc.value.total_odoo == 150.0
        assert exc.value.total_origen == 121.0

    def test_evita_ruido_de_coma_flotante(self):
        # 0.1 + 0.2 = 0.30000000000000004 en float; Decimal(str(...)) lo maneja.
        odoo = _odoo_con_total(0.3)
        res = verificar_total(0.3, 42, odoo)
        assert res["cuadra"] is True

    def test_factura_inexistente_lanza_error(self):
        odoo = MagicMock()
        odoo.execute.return_value = []
        with pytest.raises(DescuadreError):
            verificar_total(100.0, 999, odoo)
