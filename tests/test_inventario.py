"""
Tests del ajuste de existencias (stock.quant).

Aislan el state store con SQLite temporal y mockean Odoo, verificando los modos
de ajuste, la idempotencia y las validaciones sin conexion real.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + inventario recargado contra el."""
    db_file = tmp_path / "control_stock.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.inventario as inventario
    importlib.reload(inventario)
    return inventario, state_store


def _odoo(cantidad_actual=10.0, quant_existe=True):
    """
    Mock de Odoo:
      product.product search_read -> id 100
      stock.location search_read  -> id 8
      stock.quant search_read     -> quant 500 con la cantidad indicada
      write / create / action_apply_inventory -> OK
    """
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "product.product" and method == "search_read":
            return [{"id": 100}]
        if model == "stock.location" and method == "search_read":
            return [{"id": 8}]
        if model == "stock.quant" and method == "search_read":
            return [{"id": 500, "quantity": cantidad_actual}] if quant_existe else []
        if model == "stock.quant" and method == "create":
            return 501
        return True

    odoo.execute.side_effect = execute
    return odoo


# --- modos de ajuste ---


class TestModos:
    def test_fijar_establece_cantidad_exacta(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=10.0)

        res = inventario.ajustar_stock(
            {"ajuste_id": "A-1", "producto_ref": "ABC", "cantidad": 25, "modo": "fijar"},
            odoo,
        )
        assert res["cantidad_anterior"] == 10.0
        assert res["cantidad_final"] == 25.0

        # Debe escribir inventory_quantity=25 y aplicar el ajuste.
        escrituras = [c for c in odoo.execute.call_args_list if c.args[1] == "write"]
        assert escrituras[0].args[3]["inventory_quantity"] == 25.0
        metodos = [c.args[1] for c in odoo.execute.call_args_list]
        assert "action_apply_inventory" in metodos

    def test_incrementar_suma_a_lo_existente(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=10.0)
        res = inventario.ajustar_stock(
            {"ajuste_id": "A-2", "producto_ref": "ABC", "cantidad": 5, "modo": "incrementar"},
            odoo,
        )
        assert res["cantidad_final"] == 15.0

    def test_decrementar_resta(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=10.0)
        res = inventario.ajustar_stock(
            {"ajuste_id": "A-3", "producto_ref": "ABC", "cantidad": 4, "modo": "decrementar"},
            odoo,
        )
        assert res["cantidad_final"] == 6.0

    def test_modo_por_defecto_es_fijar(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=10.0)
        res = inventario.ajustar_stock(
            {"ajuste_id": "A-4", "producto_ref": "ABC", "cantidad": 7}, odoo
        )
        assert res["modo"] == "fijar"
        assert res["cantidad_final"] == 7.0

    def test_producto_sin_quant_previo_se_crea(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(quant_existe=False)
        res = inventario.ajustar_stock(
            {"ajuste_id": "A-5", "producto_ref": "ABC", "cantidad": 3}, odoo
        )
        assert res["cantidad_anterior"] == 0.0
        assert res["cantidad_final"] == 3.0
        assert res["quant_id"] == 501


# --- idempotencia (critica: un incremento repetido descuadraria el stock) ---


class TestIdempotencia:
    def test_mismo_ajuste_no_se_aplica_dos_veces(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=10.0)
        registro = {
            "ajuste_id": "A-10", "producto_ref": "ABC",
            "cantidad": 5, "modo": "incrementar",
        }

        inventario.ajustar_stock(registro, odoo)
        llamadas = odoo.execute.call_count

        res2 = inventario.ajustar_stock(registro, odoo)
        assert res2["idempotente"] is True
        assert odoo.execute.call_count == llamadas  # no toco Odoo de nuevo


# --- validaciones y errores ---


class TestValidaciones:
    def test_sin_ajuste_id_lanza_error(self, entorno):
        inventario, _ = entorno
        with pytest.raises(inventario.InventarioError, match="ajuste_id"):
            inventario.ajustar_stock({"producto_ref": "ABC", "cantidad": 1}, _odoo())

    def test_modo_invalido_lanza_error(self, entorno):
        inventario, _ = entorno
        with pytest.raises(inventario.InventarioError, match="invalido"):
            inventario.ajustar_stock(
                {"ajuste_id": "A-20", "producto_ref": "ABC", "cantidad": 1, "modo": "raro"},
                _odoo(),
            )

    def test_cantidad_negativa_lanza_error(self, entorno):
        inventario, _ = entorno
        with pytest.raises(inventario.InventarioError, match="negativa"):
            inventario.ajustar_stock(
                {"ajuste_id": "A-21", "producto_ref": "ABC", "cantidad": -5}, _odoo()
            )

    def test_producto_inexistente_lanza_error(self, entorno):
        inventario, state_store = entorno
        odoo = MagicMock()
        odoo.execute.return_value = []  # no encuentra el producto

        with pytest.raises(inventario.InventarioError, match="No existe en Odoo"):
            inventario.ajustar_stock(
                {"ajuste_id": "A-22", "producto_ref": "NO-EXISTE", "cantidad": 1}, odoo
            )

        mapa = state_store.buscar_mapeo("ajuste_stock", "A-22")
        assert mapa.estado == "ERROR"

    def test_decrementar_por_debajo_de_cero_lanza_error(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=3.0)
        with pytest.raises(inventario.InventarioError, match="insuficiente"):
            inventario.ajustar_stock(
                {"ajuste_id": "A-23", "producto_ref": "ABC",
                 "cantidad": 10, "modo": "decrementar"},
                odoo,
            )


# --- consulta ---


class TestConsulta:
    def test_consultar_devuelve_cantidad(self, entorno):
        inventario, _ = entorno
        odoo = _odoo(cantidad_actual=42.0)
        res = inventario.consultar_stock({"producto_ref": "ABC"}, odoo)
        assert res["cantidad"] == 42.0
        assert res["producto_id_odoo"] == 100
