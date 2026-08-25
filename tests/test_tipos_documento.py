"""
Tipos de documento contable (account.move.move_type).

El riesgo que cubren estos tests: move_type decide el SIGNO del apunte. Antes,
mappings.yaml lo fijaba a out_invoice, asi que una devolucion enviada al
middleware se registraba como una VENTA — el cliente quedaba debiendo el importe
en vez de que se le abonara, y sin ningun error visible.
"""

import pytest

from core.facturacion import ENTIDADES_DOCUMENTO
from core.mapper import cargar_config


class TestMapeoDeTipos:
    """Cada entidad de documento debe declarar su move_type en el YAML."""

    ESPERADO = {
        "factura": "out_invoice",
        "nota_credito": "out_refund",
        "factura_proveedor": "in_invoice",
        "nota_debito": "in_refund",
    }

    @pytest.mark.parametrize("entidad,move_type", ESPERADO.items())
    def test_cada_entidad_declara_su_move_type(self, entidad, move_type):
        conf = cargar_config()
        assert entidad in conf, f"falta la entidad '{entidad}' en mappings.yaml"
        assert conf[entidad]["defaults"]["move_type"] == move_type

    def test_las_cuatro_estan_habilitadas_en_el_endpoint(self):
        assert set(ENTIDADES_DOCUMENTO) == set(self.ESPERADO)

    def test_todas_validan_el_total(self):
        """Un abono mal cuadrado es tan grave como una factura mal cuadrada."""
        conf = cargar_config()
        for entidad in self.ESPERADO:
            assert conf[entidad].get("validar_total") == "total", (
                f"'{entidad}' no valida el total contra Odoo"
            )

    def test_los_documentos_de_compra_resuelven_el_proveedor(self):
        """Una compra se identifica por el NIF del proveedor, no del cliente."""
        conf = cargar_config()
        for entidad in ("factura_proveedor", "nota_debito"):
            assert conf[entidad]["resolver"]["partner_id"]["desde"] == "proveedor_nif"


class TestValidacionDelTipo:
    """
    Un tipo mal escrito debe RECHAZARSE. Si cayera al valor por defecto se
    contabilizaria lo contrario de lo pedido, que es justo el fallo original.
    """

    def _odoo(self):
        from unittest.mock import MagicMock
        return MagicMock()

    def test_tipo_desconocido_se_rechaza(self):
        # Se captura por NOMBRE y no por la clase importada: otros tests hacen
        # importlib.reload(sincronizador), lo que crea una SincronizacionError
        # distinta, y un except sobre la clase original no la reconoceria.
        from core.facturacion import crear_factura

        with pytest.raises(Exception) as exc:
            crear_factura({"factura_id": "X"}, self._odoo(), entidad="nota_credto")
        assert type(exc.value).__name__ == "SincronizacionError"
        assert "no admitido" in str(exc.value)

    def test_no_se_puede_colar_otra_entidad_del_yaml(self):
        """
        /facturas no debe servir como puerta trasera hacia entidades que no son
        documentos: 'pago' existe en el YAML pero tiene su propio endpoint.
        """
        from core.facturacion import crear_factura

        with pytest.raises(Exception) as exc:
            crear_factura({"pago_id": "P"}, self._odoo(), entidad="pago")
        assert type(exc.value).__name__ == "SincronizacionError"

    def test_sin_tipo_sigue_siendo_factura_de_venta(self, monkeypatch):
        """Compatibilidad: quien no declara tipo mantiene el comportamiento previo."""
        import core.facturacion as facturacion

        recibidas = []
        monkeypatch.setattr(
            facturacion, "sincronizar_entidad",
            lambda entidad, reg, odoo: recibidas.append(entidad),
        )
        facturacion.crear_factura({"factura_id": "F-1"}, self._odoo())
        assert recibidas == ["factura"]

    def test_el_tipo_llega_al_orquestador(self, monkeypatch):
        import core.facturacion as facturacion

        recibidas = []
        monkeypatch.setattr(
            facturacion, "sincronizar_entidad",
            lambda entidad, reg, odoo: recibidas.append(entidad),
        )
        facturacion.crear_factura(
            {"factura_id": "NC-1"}, self._odoo(), entidad="nota_credito"
        )
        assert recibidas == ["nota_credito"]
