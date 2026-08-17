"""
Tests del panel de observabilidad.

  - unidad: observabilidad.py sobre una DB de control temporal (SQLite), poblada
    con state_store, verificando resumen y listados con filtros.
  - HTTP: el router /panel (HTML sin auth) y /panel/api/* (JSON con API Key),
    parcheando _login antes de importar api.py como en test_routers.py.
"""

import importlib
import os
from unittest.mock import patch

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """State store aislado en una DB SQLite temporal, con datos de ejemplo."""
    db = tmp_path / "control_panel.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")

    import core.state_store as state_store
    importlib.reload(state_store)
    import core.observabilidad as observabilidad
    importlib.reload(observabilidad)

    from core.models_db import EstadoSync
    state_store.init_db()

    # Dos facturas (una OK, una ERROR) y un pago OK.
    state_store.registrar_mapeo("factura", "F-1", model_odoo="account.move",
                                id_odoo=10, estado=EstadoSync.PROCESADO)
    state_store.log("factura", "crear", "OK", "F-1", "id_odoo=10")
    state_store.registrar_mapeo("factura", "F-2", model_odoo="account.move",
                                estado=EstadoSync.ERROR)
    state_store.marcar_estado("factura", "F-2", EstadoSync.ERROR, error="cliente no existe")
    state_store.log("factura", "mapear", "ERROR", "F-2", "cliente no existe")
    state_store.registrar_mapeo("pago", "P-1", model_odoo="account.payment",
                                id_odoo=20, estado=EstadoSync.PROCESADO)
    state_store.log("pago", "crear", "OK", "P-1", "id_odoo=20")

    return observabilidad


class TestResumen:
    def test_totales_por_estado(self, store):
        r = store.resumen()
        assert r["total"] == 3
        assert r["por_estado"]["PROCESADO"] == 2
        assert r["por_estado"]["ERROR"] == 1
        assert r["por_entidad"] == {"factura": 2, "pago": 1}
        assert r["logs"]["total"] == 3
        assert r["logs"]["errores"] == 1


class TestListados:
    def test_lista_todo(self, store):
        d = store.listar_sincronizaciones()
        assert d["total"] == 3
        assert len(d["items"]) == 3

    def test_filtro_por_estado_error(self, store):
        d = store.listar_sincronizaciones(estado="ERROR")
        assert d["total"] == 1
        assert d["items"][0]["id_origen"] == "F-2"
        assert d["items"][0]["error"] == "cliente no existe"

    def test_filtro_por_entidad(self, store):
        d = store.listar_sincronizaciones(entidad="pago")
        assert d["total"] == 1
        assert d["items"][0]["id_odoo"] == 20

    def test_filtro_id_origen_parcial(self, store):
        d = store.listar_sincronizaciones(id_origen="F-")
        assert d["total"] == 2

    def test_logs_filtra_por_resultado(self, store):
        d = store.listar_logs(resultado="ERROR")
        assert d["total"] == 1
        assert d["items"][0]["accion"] == "mapear"

    def test_detalle_incluye_mapeo_y_logs(self, store):
        d = store.detalle_registro("factura", "F-1")
        assert d["mapeo"]["estado"] == "PROCESADO"
        assert len(d["logs"]) == 1
        assert d["logs"][0]["accion"] == "crear"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api  # noqa: F401,E402
    from api import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


class TestRouterPanel:
    def test_panel_html_sin_auth(self, monkeypatch):
        monkeypatch.delenv("API_KEY", raising=False)
        r = client.get("/panel")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Panel de sincronizacion" in r.text
        assert "Poller ahora" in r.text

    def test_api_resumen_sin_key_devuelve_401(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secreta")
        r = client.get("/panel/api/resumen")
        assert r.status_code == 401

    def test_api_resumen_con_key_ok(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secreta")
        r = client.get("/panel/api/resumen", headers={"X-Api-Key": "secreta"})
        assert r.status_code == 200
        assert "por_estado" in r.json()

    def test_api_sincronizaciones_sin_apikey_configurada(self, monkeypatch):
        # Sin API_KEY en el entorno, el acceso pasa (modo desarrollo).
        monkeypatch.delenv("API_KEY", raising=False)
        r = client.get("/panel/api/sincronizaciones")
        assert r.status_code == 200
        assert "items" in r.json()


class TestColaPoller:
    """Vista de la cola del poller (modo pull) en el panel."""

    def test_cola_sin_key_devuelve_401(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "secreta")
        r = client.get("/panel/api/cola")
        assert r.status_code == 401

    def test_cola_sin_modo_pull_avisa_en_vez_de_fallar(self, monkeypatch):
        """
        Sin SOURCE_DATABASE_URL el modo pull esta apagado: debe responder 200
        con habilitado=False, no reventar por falta de conexion.
        """
        monkeypatch.delenv("API_KEY", raising=False)
        import core.poller_source as ps
        monkeypatch.setattr(ps, "SOURCE_DATABASE_URL", "")

        r = client.get("/panel/api/cola")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["habilitado"] is False
        assert cuerpo["filas"] == []

    def test_cola_lista_filas_y_totales(self, tmp_path, monkeypatch):
        """Con una cola real, devuelve las filas y los totales por estado."""
        monkeypatch.delenv("API_KEY", raising=False)

        import importlib

        import core.poller_source as ps
        monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{tmp_path / 'cola.db'}")
        importlib.reload(ps)
        ps.init_source_db()

        with ps.get_source_session() as s:
            s.add(ps.ColaSincronizacion(
                entidad="factura", id_origen="Q-1",
                payload={"factura_id": "Q-1"}, estado="PENDIENTE",
            ))
            s.add(ps.ColaSincronizacion(
                entidad="factura", id_origen="Q-2",
                payload={"factura_id": "Q-2"}, estado="ERROR",
                error_detalle="fallo de prueba",
            ))

        # observabilidad importa poller_source dentro de la funcion, asi que
        # recargar el modulo basta para que vea la cola temporal.
        r = client.get("/panel/api/cola")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["habilitado"] is True
        assert cuerpo["totales"] == {"PENDIENTE": 1, "ERROR": 1}
        assert {f["id_origen"] for f in cuerpo["filas"]} == {"Q-1", "Q-2"}

        # El filtro por estado acota el listado.
        r2 = client.get("/panel/api/cola", params={"estado": "ERROR"})
        filas = r2.json()["filas"]
        assert len(filas) == 1
        assert filas[0]["error_detalle"] == "fallo de prueba"
