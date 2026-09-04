"""
Tests del Data Mapper (Fase 2).

Usan un OdooUniversalAPI mockeado (MagicMock) para la resolucion de claves
foraneas, de modo que no requieren conexion real con Odoo. La config de mapeo
se inyecta con un YAML temporal para no depender del mappings.yaml de produccion.
"""

import importlib
import textwrap
from unittest.mock import MagicMock

import pytest


CONFIG_YAML = textwrap.dedent(
    """
    factura:
      model: account.move
      id_origen: factura_id
      defaults:
        move_type: out_invoice
      campos:
        fecha: invoice_date
        referencia: ref
      resolver:
        partner_id:
          desde: cliente_nif
          model: res.partner
          buscar_por: vat
          obligatorio: true
    """
)


@pytest.fixture()
def mapper(tmp_path, monkeypatch):
    """
    Recarga core.mapper apuntando la config a un YAML temporal y limpia la
    cache de lru_cache para que lea el archivo de prueba.
    """
    config_file = tmp_path / "mappings.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")

    import core.mapper as mapper_mod
    importlib.reload(mapper_mod)
    monkeypatch.setattr(mapper_mod, "_MAPPINGS_PATH", str(config_file))
    mapper_mod.cargar_config.cache_clear()
    return mapper_mod


@pytest.fixture()
def odoo_encuentra_partner():
    """Mock de Odoo que siempre encuentra el partner con id=7."""
    odoo = MagicMock()
    odoo.execute.return_value = [{"id": 7}]
    return odoo


@pytest.fixture()
def odoo_no_encuentra():
    """Mock de Odoo que no encuentra nada (search_read devuelve [])."""
    odoo = MagicMock()
    odoo.execute.return_value = []
    return odoo


# --- id_origen_de ---


class TestIdOrigen:
    def test_extrae_id_origen(self, mapper):
        registro = {"factura_id": "F-100", "cliente_nif": "B123"}
        assert mapper.id_origen_de("factura", registro) == "F-100"

    def test_id_origen_ausente_lanza_error(self, mapper):
        with pytest.raises(mapper.MapeoError):
            mapper.id_origen_de("factura", {"cliente_nif": "B123"})


# --- mapear: defaults y renombrado ---


class TestMapeoBasico:
    def test_aplica_defaults_y_renombra(self, mapper, odoo_encuentra_partner):
        registro = {
            "factura_id": "F-100",
            "cliente_nif": "B123",
            "fecha": "2026-01-15",
            "referencia": "PED-9",
        }
        valores = mapper.mapear("factura", registro, odoo_encuentra_partner)
        assert valores["move_type"] == "out_invoice"      # default
        assert valores["invoice_date"] == "2026-01-15"    # renombrado fecha->invoice_date
        assert valores["ref"] == "PED-9"                  # renombrado referencia->ref
        assert valores["partner_id"] == 7                 # FK resuelta
        assert "factura_id" not in valores                # id_origen no va a Odoo

    def test_campos_ausentes_no_se_incluyen(self, mapper, odoo_encuentra_partner):
        registro = {"factura_id": "F-101", "cliente_nif": "B123"}
        valores = mapper.mapear("factura", registro, odoo_encuentra_partner)
        assert "invoice_date" not in valores
        assert "ref" not in valores
        assert valores["move_type"] == "out_invoice"


# --- mapear: resolucion de claves foraneas ---


class TestResolucionFK:
    def test_fk_resuelta_llama_search_read_correcto(self, mapper, odoo_encuentra_partner):
        registro = {"factura_id": "F-102", "cliente_nif": "B999"}
        mapper.mapear("factura", registro, odoo_encuentra_partner)
        # Verifica que busco res.partner por vat=B999
        args, kwargs = odoo_encuentra_partner.execute.call_args
        assert args[0] == "res.partner"
        assert args[1] == "search_read"
        assert args[2] == [["vat", "=", "B999"]]

    def test_fk_obligatoria_no_encontrada_lanza_error(self, mapper, odoo_no_encuentra):
        registro = {"factura_id": "F-103", "cliente_nif": "INEXISTENTE"}
        with pytest.raises(mapper.MapeoError, match="No existe en Odoo"):
            mapper.mapear("factura", registro, odoo_no_encuentra)

    def test_fk_obligatoria_sin_valor_lanza_error(self, mapper, odoo_encuentra_partner):
        registro = {"factura_id": "F-104"}  # sin cliente_nif
        with pytest.raises(mapper.MapeoError, match="obligatorio"):
            mapper.mapear("factura", registro, odoo_encuentra_partner)


# --- entidad desconocida ---


class TestEntidadDesconocida:
    def test_entidad_sin_mapeo_lanza_error(self, mapper, odoo_encuentra_partner):
        with pytest.raises(mapper.MapeoError, match="sin mapeo"):
            mapper.mapear("desconocida", {"x": 1}, odoo_encuentra_partner)


# --- mappings.yaml real ---


class TestMapeoPagoReal:
    """
    Comprueba el mapeo de 'pago' contra el core/mappings.yaml de PRODUCCION,
    no contra el YAML de prueba.

    Por que aparte: el resto del archivo inyecta una config minima a proposito,
    para que un cambio de mapeo no rompa los tests de la mecanica. Pero eso
    dejaba sin cubrir el propio YAML, y ahi es donde estuvo el fallo: sin
    partner_type, Odoo 17 rechaza el pago con "Missing required account on
    accountable line", un error que solo aparece contra un Odoo real.
    """

    def _mapear_pago(self):
        import importlib
        import core.mapper as mapper_mod
        importlib.reload(mapper_mod)
        mapper_mod.cargar_config.cache_clear()

        odoo = MagicMock()
        odoo.execute.return_value = [{"id": 7}]
        registro = {
            "pago_id": "PAG-1",
            "cliente_nif": "J-30333333-3",
            "monto": 100.0,
            "fecha": "2026-09-04",
            "moneda_iso": "VES",
            "diario_codigo": "BCO",
        }
        return mapper_mod.mapear("pago", registro, odoo)

    def test_envia_partner_type_customer(self):
        assert self._mapear_pago()["partner_type"] == "customer"

    def test_envia_payment_type_inbound(self):
        assert self._mapear_pago()["payment_type"] == "inbound"

    def test_mapea_monto_fecha_y_fks(self):
        valores = self._mapear_pago()
        assert valores["amount"] == 100.0
        assert valores["date"] == "2026-09-04"
        # partner, moneda y diario se resuelven al id que devuelve el mock.
        for campo in ("partner_id", "currency_id", "journal_id"):
            assert valores[campo] == 7
