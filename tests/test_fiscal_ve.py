"""
Tests de la preparacion fiscal venezolana sin la localizacion.

El punto delicado es la CONVERSION del porcentaje de retencion. La retencion de
IVA es un porcentaje DEL IVA, pero un account.tax se aplica siempre sobre la
base imponible, asi que hay que convertirlo antes de guardarlo.

Se comprobo contra la instancia real: creando la retencion con -75 el total de
una factura de 1.000 Bs salia 410 en vez de 1.040, porque Odoo restaba el 75%
de la base (750) en lugar del 75% del IVA (120).
"""

import importlib.util
import os
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def fiscal_mod():
    ruta = os.path.join(RAIZ, "scripts/preparar_fiscal_ve.py")
    spec = importlib.util.spec_from_file_location("_fiscal_ve", ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["_fiscal_ve"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class TestConversionRetencionIVA:
    """Retener el 75% del IVA no es lo mismo que retener el 75% de la base."""

    def _pct(self, mod, fragmento):
        for nombre, pct, _ in mod.RETENCIONES_IVA:
            if fragmento in nombre:
                return pct
        raise AssertionError(f"no encontrada: {fragmento}")

    def test_retencion_75_es_12_por_ciento_de_la_base(self, fiscal_mod):
        # 0,75 x 16% = 12%
        assert self._pct(fiscal_mod, "75%") == pytest.approx(-12.0)

    def test_retencion_100_es_16_por_ciento_de_la_base(self, fiscal_mod):
        # 1,00 x 16% = 16%
        assert self._pct(fiscal_mod, "100%") == pytest.approx(-16.0)

    def test_las_retenciones_son_negativas(self, fiscal_mod):
        """Un importe positivo SUMARIA al total en vez de restar."""
        for _, pct, _ in fiscal_mod.RETENCIONES_IVA + fiscal_mod.RETENCIONES_ISLR:
            assert pct < 0

    def test_el_calculo_cuadra_con_el_ejemplo_real(self, fiscal_mod):
        """
        Factura de 1.000 Bs con IVA 16% y retencion del 75%:
          base 1.000 + IVA 160 - retencion 120 = 1.040
        Verificado contra la instancia.
        """
        base = 1000.0
        iva = base * (fiscal_mod.IVA_VIGENTE / 100)
        ret = base * (self._pct(fiscal_mod, "75%") / 100)

        assert iva == pytest.approx(160.0)
        assert ret == pytest.approx(-120.0)
        assert base + iva + ret == pytest.approx(1040.0)

    def test_la_retencion_100_deja_el_iva_en_cero(self, fiscal_mod):
        base = 1000.0
        iva = base * (fiscal_mod.IVA_VIGENTE / 100)
        ret = base * (self._pct(fiscal_mod, "100%") / 100)

        assert iva + ret == pytest.approx(0.0)
        assert base + iva + ret == pytest.approx(1000.0)

    def test_el_nombre_dice_sobre_que_alicuota_se_calculo(self, fiscal_mod):
        """
        El porcentaje queda atado al IVA del 16%. Si la alicuota cambia hay que
        recalcularlo, asi que el nombre debe dejar constancia.
        """
        for nombre, _, _ in fiscal_mod.RETENCIONES_IVA:
            assert "16%" in nombre


class TestRetencionesISLR:
    """El ISLR SI se calcula sobre la base, no sobre el IVA: sin conversion."""

    def test_los_tramos_son_directos(self, fiscal_mod):
        esperados = {-1.0, -2.0, -3.0, -5.0}
        assert {p for _, p, _ in fiscal_mod.RETENCIONES_ISLR} == esperados


class TestPosicionesFiscales:

    def test_hay_una_por_regimen(self, fiscal_mod):
        assert len(fiscal_mod.POSICIONES) == 3
        nombres = " ".join(n for n, _ in fiscal_mod.POSICIONES)
        assert "Ordinario" in nombres
        assert "75%" in nombres and "100%" in nombres
