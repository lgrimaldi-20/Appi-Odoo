"""
Tests de las correcciones de la auditoria de seguridad.

Cada test fija un comportamiento que antes era vulnerable, para que una
regresion futura falle aqui en vez de descubrirse en produccion:

  - CRITICA: sin API_KEY el servicio cerraba en abierto (fail-open).
  - ALTA   : la clave se comparaba con != (vulnerable a timing attack).
  - ALTA   : solo /odoo tenia rate limit; el resto permitia fuerza bruta.
  - MEDIA  : las whitelists se congelaban al importar el modulo.
  - MEDIA  : los errores 500 devolvian el texto de la excepcion al cliente.
"""

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.seguridad import verify_api_key


class TestAutenticacionFailClosed:
    """Sin API_KEY el servicio debe CERRAR, no abrir."""

    def test_sin_api_key_rechaza_con_503(self, monkeypatch):
        """
        Antes: API_KEY vacia -> pasaba todo sin validar (fail-open). Un .env no
        montado dejaba la API abierta al mundo.
        """
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("PERMITIR_SIN_API_KEY", raising=False)

        with pytest.raises(HTTPException) as exc:
            verify_api_key(x_api_key=None)
        assert exc.value.status_code == 503
        assert "API_KEY" in exc.value.detail

    def test_sin_api_key_tampoco_pasa_con_clave_inventada(self, monkeypatch):
        """Mandar una clave cualquiera no sortea la falta de configuracion."""
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("PERMITIR_SIN_API_KEY", raising=False)

        with pytest.raises(HTTPException) as exc:
            verify_api_key(x_api_key="lo-que-sea")
        assert exc.value.status_code == 503

    def test_modo_desarrollo_requiere_declararse(self, monkeypatch):
        """Operar sin clave es posible, pero solo como decision explicita."""
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.setenv("PERMITIR_SIN_API_KEY", "true")

        assert verify_api_key(x_api_key=None) is None

    def test_el_modo_desarrollo_no_se_activa_por_error(self, monkeypatch):
        """Solo el valor 'true' abre; cualquier otro mantiene el cierre."""
        monkeypatch.delenv("API_KEY", raising=False)
        for valor in ("1", "si", "yes", "TRUE ", ""):
            monkeypatch.setenv("PERMITIR_SIN_API_KEY", valor)
            if valor.strip().lower() == "true":
                continue
            with pytest.raises(HTTPException) as exc:
                verify_api_key(x_api_key=None)
            assert exc.value.status_code == 503, f"valor {valor!r} no debe abrir"

    def test_clave_correcta_pasa(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-buena")
        monkeypatch.delenv("PERMITIR_SIN_API_KEY", raising=False)
        assert verify_api_key(x_api_key="clave-buena") is None

    def test_clave_incorrecta_devuelve_401(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "clave-buena")
        monkeypatch.delenv("PERMITIR_SIN_API_KEY", raising=False)

        with pytest.raises(HTTPException) as exc:
            verify_api_key(x_api_key="clave-mala")
        assert exc.value.status_code == 401

    def test_con_clave_configurada_el_modo_dev_no_la_sortea(self, monkeypatch):
        """PERMITIR_SIN_API_KEY no debe saltarse una clave ya configurada."""
        monkeypatch.setenv("API_KEY", "clave-buena")
        monkeypatch.setenv("PERMITIR_SIN_API_KEY", "true")

        with pytest.raises(HTTPException) as exc:
            verify_api_key(x_api_key=None)
        assert exc.value.status_code == 401


class TestComparacionEnTiempoConstante:
    """La clave debe compararse con secrets.compare_digest, no con !=."""

    def test_usa_compare_digest(self, monkeypatch):
        """
        Una comparacion normal termina en el primer byte distinto, y esa
        diferencia de tiempo permite reconstruir el secreto byte a byte.
        """
        monkeypatch.setenv("API_KEY", "clave-buena")
        monkeypatch.delenv("PERMITIR_SIN_API_KEY", raising=False)

        with patch("core.seguridad.secrets.compare_digest",
                   wraps=__import__("secrets").compare_digest) as espia:
            verify_api_key(x_api_key="clave-buena")
        assert espia.called, "la clave debe compararse en tiempo constante"

    def test_el_codigo_no_compara_claves_con_igualdad(self):
        """Guardia de regresion: que no reaparezca el '!=' sobre la clave."""
        import inspect

        import core.seguridad as seguridad

        fuente = inspect.getsource(seguridad.verify_api_key)
        assert "compare_digest" in fuente
        assert "x_api_key != api_key" not in fuente


class TestWhitelistsSeReleen:
    """Restringir la whitelist debe surtir efecto sin reiniciar el proceso."""

    def test_recargar_lee_el_entorno(self, monkeypatch):
        import api as api_module

        monkeypatch.setenv("ALLOWED_MODELS", "res.partner,account.move")
        api_module.recargar_whitelists()
        assert api_module.ALLOWED_MODELS == {"res.partner", "account.move"}

        # Simula el cierre de acceso durante un incidente.
        monkeypatch.setenv("ALLOWED_MODELS", "res.partner")
        api_module.recargar_whitelists()
        assert api_module.ALLOWED_MODELS == {"res.partner"}
        assert "account.move" not in api_module.ALLOWED_MODELS

    def test_whitelist_vacia_significa_sin_restriccion(self, monkeypatch):
        import api as api_module

        monkeypatch.setenv("ALLOWED_MODELS", "")
        api_module.recargar_whitelists()
        assert api_module.ALLOWED_MODELS == set()


class TestLimitesDeTasa:
    """El limite ya no cubre solo /odoo."""

    def test_hay_limite_global_por_defecto(self):
        from core.limites import limiter

        assert limiter._default_limits, "debe haber un limite por defecto"

    def test_los_endpoints_costosos_tienen_limite_propio(self):
        """
        /smartier/ingerir y /poller/ejecutar disparan trabajo contra sistemas
        externos y consumen la cuota de 5 req/s de Smartier.
        """
        import inspect

        import routers.poller as rp
        import routers.smartier as rs

        assert "@limitar(" in inspect.getsource(rs)
        assert "@limitar(" in inspect.getsource(rp)


class TestErroresNoFiltranDetalles:
    """Un 500 no debe devolver el texto de la excepcion al cliente."""

    def test_el_detalle_interno_no_llega_al_cliente(self):
        import inspect

        import api as api_module

        fuente = inspect.getsource(api_module.odoo_proxy)
        assert 'detail=f"Error interno: {e}"' not in fuente
        assert '"Error interno del servicio."' in fuente
