"""
Tests del acceso del panel (el JS embebido en routers/panel.py).

Aqui no hay navegador, asi que se comprueban dos cosas distintas:

  1. Sobre el HTML servido: que la logica de acceso tenga la forma correcta.
     Son asserts de texto, y su valor esta en fijar EXACTAMENTE los tres
     fallos que se corrigieron, para que no vuelvan al editar el panel.
  2. Sobre la API: que el contrato en el que se apoya ese JS (200 con clave
     buena, 401 con clave mala) se cumple de verdad.

Los tres fallos que se protegen, todos del mismo origen -- entrar() leia la
clave del formulario:
  - Al refrescar (F5), el campo esta vacio: el autologin machacaba con "" la
    clave de sessionStorage y la sesion se caia al login.
  - Como el fallo borraba la clave guardada, la primera clave tecleada se
    perdia y habia que escribirla dos veces.
  - Un 401 posterior llamaba a salir(), que hace location.reload(): con el
    auto-refresco encendido eso recargaba la pagina en bucle cada 5 s.
"""

import os
import re
from unittest.mock import patch

import pytest

with patch("odoo_universal.OdooUniversalAPI._login", return_value=1):
    os.environ.setdefault("ODOO_URL", "https://test-odoo.com")
    os.environ.setdefault("ODOO_DB", "test-db")
    os.environ.setdefault("ODOO_USERNAME", "test-user")
    os.environ.setdefault("ODOO_PASSWORD", "test-pass")

    from api import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module")
def html():
    """HTML del panel (la pagina publica, sin API Key)."""
    r = client.get("/panel")
    assert r.status_code == 200
    return r.text


class TestAutologinNoLeeElFormulario:
    def test_el_autologin_usa_la_clave_guardada(self, html):
        # El fallo original era 'if(APIKEY){ entrar(); }': entrar() leia el
        # campo, vacio tras un refresco.
        assert "acceder(APIKEY, true)" in html

    def test_entrar_no_se_llama_en_el_arranque(self, html):
        assert not re.search(r"if\s*\(\s*APIKEY\s*\)\s*\{\s*entrar\(\)", html)

    def test_acceder_recibe_la_clave_como_parametro(self, html):
        # Si volviera a leer del DOM dentro de acceder(), el refresco se
        # rompe otra vez.
        cuerpo = html.split("function acceder(")[1].split("function entrar(")[0]
        assert 'getElementById("key")' not in cuerpo


class TestClaveTecleadaSeConserva:
    def test_solo_el_autologin_borra_la_clave_guardada(self, html):
        # Una clave recien tecleada que falla no debe borrar nada: el usuario
        # tiene que poder corregirla sin volver a empezar.
        cuerpo = html.split("function acceder(")[1].split("function entrar(")[0]
        assert 'if(esAutologin) sessionStorage.removeItem("apikey")' in cuerpo

    def test_entrar_rechaza_una_clave_vacia(self, html):
        # Sin esto, pulsar Entrar en blanco lanzaba una peticion condenada al
        # 401 y ensuciaba el mensaje de error.
        cuerpo = html.split("function entrar(")[1].split("function mostrarPuerta(")[0]
        assert "if(!clave)" in cuerpo


class TestSesionCaducadaNoRecarga:
    def test_no_se_llama_a_salir_en_el_manejo_del_401(self, html):
        # salir() hace location.reload(): con auto-refresco encendido eso
        # recarga la pagina cada 5 s indefinidamente.
        cuerpo = html.split("async function cargarTodo(")[1].split("const chkAuto")[0]
        assert "salir()" not in cuerpo
        assert "mostrarPuerta(" in cuerpo

    def test_se_detiene_el_auto_refresco(self, html):
        cuerpo = html.split("async function cargarTodo(")[1].split("const chkAuto")[0]
        assert "clearInterval(autoTimer)" in cuerpo


class TestContratoDeLaApi:
    """El JS se apoya en distinguir 401 de 200; se comprueba que asi sea."""

    def test_sin_clave_responde_401(self):
        assert client.get("/panel/api/resumen").status_code == 401

    def test_clave_incorrecta_responde_401(self):
        r = client.get("/panel/api/resumen", headers={"X-Api-Key": "no-es"})
        assert r.status_code == 401

    def test_la_pagina_del_panel_no_pide_clave(self):
        # El shell HTML es publico a proposito: los datos van protegidos.
        assert client.get("/panel").status_code == 200
