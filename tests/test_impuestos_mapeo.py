"""
Resolucion de impuestos por NOMBRE y validacion de importe en pagos.

Dos huecos que cubren estos tests:

  - Los tax_ids son ids INTERNOS de Odoo y cambian entre instancias. Obligar al
    sistema de origen a conocerlos ata la integracion a una base de datos
    concreta: lo que funciona en pruebas falla en produccion.
  - account.payment no validaba importe, asi que un cobro por una cantidad
    equivocada se sincronizaba sin queja y luego conciliaba mal.
"""

from unittest.mock import MagicMock

import pytest

from core import mapper


class TestImpuestosPorNombre:
    def _odoo(self, encontrado=True, id_tax=7):
        odoo = MagicMock()
        odoo.consultas = []

        def execute(model, method, *a, **k):
            if model == "account.tax" and method == "search_read":
                odoo.consultas.append(a[0])          # el dominio usado
                return [{"id": id_tax}] if encontrado else []
            if model == "res.partner":
                return [{"id": 1}]
            return None

        odoo.execute.side_effect = execute
        return odoo

    def test_traduce_el_nombre_al_id_de_odoo(self):
        odoo = self._odoo()
        lineas = mapper._aplicar_impuestos_a_lineas(
            odoo, [[0, 0, {"product_id": 1, "impuestos": ["15%"]}]], "out_invoice"
        )
        assert lineas[0][2]["tax_ids"] == [(6, 0, [7])]
        assert "impuestos" not in lineas[0][2], "el campo de origen debe consumirse"

    def test_una_compra_usa_el_impuesto_de_compra(self):
        """
        El nombre NO es unico: "15%" existe para venta y para compra. Sin filtrar
        por type_tax_use, una factura de proveedor llevaria el IVA repercutido en
        vez del soportado.
        """
        odoo = self._odoo()
        mapper._aplicar_impuestos_a_lineas(
            odoo, [[0, 0, {"impuestos": ["15%"]}]], "in_invoice"
        )
        dominio = odoo.consultas[0]
        assert ["type_tax_use", "=", "purchase"] in dominio

    def test_una_venta_usa_el_impuesto_de_venta(self):
        odoo = self._odoo()
        mapper._aplicar_impuestos_a_lineas(
            odoo, [[0, 0, {"impuestos": ["15%"]}]], "out_refund"
        )
        assert ["type_tax_use", "=", "sale"] in odoo.consultas[0]

    def test_un_impuesto_inexistente_es_error(self):
        """No se puede ignorar: cambiaria el total del documento."""
        odoo = self._odoo(encontrado=False)
        with pytest.raises(mapper.MapeoError) as exc:
            mapper._aplicar_impuestos_a_lineas(
                odoo, [[0, 0, {"impuestos": ["NO-EXISTE"]}]], "out_invoice"
            )
        assert "NO-EXISTE" in str(exc.value)

    def test_respeta_los_tax_ids_ya_resueltos(self):
        """Compatibilidad: quien ya manda ids crudos sigue funcionando."""
        odoo = self._odoo()
        lineas = mapper._aplicar_impuestos_a_lineas(
            odoo, [[0, 0, {"tax_ids": [(6, 0, [99])]}]], "out_invoice"
        )
        assert lineas[0][2]["tax_ids"] == [(6, 0, [99])]
        assert not odoo.consultas, "no debia consultar Odoo"

    def test_no_toca_las_lineas_sin_impuestos(self):
        odoo = self._odoo()
        entrada = [[0, 0, {"product_id": 1, "quantity": 2}]]
        assert mapper._aplicar_impuestos_a_lineas(odoo, entrada, "out_invoice") == entrada

    def test_cachea_la_resolucion(self):
        """Un lote de facturas con el mismo IVA no debe repetir la consulta."""
        mapper.limpiar_cache_fk()
        odoo = self._odoo()
        for _ in range(3):
            mapper._aplicar_impuestos_a_lineas(
                odoo, [[0, 0, {"impuestos": ["15%"]}]], "out_invoice"
            )
        assert len(odoo.consultas) == 1


class TestValidacionDeImporteEnPagos:
    def test_el_pago_declara_su_campo_de_importe(self):
        """
        account.payment NO tiene amount_total (eso es de account.move): si el
        YAML no lo dijera, verificar_total leeria un campo inexistente.
        """
        conf = mapper.cargar_config()["pago"]
        assert conf.get("validar_total") == "monto"
        assert conf.get("campo_total_odoo") == "amount"

    def test_verificar_total_lee_el_campo_indicado(self):
        from core.impuestos import verificar_total

        odoo = MagicMock()
        odoo.execute.return_value = [{"amount": 500.0}]
        res = verificar_total(500.0, 1, odoo,
                              model_odoo="account.payment", campo_odoo="amount")
        assert res["cuadra"] is True
        odoo.execute.assert_called_once()
        assert odoo.execute.call_args.args[0] == "account.payment"

    def test_detecta_un_pago_por_importe_distinto(self):
        from core.impuestos import DescuadreError, verificar_total

        odoo = MagicMock()
        odoo.execute.return_value = [{"amount": 450.0}]
        with pytest.raises(DescuadreError):
            verificar_total(500.0, 1, odoo,
                            model_odoo="account.payment", campo_odoo="amount")
