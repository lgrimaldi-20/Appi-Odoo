"""
Tests de la traduccion al castellano de los errores de Odoo.

Lo que se protege aqui es sobre todo la regla de NO tocar lo desconocido: un
mensaje sin traduccion debe salir intacto. Si la funcion empezara a recortar o
adivinar, el panel mostraria menos informacion de la que llego, y el error de
verdad se perderia.
"""

import pytest

from core.traduccion_errores import traducir


class TestTraduce:
    def test_cuenta_contable_faltante(self):
        # El original no menciona al contacto, que es donde suele estar el
        # problema; la traduccion tiene que decirlo.
        r = traducir(
            "The operation cannot be completed: "
            "Missing required account on accountable line."
        )
        assert "cuenta" in r.lower()
        assert "por cobrar" in r.lower()

    def test_conserva_el_original_entre_corchetes(self):
        # Sin el texto de Odoo no se podria buscar el error ni reportarlo.
        r = traducir("Missing required account on accountable line.")
        assert "[Odoo:" in r

    def test_cobros_pendientes(self):
        r = traducir(
            "You cannot create a new payment without an outstanding "
            "payments/receipts account set"
        )
        assert "pendientes" in r.lower()

    def test_es_insensible_a_mayusculas(self):
        assert traducir("ACCESS DENIED") != "ACCESS DENIED"

    def test_encuentra_el_patron_dentro_de_un_texto_largo(self):
        r = traducir("Error in module x: access denied for user 42")
        assert "denegado" in r.lower()


class TestNoRompe:
    def test_mensaje_desconocido_sale_intacto(self):
        # Preferimos ingles legible a una traduccion aproximada que despiste.
        original = "Some brand new Odoo error nobody mapped yet"
        assert traducir(original) == original

    def test_mensaje_ya_en_castellano_no_se_toca(self):
        original = "No existe en Odoo un 'res.partner' con vat='J-1'"
        assert traducir(original) == original

    @pytest.mark.parametrize("valor", ["", None])
    def test_vacio_o_none(self, valor):
        assert traducir(valor) == valor

    def test_no_lanza_con_un_objeto_raro(self):
        # Se llama al registrar un error: si fallara, taparia el error real.
        class Raro:
            def __str__(self):
                return "boom access denied"

        assert "denegado" in traducir(Raro()).lower()


class TestPrioridadDePatrones:
    """
    Un patron especifico tiene que ganar al generico que lo contiene. Se
    comprueba porque el orden de la tabla es lo unico que lo garantiza, y es
    facil de romper al anadir una entrada nueva.
    """

    def test_precio_usd_gana_al_generico(self):
        r = traducir(
            "Valla - The 'USD List Price' field cannot be less than or equal to 0"
        )
        assert "dolares" in r.lower()
        # El generico habria mandado a revisar el importe de la factura, que no
        # es donde esta el problema.
        assert "el valor debe ser mayor" not in r.lower()

    def test_el_generico_sigue_disponible(self):
        r = traducir("Quantity cannot be less than or equal to 0")
        assert "mayor que cero" in r.lower()
