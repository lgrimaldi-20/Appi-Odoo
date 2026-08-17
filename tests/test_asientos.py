"""
Tests de asientos contables (crear/eliminar account.move tipo 'entry').

Aislan el state store con SQLite temporal y mockean Odoo, verificando el cuadre
debe/haber, la resolucion de cuentas por codigo, la idempotencia y la eliminacion
(draft + unlink) sin conexion real.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """state_store con SQLite temporal + asientos recargado contra el."""
    db_file = tmp_path / "control_asientos.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.asientos as asientos
    importlib.reload(asientos)
    return asientos, state_store


def _odoo(estado_move="posted", existe=True):
    """
    Mock de Odoo:
      account.journal search_read -> id 3 (por code)
      account.account search_read -> ids por code
      account.move create -> 999 ; action_post/button_draft/unlink -> OK
      account.move read -> estado indicado (para eliminar)
    """
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "account.journal" and method == "search_read":
            return [{"id": 3}]
        if model == "account.account" and method == "search_read":
            # dominio: args[0] = [["code","in",[...]]]
            cond = args[0][0]  # ["code","in",[...]]
            codigos = cond[2]
            return [{"id": 1000 + i, "code": c} for i, c in enumerate(codigos)]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            return [{"id": 999, "state": estado_move, "name": "MISC/2026/07/0001"}] if existe else []
        return True

    odoo.execute.side_effect = execute
    return odoo


def _registro(**over):
    base = {
        "asiento_id": "A-1",
        "diario_codigo": "MISC",
        "fecha": "2026-07-28",
        "referencia": "Prueba",
        "lineas": [
            {"cuenta_codigo": "1.1.1", "debe": 1000, "haber": 0, "concepto": "Caja"},
            {"cuenta_codigo": "5.3.9", "debe": 0, "haber": 1000, "concepto": "Ingreso"},
        ],
    }
    base.update(over)
    return base


# --- creacion ---

class TestCrear:
    def test_crea_y_postea(self, entorno):
        asientos, _ = entorno
        r = asientos.crear_asiento(_registro(), _odoo())
        assert r["id_odoo"] == 999
        assert r["estado"] == "PROCESADO"
        assert r["posteado"] is True
        assert r["idempotente"] is False

    def test_no_postear_deja_borrador(self, entorno):
        asientos, _ = entorno
        odoo = _odoo()
        r = asientos.crear_asiento(_registro(postear=False), odoo)
        assert r["posteado"] is False
        # action_post no debe haberse llamado
        llamadas = [c.args for c in odoo.execute.call_args_list]
        assert not any(m == "account.move" and me == "action_post" for m, me, *_ in llamadas)

    def test_idempotente(self, entorno):
        asientos, _ = entorno
        odoo = _odoo()
        asientos.crear_asiento(_registro(), odoo)
        r2 = asientos.crear_asiento(_registro(), odoo)
        assert r2["idempotente"] is True
        assert r2["id_odoo"] == 999


# --- validaciones de cuadre ---

class TestCuadre:
    def test_descuadre_rechazado(self, entorno):
        asientos, _ = entorno
        reg = _registro(lineas=[
            {"cuenta_codigo": "1.1.1", "debe": 1000, "haber": 0},
            {"cuenta_codigo": "5.3.9", "debe": 0, "haber": 950},  # no cuadra
        ])
        with pytest.raises(asientos.AsientoError, match="no cuadra"):
            asientos.crear_asiento(reg, _odoo())

    def test_menos_de_dos_lineas(self, entorno):
        asientos, _ = entorno
        reg = _registro(lineas=[{"cuenta_codigo": "1.1.1", "debe": 100, "haber": 0}])
        with pytest.raises(asientos.AsientoError, match="al menos 2 lineas"):
            asientos.crear_asiento(reg, _odoo())

    def test_debe_y_haber_en_misma_linea(self, entorno):
        asientos, _ = entorno
        reg = _registro(lineas=[
            {"cuenta_codigo": "1.1.1", "debe": 100, "haber": 100},
            {"cuenta_codigo": "5.3.9", "debe": 0, "haber": 0},
        ])
        with pytest.raises(asientos.AsientoError):
            asientos.crear_asiento(reg, _odoo())

    def test_cuenta_inexistente(self, entorno):
        asientos, _ = entorno
        odoo = MagicMock()

        def execute(model, method, *args, **kwargs):
            if model == "account.journal" and method == "search_read":
                return [{"id": 3}]
            if model == "account.account" and method == "search_read":
                return []  # ninguna cuenta encontrada
            return True
        odoo.execute.side_effect = execute

        with pytest.raises(asientos.AsientoError, match="no existe la cuenta"):
            asientos.crear_asiento(_registro(), odoo)

    def test_diario_inexistente(self, entorno):
        asientos, _ = entorno
        odoo = MagicMock()
        odoo.execute.side_effect = lambda m, me, *a, **k: [] if m == "account.journal" else True
        with pytest.raises(asientos.AsientoError, match="diario"):
            asientos.crear_asiento(_registro(), odoo)


# --- eliminacion ---

class TestEliminar:
    def test_elimina_posteado_pasa_por_draft(self, entorno):
        asientos, _ = entorno
        odoo = _odoo(estado_move="posted")
        asientos.crear_asiento(_registro(), odoo)
        r = asientos.eliminar_asiento("A-1", odoo)
        assert r["eliminado"] is True
        assert r["id_odoo"] == 999
        llamadas = [(c.args[0], c.args[1]) for c in odoo.execute.call_args_list]
        assert ("account.move", "button_draft") in llamadas
        assert ("account.move", "unlink") in llamadas

    def test_elimina_borrador_no_llama_draft(self, entorno):
        asientos, _ = entorno
        odoo = _odoo(estado_move="draft")
        asientos.crear_asiento(_registro(postear=False), odoo)
        asientos.eliminar_asiento("A-1", odoo)
        llamadas = [(c.args[0], c.args[1]) for c in odoo.execute.call_args_list]
        # No debe forzar draft si ya estaba en borrador
        assert ("account.move", "button_draft") not in llamadas
        assert ("account.move", "unlink") in llamadas

    def test_marca_eliminado_en_state_store(self, entorno):
        asientos, state_store = entorno
        odoo = _odoo()
        asientos.crear_asiento(_registro(), odoo)
        asientos.eliminar_asiento("A-1", odoo)
        m = state_store.buscar_mapeo("asiento", "A-1")
        assert m.estado == "ELIMINADO"

    def test_eliminar_por_id_odoo_directo(self, entorno):
        asientos, _ = entorno
        r = asientos.eliminar_asiento(None, _odoo(), id_odoo=999)
        assert r["eliminado"] is True

    def test_eliminar_inexistente(self, entorno):
        asientos, _ = entorno
        with pytest.raises(asientos.AsientoError, match="No hay un asiento"):
            asientos.eliminar_asiento("NO-EXISTE", _odoo())

    def test_eliminar_sin_identificador(self, entorno):
        asientos, _ = entorno
        with pytest.raises(asientos.AsientoError):
            asientos.eliminar_asiento(None, _odoo())


# ---------------------------------------------------------------------------
# HTTP: router /asientos
# ---------------------------------------------------------------------------

import os  # noqa: E402
from unittest.mock import patch  # noqa: E402

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api  # noqa: F401,E402
    from api import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import routers.asientos as r_asientos  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def sin_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)


class TestRouterAsientos:
    def test_post_crea(self, monkeypatch):
        monkeypatch.setattr(
            r_asientos, "crear_asiento",
            lambda reg, odoo: {"asiento_id": reg["asiento_id"], "id_odoo": 999,
                               "estado": "PROCESADO", "posteado": True, "idempotente": False},
        )
        r = client.post("/asientos", json={"registro": _registro()})
        assert r.status_code == 200
        assert r.json()["id_odoo"] == 999

    def test_post_descuadre_devuelve_422(self, monkeypatch):
        # Usa la MISMA clase AsientoError que el router capturara (r_asientos),
        # para que el reload de core.asientos en otros tests no rompa la identidad.
        def falla(reg, odoo):
            raise r_asientos.AsientoError("El asiento no cuadra")
        monkeypatch.setattr(r_asientos, "crear_asiento", falla)
        r = client.post("/asientos", json={"registro": _registro()})
        assert r.status_code == 422
        assert "no cuadra" in r.json()["detail"]

    def test_delete_elimina(self, monkeypatch):
        monkeypatch.setattr(
            r_asientos, "eliminar_asiento",
            lambda aid, odoo, id_odoo=None: {"eliminado": True, "id_odoo": 999, "asiento_id": aid},
        )
        r = client.request("DELETE", "/asientos", json={"asiento_id": "A-1"})
        assert r.status_code == 200
        assert r.json()["eliminado"] is True
