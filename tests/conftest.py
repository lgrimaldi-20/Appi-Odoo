"""
Configuracion compartida de los tests.

La cache de datos maestros de core.mapper es global (vive en el proceso) y se
indexa por id() del conector. En los tests los conectores son MagicMock de vida
corta, y CPython reutiliza direcciones de memoria liberadas: sin limpiarla, un
mock nuevo puede heredar la entrada de otro ya recolectado y devolver un id que
ese test nunca configuro. Se vacia antes de cada test para que sean aislados.
"""

import pytest


@pytest.fixture(autouse=True)
def _cache_limpia():
    from core import mapper
    mapper.limpiar_cache_fk()
    yield
    mapper.limpiar_cache_fk()
