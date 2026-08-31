"""
Tests unitarios e de integracion del middleware API-Odoo.
Ejecutar con: pytest tests/ -v
"""

import pytest
from unittest.mock import MagicMock, patch

# Parchamos OdooUniversalAPI._login antes de importar api.py
# para evitar conexion real con Odoo durante los tests.
with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    import os
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    import api as api_module
    from api import app

from fastapi.testclient import TestClient
import odoo_universal

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_api_key(monkeypatch):
    """
    Por defecto, tests sin API Key.

    La autenticacion es fail-closed: sin API_KEY el servicio responde 503, asi
    que para simplificar los tests se activa el modo de desarrollo explicito
    (PERMITIR_SIN_API_KEY), que es la misma via que usaria un entorno local.
    """
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("PERMITIR_SIN_API_KEY", "true")
    # Las whitelists se releen del entorno en cada peticion, asi que se vacian
    # ahi ademas de en el modulo; si no, el .env real se colaria en los tests.
    monkeypatch.setenv("ALLOWED_MODELS", "")
    monkeypatch.setenv("ALLOWED_METHODS", "")
    monkeypatch.setattr(api_module, "ALLOWED_MODELS", set())
    monkeypatch.setattr(api_module, "ALLOWED_METHODS", set())


@pytest.fixture(autouse=True)
def mock_execute(monkeypatch):
    """Reemplaza odoo.execute con un mock en todos los tests."""
    mock = MagicMock(return_value=[{"id": 1, "name": "Test"}])
    if "default" in odoo_universal._tenants:
        monkeypatch.setattr(odoo_universal._tenants["default"], "execute", mock)
    return mock


# --- /health ---


class TestHealth:
    def test_responde_200_sin_autenticacion(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "odoo_conectado" in data

    def test_muestra_estado_ok_cuando_odoo_conectado(self):
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "ok"
        assert data["odoo_conectado"] is True


# --- /odoo autenticacion ---


class TestAutenticacion:
    def test_rechaza_sin_api_key_cuando_configurada(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 401

    def test_rechaza_api_key_incorrecta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post(
            "/odoo",
            json={"model": "res.partner", "method": "search_read"},
            headers={"X-Api-Key": "clave-incorrecta"},
        )
        assert response.status_code == 401

    def test_acepta_api_key_correcta(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-secreta")
        response = client.post(
            "/odoo",
            json={"model": "res.partner", "method": "search_read"},
            headers={"X-Api-Key": "clave-secreta"},
        )
        assert response.status_code == 200
        assert "result" in response.json()

    def test_permite_sin_api_key_cuando_no_configurada(self):
        """Sin API_KEY en .env, el endpoint es accesible (modo desarrollo)."""
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 200


# --- /odoo whitelist ---


class TestWhitelist:
    def test_modelo_no_permitido(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODELS", "res.partner")
        response = client.post("/odoo", json={
            "model": "hr.employee", "method": "search_read"
        })
        assert response.status_code == 422

    def test_metodo_no_permitido(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_METHODS", "search_read,read")
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "unlink"
        })
        assert response.status_code == 422

    def test_modelo_permitido(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODELS", "res.partner")
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 200

    def test_sin_whitelist_permite_todo(self):
        """Sin ALLOWED_MODELS/METHODS configurados, permite cualquier modelo/metodo."""
        response = client.post("/odoo", json={
            "model": "cualquier.modelo", "method": "cualquier_metodo"
        })
        assert response.status_code == 200


# --- /odoo errores ---


class TestErrores:
    def test_error_de_conexion_devuelve_503(self, monkeypatch):
        if "default" in odoo_universal._tenants:
            monkeypatch.setattr(
                odoo_universal._tenants["default"],
                "execute",
                MagicMock(side_effect=odoo_universal.OdooConnectionError("Sin conexion")),
            )
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 503

    def test_error_de_ejecucion_devuelve_422(self, monkeypatch):
        if "default" in odoo_universal._tenants:
            monkeypatch.setattr(
                odoo_universal._tenants["default"],
                "execute",
                MagicMock(side_effect=odoo_universal.OdooExecutionError("Campo no existe")),
            )
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        assert response.status_code == 422

    def test_tenant_no_configurado_devuelve_400(self):
        response = client.post("/odoo", json={
            "model": "res.partner", "method": "search_read", "tenant": "inexistente"
        })
        assert response.status_code == 400


# --- /odoo funcionalidad ---


class TestFuncionalidad:
    def test_respuesta_exitosa_contiene_result(self):
        response = client.post("/odoo", json={
            "model": "res.partner",
            "method": "search_read",
            "args": [[["customer_rank", ">", 0]]],
            "kwargs": {"fields": ["name", "email"], "limit": 5},
        })
        assert response.status_code == 200
        data = response.json()
        assert "result" in data
        assert isinstance(data["result"], list)

    def test_tenant_default_se_usa_por_defecto(self, mock_execute):
        client.post("/odoo", json={
            "model": "res.partner", "method": "search_read"
        })
        mock_execute.assert_called_once()
