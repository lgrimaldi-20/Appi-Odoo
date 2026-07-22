"""
Tests de conciliacion factura <-> pago (Fase 4).

Aislan el state store con SQLite temporal y mockean Odoo, verificando el cruce
de apuntes, la idempotencia y el manejo de errores sin conexion real.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + conciliacion recargada contra el."""
    db_file = tmp_path / "control_conc.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.conciliacion as conciliacion
    importlib.reload(conciliacion)
    return conciliacion, state_store


def _odoo_con_lineas():
    """
    Mock de Odoo que:
      - read de account.payment -> move_id [90, "PAGO/1"]
      - search_read de account.move.line -> una linea conciliable por asiento
      - reconcile -> True
    """
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "account.payment" and method == "read":
            return [{"move_id": [90, "PAGO/1"]}]
        if model == "account.move.line" and method == "search_read":
            move_id = args[0][0][0][2]  # filtro ["move_id","=",X]
            return [{"id": 1000 + move_id, "account_id": [5, "Clientes"], "balance": 100.0}]
        if model == "account.move.line" and method == "reconcile":
            return True
        return None

    odoo.execute.side_effect = execute
    return odoo


# --- flujo feliz ---


class TestConciliacionExitosa:
    def test_concilia_y_llama_reconcile(self, entorno):
        conciliacion, state_store = entorno
        odoo = _odoo_con_lineas()

        res = conciliacion.conciliar(
            10, 20, odoo, factura_id_origen="F-1", pago_id_origen="P-1"
        )

        assert res["conciliado"] is True
        assert res["idempotente"] is False
        # Debe haber llamado a reconcile con los apuntes de factura y pago.
        metodos = [c.args[1] for c in odoo.execute.call_args_list]
        assert "reconcile" in metodos

        mapa = state_store.buscar_mapeo("conciliacion", "F-1:P-1")
        assert mapa.estado == "PROCESADO"


# --- idempotencia ---


class TestIdempotencia:
    def test_no_reconcilia_dos_veces(self, entorno):
        conciliacion, _ = entorno
        odoo = _odoo_con_lineas()

        conciliacion.conciliar(10, 20, odoo, factura_id_origen="F-2", pago_id_origen="P-2")
        llamadas = odoo.execute.call_count

        res2 = conciliacion.conciliar(10, 20, odoo, factura_id_origen="F-2", pago_id_origen="P-2")
        assert res2["idempotente"] is True
        assert odoo.execute.call_count == llamadas  # no toco Odoo


# --- errores ---


class TestErrores:
    def test_sin_lineas_conciliables_lanza_error(self, entorno):
        conciliacion, state_store = entorno
        odoo = MagicMock()

        def execute(model, method, *args, **kwargs):
            if model == "account.payment" and method == "read":
                return [{"move_id": [90, "PAGO/1"]}]
            if method == "search_read":
                return []  # ninguna linea conciliable
            return None

        odoo.execute.side_effect = execute

        with pytest.raises(conciliacion.ConciliacionError, match="Sin apuntes"):
            conciliacion.conciliar(10, 20, odoo, factura_id_origen="F-3", pago_id_origen="P-3")

        mapa = state_store.buscar_mapeo("conciliacion", "F-3:P-3")
        assert mapa.estado == "ERROR"

    def test_pago_sin_move_id_lanza_error(self, entorno):
        conciliacion, _ = entorno
        odoo = MagicMock()

        def execute(model, method, *args, **kwargs):
            if model == "account.payment" and method == "read":
                return [{"move_id": False}]
            if method == "search_read":
                return [{"id": 1, "account_id": [5, "x"], "balance": 100.0}]
            return None

        odoo.execute.side_effect = execute

        with pytest.raises(conciliacion.ConciliacionError):
            conciliacion.conciliar(10, 20, odoo, factura_id_origen="F-4", pago_id_origen="P-4")
