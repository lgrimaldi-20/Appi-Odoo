"""
Tests del mapeo de Estado/Tipo de Smartier a los campos de Odoo.

Por que existen: HOY los 3 clientes estan 'Habilitado' y los 55 productos
'Disponible', asi que los datos reales NO ejercitan ninguna de estas ramas. Sin
estos tests, el mapeo se descubriria roto el dia que el cliente deshabilite su
primer producto -- en produccion y sin aviso.

Se cubren tres decisiones deliberadas:

  1. Solo 'Deshabilitado' archiva. Un estado desconocido deja el registro
     ACTIVO: un archivado por error desaparece de las busquedas sin que nadie
     lo note, mientras que un registro de mas se ve y se corrige.
  2. 'Borrador' deja el producto activo pero NO vendible.
  3. La deduplicacion busca con active_test=False. Sin eso, archivar un
     registro y resincronizar crea un DUPLICADO, porque Odoo oculta los
     archivados en toda busqueda.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cargar(nombre_modulo: str, ruta_rel: str):
    """
    Carga un script de scripts/ como modulo.

    Se hace por ruta y no con un import normal porque scripts/ no es un paquete
    y sus modulos ejecutan load_dotenv() al importarse.
    """
    ruta = os.path.join(RAIZ, ruta_rel)
    spec = importlib.util.spec_from_file_location(nombre_modulo, ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre_modulo] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def clientes_mod():
    return _cargar("_sync_clientes", "scripts/sincronizar_clientes_smartier.py")


@pytest.fixture(scope="module")
def productos_mod():
    return _cargar("_sync_productos", "scripts/sincronizar_productos_smartier.py")


def _cliente(**extra) -> dict:
    """Cliente de Smartier con la forma real devuelta por la API."""
    base = {
        "Id": 8,
        "Nombre": "LISBETH SANCHEZ",
        "Tipo": "Contacto",
        "Estado": "Habilitado",
        "Documento": {"Tipo": 6, "Contenido": None},
        "Email": "ventas@ejemplo.net",
        "RazonSocial": None,
    }
    base.update(extra)
    return base


def _producto(**extra) -> dict:
    """Producto de Smartier con la forma real devuelta por la API."""
    base = {
        "Id": 12,
        "Nombre": "Talonario 1/2 carta",
        "Tipo": "Simple",
        "Estado": "Disponible",
        "PorcentajeIVA": 16,
        "Exento": False,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Clientes: Estado -> active
# ---------------------------------------------------------------------------


class TestEstadoCliente:

    def test_habilitado_queda_activo(self, clientes_mod):
        assert clientes_mod._valores(_cliente(Estado="Habilitado"))["active"] is True

    def test_deshabilitado_archiva(self, clientes_mod):
        assert clientes_mod._valores(_cliente(Estado="Deshabilitado"))["active"] is False

    def test_estado_desconocido_no_archiva(self, clientes_mod):
        """
        La decision clave: ante un estado que Smartier anada en el futuro, el
        contacto sigue VISIBLE. Archivar por defecto lo haria desaparecer de
        las busquedas de Odoo sin que nadie se entere.
        """
        for estado in ("Suspendido", "EnRevision", "", None):
            valores = clientes_mod._valores(_cliente(Estado=estado))
            assert valores["active"] is True, f"{estado!r} no debe archivar"

    def test_espacios_alrededor_del_estado(self, clientes_mod):
        assert clientes_mod._valores(_cliente(Estado="  Deshabilitado  "))["active"] is False


class TestTipoCliente:

    def test_contacto_es_persona(self, clientes_mod):
        assert clientes_mod._valores(_cliente(Tipo="Contacto"))["company_type"] == "person"

    def test_empresa_es_compania(self, clientes_mod):
        assert clientes_mod._valores(_cliente(Tipo="Empresa"))["company_type"] == "company"

    def test_el_tipo_manda_sobre_la_razon_social(self, clientes_mod):
        """
        Antes se deducia de si habia RazonSocial. Smartier da el Tipo explicito,
        y un Contacto puede traer razon social sin ser una empresa.
        """
        valores = clientes_mod._valores(
            _cliente(Tipo="Contacto", RazonSocial="TURICOPY C.A.")
        )
        assert valores["company_type"] == "person"

    def test_sin_tipo_se_cae_en_la_razon_social(self, clientes_mod):
        assert clientes_mod._valores(
            _cliente(Tipo=None, RazonSocial="TURICOPY C.A.")
        )["company_type"] == "company"
        assert clientes_mod._valores(
            _cliente(Tipo=None, RazonSocial=None)
        )["company_type"] == "person"


# ---------------------------------------------------------------------------
# Productos: Estado -> (active, sale_ok)
# ---------------------------------------------------------------------------


class TestEstadoProducto:

    def test_disponible_activo_y_vendible(self, productos_mod):
        assert productos_mod._estado_odoo(_producto(Estado="Disponible")) == (True, True)

    def test_borrador_activo_pero_no_vendible(self, productos_mod):
        """Existe en el catalogo, pero todavia no puede facturarse."""
        assert productos_mod._estado_odoo(_producto(Estado="Borrador")) == (True, False)

    def test_deshabilitado_archivado(self, productos_mod):
        assert productos_mod._estado_odoo(_producto(Estado="Deshabilitado")) == (False, False)

    def test_estado_desconocido_queda_disponible(self, productos_mod):
        for estado in ("Agotado", "Descatalogado", "", None):
            assert productos_mod._estado_odoo(_producto(Estado=estado)) == (True, True), \
                f"{estado!r} no debe archivar"

    def test_los_valores_llegan_a_odoo(self, productos_mod):
        """El mapeo debe reflejarse en el dict que se manda a Odoo."""
        valores = productos_mod._valores(_producto(Estado="Borrador"), tax_id=3)
        assert valores["active"] is True
        assert valores["sale_ok"] is False

        valores = productos_mod._valores(_producto(Estado="Deshabilitado"), tax_id=3)
        assert valores["active"] is False
        assert valores["sale_ok"] is False


# ---------------------------------------------------------------------------
# Deduplicacion frente a registros archivados
# ---------------------------------------------------------------------------


class TestDedupEncuentraArchivados:
    """
    Sin active_test=False, archivar un registro y volver a sincronizar crea un
    duplicado: Odoo no lo encuentra y el script cree que es nuevo.
    """

    def test_clientes_busca_incluyendo_archivados(self, clientes_mod):
        odoo = MagicMock()
        odoo.execute.return_value = []

        clientes_mod._buscar_en_odoo(odoo, _cliente(Documento={"Contenido": "J-123"}))

        assert odoo.execute.called
        for llamada in odoo.execute.call_args_list:
            contexto = llamada.kwargs.get("context") or {}
            assert contexto.get("active_test") is False, \
                "la busqueda debe incluir los archivados"

    def test_clientes_encuentra_un_contacto_archivado(self, clientes_mod):
        """Un contacto archivado se REUTILIZA, no se duplica."""
        odoo = MagicMock()
        odoo.execute.return_value = [{"id": 42, "name": "ARCHIVADO"}]

        id_odoo, motivo = clientes_mod._buscar_en_odoo(odoo, _cliente())

        assert id_odoo == 42
        assert "SMARTIER-8" in motivo

    def test_productos_busca_incluyendo_archivados(self, productos_mod):
        import inspect

        fuente = inspect.getsource(productos_mod.main)
        assert '"active_test": False' in fuente, \
            "la dedup de productos debe incluir los archivados"
