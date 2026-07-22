"""
Tests de los endpoints de negocio /facturas y /pagos (Fase 3).

Parchan OdooUniversalAPI._login antes de importar api.py (igual que test_api.py)
para evitar conexion real, y mockean la logica de sincronizacion para probar la
capa HTTP (codigos de estado, forma de la respuesta) de forma aislada.
"""

import os
from unittest.mock import patch

import pytest

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api  # noqa: F401  (monta los routers)
    from api import app

from fastapi.testclient import TestClient

import routers.conciliacion as r_conciliacion
import routers.facturas as r_facturas
import routers.pagos as r_pagos
from core.conciliacion import ConciliacionError
from core.sincronizador import ResultadoSync, SincronizacionError

client = TestClient(app)


@pytest.fixture(autouse=True)
def sin_api_key(monkeypatch):
    """Desactiva la API key para simplificar (modo desarrollo)."""
    monkeypatch.delenv("API_KEY", raising=False)


def _resultado_ok(registro):
    return ResultadoSync(
        id_origen=str(registro.get("fid", "X")),
        id_odoo=555, estado="PROCESADO", idempotente=False,
    )


@pytest.fixture()
def mock_sync_ok(monkeypatch):
    """
    crear_factura/crear_pago devuelven un resultado exitoso.
    Se parchea en el punto de uso (los routers llaman a estas funciones).
    """
    monkeypatch.setattr(r_facturas, "crear_factura", lambda reg, odoo: _resultado_ok(reg))
    monkeypatch.setattr(r_pagos, "crear_pago", lambda reg, odoo: _resultado_ok(reg))


# --- /facturas ---


class TestFacturas:
    def test_factura_exitosa_devuelve_id_odoo(self, mock_sync_ok):
        r = client.post("/facturas", json={"registro": {"fid": "F-1"}})
        assert r.status_code == 200
        data = r.json()
        assert data["id_odoo"] == 555
        assert data["estado"] == "PROCESADO"
        assert data["idempotente"] is False

    def test_factura_error_negocio_devuelve_422(self, monkeypatch):
        def falla(reg, odoo):
            raise SincronizacionError("cliente no existe")
        monkeypatch.setattr(r_facturas, "crear_factura", falla)

        r = client.post("/facturas", json={"registro": {"fid": "F-2"}})
        assert r.status_code == 422
        assert "cliente no existe" in r.json()["detail"]

    def test_factura_tenant_inexistente_devuelve_400(self, mock_sync_ok):
        r = client.post(
            "/facturas",
            json={"registro": {"fid": "F-3"}, "tenant": "no-existe"},
        )
        assert r.status_code == 400

    def test_factura_async_encola(self, monkeypatch):
        """Con async=true responde encolado + task_id (modo eager en tests)."""
        fake_task = type("T", (), {"id": "task-123"})()
        monkeypatch.setattr(
            r_facturas.sincronizar_factura_task, "delay",
            lambda registro, tenant: fake_task,
        )
        r = client.post("/facturas", json={"registro": {"fid": "F-A"}, "async": True})
        assert r.status_code == 200
        data = r.json()
        assert data["encolado"] is True
        assert data["task_id"] == "task-123"


# --- /pagos ---


class TestPagos:
    def test_pago_exitoso_devuelve_id_odoo(self, mock_sync_ok):
        r = client.post("/pagos", json={"registro": {"fid": "P-1"}})
        assert r.status_code == 200
        assert r.json()["id_odoo"] == 555

    def test_pago_error_negocio_devuelve_422(self, monkeypatch):
        def falla(reg, odoo):
            raise SincronizacionError("diario no encontrado")
        monkeypatch.setattr(r_pagos, "crear_pago", falla)

        r = client.post("/pagos", json={"registro": {"fid": "P-2"}})
        assert r.status_code == 422


# --- /conciliar ---


class TestConciliacion:
    def test_conciliacion_exitosa(self, monkeypatch):
        def ok(f_odoo, p_odoo, odoo, factura_id_origen="", pago_id_origen=""):
            return {"conciliado": True, "idempotente": False, "clave": "F-1:P-1"}
        monkeypatch.setattr(r_conciliacion, "conciliar", ok)

        r = client.post("/conciliar", json={"factura_id_odoo": 10, "pago_id_odoo": 20})
        assert r.status_code == 200
        assert r.json()["conciliado"] is True

    def test_conciliacion_error_devuelve_422(self, monkeypatch):
        def falla(f_odoo, p_odoo, odoo, factura_id_origen="", pago_id_origen=""):
            raise ConciliacionError("sin apuntes conciliables")
        monkeypatch.setattr(r_conciliacion, "conciliar", falla)

        r = client.post("/conciliar", json={"factura_id_odoo": 10, "pago_id_odoo": 20})
        assert r.status_code == 422
