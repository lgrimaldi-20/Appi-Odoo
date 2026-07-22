"""
Tests de la base de datos de control (state store - Fase 1).

Usan una base de datos SQLite temporal y aislada por test, de modo que no
tocan la base de datos real (control.db) ni requieren conexion con Odoo.
"""

import importlib

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """
    Recarga core.state_store apuntando DATABASE_URL a un SQLite temporal y
    crea las tablas. Devuelve el modulo listo para usar, aislado por test.
    """
    db_file = tmp_path / "control_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    import core.state_store as state_store
    importlib.reload(state_store)  # relee DATABASE_URL con el valor del monkeypatch
    state_store.init_db()
    return state_store


@pytest.fixture()
def EstadoSync():
    from core.models_db import EstadoSync as _E
    return _E


# --- init_db ---


class TestInitDb:
    def test_init_db_es_idempotente(self, store):
        # Llamar dos veces no debe fallar.
        store.init_db()
        store.init_db()


# --- registrar_mapeo / buscar_mapeo (idempotencia) ---


class TestMapeo:
    def test_buscar_inexistente_devuelve_none(self, store):
        assert store.buscar_mapeo("factura", "F-001") is None

    def test_registrar_y_buscar(self, store, EstadoSync):
        store.registrar_mapeo(
            "factura", "F-001",
            model_odoo="account.move", id_odoo=42,
            estado=EstadoSync.PROCESADO,
        )
        mapa = store.buscar_mapeo("factura", "F-001")
        assert mapa is not None
        assert mapa.id_odoo == 42
        assert mapa.model_odoo == "account.move"
        assert mapa.estado == EstadoSync.PROCESADO.value

    def test_registrar_dos_veces_no_duplica(self, store, EstadoSync):
        """Idempotencia: mismo (entidad, id_origen) actualiza, no crea otra fila."""
        store.registrar_mapeo("factura", "F-001", estado=EstadoSync.PENDIENTE)
        store.registrar_mapeo(
            "factura", "F-001", id_odoo=99, estado=EstadoSync.PROCESADO
        )
        mapa = store.buscar_mapeo("factura", "F-001")
        assert mapa.id_odoo == 99
        assert mapa.estado == EstadoSync.PROCESADO.value

    def test_id_origen_numerico_se_normaliza_a_texto(self, store):
        """Un id_origen entero y su equivalente en texto son el mismo registro."""
        store.registrar_mapeo("pago", 123, id_odoo=7)
        assert store.buscar_mapeo("pago", "123") is not None
        assert store.buscar_mapeo("pago", 123) is not None


# --- ya_procesado (atajo de idempotencia) ---


class TestYaProcesado:
    def test_devuelve_id_odoo_si_procesado(self, store, EstadoSync):
        store.registrar_mapeo(
            "factura", "F-002", id_odoo=55, estado=EstadoSync.PROCESADO
        )
        assert store.ya_procesado("factura", "F-002") == 55

    def test_devuelve_none_si_pendiente(self, store, EstadoSync):
        store.registrar_mapeo("factura", "F-003", estado=EstadoSync.PENDIENTE)
        assert store.ya_procesado("factura", "F-003") is None

    def test_devuelve_none_si_no_existe(self, store):
        assert store.ya_procesado("factura", "no-existe") is None


# --- marcar_estado ---


class TestMarcarEstado:
    def test_marcar_error_guarda_mensaje(self, store, EstadoSync):
        store.registrar_mapeo("factura", "F-004", estado=EstadoSync.PROCESANDO)
        store.marcar_estado("factura", "F-004", EstadoSync.ERROR, error="Odoo cayo")
        mapa = store.buscar_mapeo("factura", "F-004")
        assert mapa.estado == EstadoSync.ERROR.value
        assert mapa.error == "Odoo cayo"

    def test_marcar_no_error_limpia_mensaje(self, store, EstadoSync):
        store.registrar_mapeo("factura", "F-005", estado=EstadoSync.ERROR)
        store.marcar_estado("factura", "F-005", EstadoSync.ERROR, error="fallo")
        store.marcar_estado("factura", "F-005", EstadoSync.PROCESADO)
        mapa = store.buscar_mapeo("factura", "F-005")
        assert mapa.estado == EstadoSync.PROCESADO.value
        assert mapa.error is None

    def test_marcar_inexistente_lanza_keyerror(self, store, EstadoSync):
        with pytest.raises(KeyError):
            store.marcar_estado("factura", "no-existe", EstadoSync.PROCESADO)


# --- hash de payload ---


class TestHash:
    def test_hash_estable_independiente_del_orden(self, store):
        h1 = store.calcular_hash({"a": 1, "b": 2})
        h2 = store.calcular_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_hash_cambia_con_los_datos(self, store):
        h1 = store.calcular_hash({"total": 100})
        h2 = store.calcular_hash({"total": 200})
        assert h1 != h2


# --- log de auditoria ---


class TestLog:
    def test_log_agrega_fila(self, store):
        from core.models_db import SyncLog
        store.log("factura", "crear", resultado="OK", id_origen="F-006", detalle="ok")
        with store.get_session() as session:
            filas = session.query(SyncLog).filter_by(id_origen="F-006").all()
            assert len(filas) == 1
            assert filas[0].accion == "crear"
            assert filas[0].resultado == "OK"
