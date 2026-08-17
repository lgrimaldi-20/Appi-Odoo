"""
Tests de compatibilidad con Odoo 19.

Cubren los puntos que cambiaron respecto a versiones anteriores y que este
middleware tuvo que adaptar:

  - common.login fue ELIMINADO en Odoo 19 -> se usa common.authenticate.
  - common.version se consulta al arrancar para conocer la version del servidor.
  - stock.quant exige inventory_quantity_set=True para aplicar un conteo.
  - account.account.code es company-dependent -> se resuelve cuenta a cuenta.
  - reconcile() rechaza apuntes en borrador -> se filtra parent_state='posted'.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest


def _respuesta(resultado):
    """Respuesta HTTP simulada de un JSON-RPC correcto."""
    r = MagicMock()
    r.json.return_value = {"result": resultado}
    r.raise_for_status.return_value = None
    return r


class TestAutenticacion:
    """El conector debe hablar el protocolo de Odoo 19."""

    def test_usa_authenticate_y_no_el_login_obsoleto(self):
        """Odoo 19 elimino common.login: debe llamarse a common.authenticate."""
        from odoo_universal import OdooUniversalAPI

        llamadas = []

        def fake_post(url, json=None, timeout=None):
            llamadas.append(json["params"])
            if json["params"]["method"] == "version":
                return _respuesta({"server_serie": "19.0"})
            return _respuesta(7)

        with patch("odoo_universal.requests.post", side_effect=fake_post):
            api = OdooUniversalAPI("http://odoo", "db", "user", "pass")

        metodos = [p["method"] for p in llamadas]
        assert "authenticate" in metodos
        assert "login" not in metodos, "common.login ya no existe en Odoo 19"
        assert api.uid == 7

        # authenticate exige un 4o argumento (entorno/user agent).
        args_auth = next(p["args"] for p in llamadas if p["method"] == "authenticate")
        assert len(args_auth) == 4
        assert args_auth[3] == {}

    def test_detecta_la_version_del_servidor(self):
        """common.version se consulta antes del login y expone la version mayor."""
        from odoo_universal import OdooUniversalAPI

        def fake_post(url, json=None, timeout=None):
            if json["params"]["method"] == "version":
                return _respuesta({"server_serie": "19.0"})
            return _respuesta(1)

        with patch("odoo_universal.requests.post", side_effect=fake_post):
            api = OdooUniversalAPI("http://odoo", "db", "user", "pass")

        assert api.version == "19.0"
        assert api.version_mayor == 19

    def test_version_ilegible_no_rompe_el_arranque(self):
        """Si common.version falla, se degrada a 0 y el login sigue adelante."""
        import requests as _requests

        from odoo_universal import OdooUniversalAPI

        def fake_post(url, json=None, timeout=None):
            if json["params"]["method"] == "version":
                raise _requests.RequestException("sin respuesta")
            return _respuesta(1)

        with patch("odoo_universal.requests.post", side_effect=fake_post):
            api = OdooUniversalAPI("http://odoo", "db", "user", "pass")

        assert api.version_info == {}
        assert api.version_mayor == 0
        assert api.uid == 1


class TestInventarioOdoo19:
    """stock.quant: el conteo debe marcarse como introducido."""

    @pytest.fixture()
    def inventario(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
        import core.state_store as state_store
        importlib.reload(state_store)
        state_store.init_db()
        import core.inventario as inventario
        importlib.reload(inventario)
        return inventario

    def _odoo(self, quant_existe=True):
        odoo = MagicMock()

        def execute(model, method, *args, **kwargs):
            if model == "product.product" and method == "search_read":
                return [{"id": 100}]
            if model == "stock.location" and method == "search_read":
                return [{"id": 8}]
            if model == "stock.quant" and method == "search_read":
                return [{"id": 500, "quantity": 10.0}] if quant_existe else []
            if model == "stock.quant" and method == "create":
                return 501
            return True

        odoo.execute.side_effect = execute
        return odoo

    def test_write_marca_inventory_quantity_set(self, inventario):
        """Sin inventory_quantity_set, Odoo 17+ ignora la cantidad contada."""
        odoo = self._odoo(quant_existe=True)
        inventario.ajustar_stock(
            {"ajuste_id": "A1", "producto_ref": "REF", "cantidad": 25, "modo": "fijar"},
            odoo,
        )
        escrituras = [
            c for c in odoo.execute.call_args_list
            if c.args[0] == "stock.quant" and c.args[1] == "write"
        ]
        assert escrituras, "deberia escribirse el quant existente"
        valores = escrituras[0].args[3]
        assert valores["inventory_quantity"] == 25
        assert valores["inventory_quantity_set"] is True

    def test_create_marca_inventory_quantity_set(self, inventario):
        """El quant creado desde cero tambien debe llevar la marca."""
        odoo = self._odoo(quant_existe=False)
        inventario.ajustar_stock(
            {"ajuste_id": "A2", "producto_ref": "REF", "cantidad": 5, "modo": "fijar"},
            odoo,
        )
        creaciones = [
            c for c in odoo.execute.call_args_list
            if c.args[0] == "stock.quant" and c.args[1] == "create"
        ]
        assert creaciones
        assert creaciones[0].args[2]["inventory_quantity_set"] is True


class TestCuentasCompanyDependent:
    """account.account.code es company-dependent desde Odoo 17."""

    @pytest.fixture()
    def asientos(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
        import core.state_store as state_store
        importlib.reload(state_store)
        state_store.init_db()
        import core.asientos as asientos
        importlib.reload(asientos)
        return asientos

    def test_resuelve_cuentas_una_a_una_con_igual(self, asientos):
        """
        No debe usarse un 'in' sobre code (no fiable en company-dependent):
        cada cuenta se busca con '=' para que Odoo la traduzca a la compania.
        """
        odoo = MagicMock()
        codigos_pedidos = []

        def execute(model, method, *args, **kwargs):
            if model == "account.journal" and method == "search_read":
                return [{"id": 3}]
            if model == "account.account" and method == "search_read":
                dominio = args[0]
                operador, valor = dominio[0][1], dominio[0][2]
                assert operador == "=", "code es company-dependent: usar '=' no 'in'"
                codigos_pedidos.append(valor)
                return [{"id": 900 + len(codigos_pedidos), "code": valor}]
            if method == "create":
                return 77
            return True

        odoo.execute.side_effect = execute

        asientos.crear_asiento(
            {
                "asiento_id": "AS1",
                "diario_codigo": "MISC",
                "lineas": [
                    {"cuenta_codigo": "430000", "debe": 100, "haber": 0},
                    {"cuenta_codigo": "700000", "debe": 0, "haber": 100},
                ],
            },
            odoo,
        )
        assert sorted(codigos_pedidos) == ["430000", "700000"]


class TestConciliacionOdoo19:
    """reconcile() no admite apuntes en borrador."""

    @pytest.fixture()
    def conciliacion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
        import core.state_store as state_store
        importlib.reload(state_store)
        state_store.init_db()
        import core.conciliacion as conciliacion
        importlib.reload(conciliacion)
        return conciliacion

    def test_solo_busca_apuntes_posteados(self, conciliacion):
        """El dominio debe restringir parent_state='posted'."""
        odoo = MagicMock()
        dominios = []

        def execute(model, method, *args, **kwargs):
            if model == "account.move.line" and method == "search_read":
                dominios.append(args[0])
                return [{"id": 11, "account_id": [1, "430"], "balance": 100.0}]
            if model == "account.payment" and method == "read":
                return [{"move_id": [55, "PAY/1"]}]
            return True

        odoo.execute.side_effect = execute
        conciliacion.conciliar(10, 20, odoo)

        assert dominios, "deberian buscarse apuntes"
        for dominio in dominios:
            condiciones = {c[0]: c[2] for c in dominio}
            assert condiciones.get("parent_state") == "posted"
