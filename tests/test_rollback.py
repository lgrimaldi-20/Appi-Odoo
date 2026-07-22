"""
Tests del rollback logico / compensacion (Fase 5).
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + rollback recargado contra el."""
    db_file = tmp_path / "control_rb.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.rollback as rollback
    importlib.reload(rollback)
    return rollback, state_store


class TestCancelarFactura:
    def test_cancela_con_draft_y_cancel(self, entorno):
        rollback, state_store = entorno
        odoo = MagicMock()
        odoo.execute.return_value = True

        ok = rollback.cancelar_factura(42, odoo, id_origen="F-1", motivo="pago fallo")

        assert ok is True
        metodos = [c.args[1] for c in odoo.execute.call_args_list]
        assert metodos == ["button_draft", "button_cancel"]

    def test_fallo_al_cancelar_no_relanza_y_loguea(self, entorno):
        rollback, state_store = entorno
        from odoo_universal import OdooExecutionError

        odoo = MagicMock()
        odoo.execute.side_effect = OdooExecutionError("no se puede cancelar")

        # No debe relanzar: devuelve False.
        ok = rollback.cancelar_factura(42, odoo, id_origen="F-2", motivo="x")
        assert ok is False

        # Debe haber un log ERROR de rollback para intervencion manual.
        from core.models_db import SyncLog
        with state_store.get_session() as s:
            logs = s.query(SyncLog).filter_by(id_origen="F-2", accion="rollback").all()
            assert len(logs) == 1
            assert logs[0].resultado == "ERROR"
