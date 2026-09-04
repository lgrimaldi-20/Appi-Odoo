"""
Tests de la sincronizacion de datos maestros (clientes) Smartier -> Odoo.

Lo que mas se protege aqui es la ACTUALIZACION, que es donde estaban los
fallos: antes se reescribian todos los campos de todos los clientes en cada
pasada, y entre ellos 'comment', un campo de texto libre donde contabilidad
escribe sus propias notas.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def maestros(tmp_path, monkeypatch):
    """Modulo con el state store aislado en una DB SQLite temporal."""
    db = tmp_path / "control_maestros.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")

    import core.state_store as state_store
    importlib.reload(state_store)
    import core.maestros_smartier as mod
    importlib.reload(mod)
    state_store.init_db()
    # La cache de localizacion es global: se limpia para no arrastrarla.
    mod._LOCALIZACION_VE = None
    return mod


def cliente_smartier(**kw):
    """Cliente de Smartier con la forma que devuelve la API real."""
    base = {
        "Id": 6,
        "Nombre": "JUNIOR SUMOZA",
        "RazonSocial": None,
        "Tipo": "Contacto",
        "Estado": "Habilitado",
        "Email": "junior@ejemplo.com",
        "Documento": {"Tipo": 6, "Contenido": "J-30111111-1"},
    }
    base.update(kw)
    return base


class TestTraduccionDeCampos:
    def test_rif_ausente_devuelve_none(self, maestros):
        c = cliente_smartier(Documento={"Contenido": None})
        assert maestros.rif_de(c) is None

    def test_deshabilitado_archiva(self, maestros):
        assert maestros.esta_activo(cliente_smartier(Estado="Deshabilitado")) is False

    def test_un_estado_desconocido_deja_el_contacto_visible(self, maestros):
        # Se comprueba el valor negativo a proposito: un estado nuevo de
        # Smartier no debe archivar contactos en silencio.
        assert maestros.esta_activo(cliente_smartier(Estado="Suspendido")) is True

    def test_tipo_empresa(self, maestros):
        v = maestros.valores_de(cliente_smartier(Tipo="Empresa"))
        assert v["company_type"] == "company"

    def test_no_envia_vat_vacio(self, maestros):
        # Mandar vat="" borraria un RIF cargado a mano en Odoo.
        v = maestros.valores_de(cliente_smartier(Documento={"Contenido": None}))
        assert "vat" not in v

    def test_la_referencia_lleva_el_id_de_smartier(self, maestros):
        assert maestros.valores_de(cliente_smartier())["ref"] == "SMARTIER-6"


class TestDeteccionDeCambios:
    """Sin esto se reescribian todos los campos en cada pasada."""

    def _actual(self, **kw):
        base = {
            "id": 8, "name": "JUNIOR SUMOZA", "email": "junior@ejemplo.com",
            "active": True, "company_type": "person",
            "vat": "J-30111111-1", "ref": "SMARTIER-6",
        }
        base.update(kw)
        return base

    def test_sin_diferencias_no_hay_nada_que_escribir(self, maestros):
        assert maestros.cambios_para(cliente_smartier(), self._actual()) == {}

    def test_detecta_el_nombre_cambiado(self, maestros):
        c = cliente_smartier(Nombre="JUNIOR A. SUMOZA")
        cambios = maestros.cambios_para(c, self._actual())
        assert cambios == {"name": "JUNIOR A. SUMOZA"}

    def test_detecta_la_baja(self, maestros):
        c = cliente_smartier(Estado="Deshabilitado")
        assert maestros.cambios_para(c, self._actual())["active"] is False

    def test_un_char_vacio_en_odoo_no_cuenta_como_cambio(self, maestros):
        # Odoo devuelve False para los char vacios; sin normalizar, False != ""
        # daria un cambio falso en cada pasada.
        c = cliente_smartier(Email=None)
        assert "email" not in maestros.cambios_para(c, self._actual(email=False))

    def test_nunca_actualiza_el_comment(self, maestros):
        # 'comment' es de contabilidad: se rellena al crear como pista, pero
        # una pasada de sincronizacion no debe apropiarse de el.
        c = cliente_smartier(Nombre="OTRO NOMBRE")
        assert "comment" not in maestros.cambios_para(c, self._actual())

    def test_nunca_actualiza_los_campos_de_retencion(self, maestros):
        c = cliente_smartier(Nombre="OTRO NOMBRE")
        cambios = maestros.cambios_para(c, self._actual())
        for campo in ("wh_iva_agent", "wh_iva_rate", "islr_withholding_agent"):
            assert campo not in cambios


class TestCreacion:
    def test_nace_sin_retencion_si_hay_localizacion(self, maestros):
        odoo = MagicMock()
        odoo.execute.return_value = 1      # search_count: localizacion presente
        v = maestros.valores_creacion(cliente_smartier(), odoo)
        assert v["wh_iva_agent"] is False
        assert v["islr_withholding_agent"] is False

    def test_sin_localizacion_no_envia_campos_inexistentes(self, maestros):
        # Enviar campos que no existen haria fallar el create entero.
        odoo = MagicMock()
        odoo.execute.return_value = 0
        v = maestros.valores_creacion(cliente_smartier(), odoo)
        assert "wh_iva_agent" not in v

    def test_sin_rif_deja_aviso_en_el_comment(self, maestros):
        odoo = MagicMock()
        odoo.execute.return_value = 0
        v = maestros.valores_creacion(
            cliente_smartier(Documento={"Contenido": None}), odoo)
        assert "PENDIENTE DE VALIDACION FISCAL" in v["comment"]


class TestSincronizarCliente:
    def test_crea_cuando_no_existe(self, maestros):
        # Cache fijada: _tiene_localizacion_ve no consume una llamada del mock.
        maestros._LOCALIZACION_VE = False
        odoo = MagicMock()
        # search_read (RIF) -> [], search_read (ref) -> [], create -> 55
        odoo.execute.side_effect = [[], [], 55]
        r = maestros.ResultadoMaestros()
        maestros.sincronizar_cliente(cliente_smartier(), odoo, r)
        assert (r.creados, r.actualizados, r.errores) == (1, 0, 0)

    def test_no_escribe_cuando_no_hay_cambios(self, maestros):
        actual = {"id": 8, "name": "JUNIOR SUMOZA", "email": "junior@ejemplo.com",
                  "active": True, "company_type": "person",
                  "vat": "J-30111111-1", "ref": "SMARTIER-6"}
        odoo = MagicMock()
        odoo.execute.side_effect = [[actual]]   # lo encuentra por RIF
        r = maestros.ResultadoMaestros()
        maestros.sincronizar_cliente(cliente_smartier(), odoo, r)
        assert r.sin_cambios == 1
        # Una sola llamada: la busqueda. No hubo write.
        assert odoo.execute.call_count == 1

    def test_un_cliente_rechazado_no_aborta_la_pasada(self, maestros):
        from odoo_universal import OdooExecutionError
        maestros._LOCALIZACION_VE = False
        odoo = MagicMock()
        odoo.execute.side_effect = [[], [], OdooExecutionError("dato invalido")]
        r = maestros.ResultadoMaestros()
        maestros.sincronizar_cliente(cliente_smartier(), odoo, r)
        assert r.errores == 1
        assert r.creados == 0

    def test_un_contacto_archivado_se_encuentra(self, maestros):
        # Sin active_test=False, un contacto dado de baja no se encontraria y
        # la pasada siguiente crearia un DUPLICADO.
        odoo = MagicMock()
        odoo.execute.return_value = []
        maestros.buscar_en_odoo(odoo, cliente_smartier())
        for llamada in odoo.execute.call_args_list:
            assert llamada.kwargs.get("context") == {"active_test": False}


class TestEstadoEnElPanel:
    def test_sin_rif_queda_pendiente_no_procesado(self, maestros):
        import core.state_store as state_store
        maestros._LOCALIZACION_VE = False
        odoo = MagicMock()
        # Sin RIF no se busca por RIF: solo hay busqueda por 'ref' y create.
        odoo.execute.side_effect = [[], 55]
        r = maestros.ResultadoMaestros()
        maestros.sincronizar_cliente(
            cliente_smartier(Documento={"Contenido": None}), odoo, r)
        mapa = state_store.buscar_mapeo("cliente", "6")
        # Esta en Odoo, pero todavia no se le puede facturar: el panel tiene
        # que distinguirlo de un cliente listo.
        assert mapa.estado == "PENDIENTE"

    def test_con_rif_queda_procesado(self, maestros):
        import core.state_store as state_store
        maestros._LOCALIZACION_VE = False
        odoo = MagicMock()
        odoo.execute.side_effect = [[], [], 55]
        r = maestros.ResultadoMaestros()
        maestros.sincronizar_cliente(cliente_smartier(), odoo, r)
        assert state_store.buscar_mapeo("cliente", "6").estado == "PROCESADO"
