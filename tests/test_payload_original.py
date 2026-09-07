"""
Tests del guardado del documento ORIGINAL de Smartier en la cola.

Por que importa: 'payload' guarda solo lo que el mapper necesita. Si Smartier
anade manana un campo que hoy ignoramos, no quedaria rastro de el, y la unica
forma de recuperarlo seria volver a pedirselo a la API -- que puede haber
purgado la nota. 'payload_original' conserva el documento completo.
"""

import importlib

import pytest


@pytest.fixture()
def cola(tmp_path, monkeypatch):
    """Cola aislada en una SQLite temporal."""
    db = tmp_path / "cola.db"
    monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{db}")
    import core.poller_source as ps
    importlib.reload(ps)
    ps.init_source_db()
    return ps


NOTA = {
    "Id": 9001,
    "Estado": "Facturada",
    "Cantidad": 10,
    "PrecioUnitario": {"Monto": 4020.0, "Moneda": "Nacional"},
    # Campo que el mapper NO usa: es justo lo que se perderia sin el original.
    "CampoQueHoyIgnoramos": "valor importante",
    "Orden": {"Id": 5001, "Cliente": {"Id": 8, "Documento": {"Contenido": "J-1"}},
              "Producto": {"Id": 283, "Nombre": "Hojas", "PorcentajeIVA": 16}},
}


class TestGuardadoDelOriginal:
    def test_la_ingesta_guarda_la_nota_entera(self, cola, monkeypatch):
        import core.ingesta_smartier as ing
        importlib.reload(ing)
        monkeypatch.setattr(ing, "poller_source", cola)

        registro = ing.nota_a_registro(NOTA)
        ing.encolar_registros("factura", [(registro["factura_id"], registro, NOTA)])

        with cola.get_source_session() as s:
            fila = s.query(cola.ColaSincronizacion).one()
            assert fila.payload_original == NOTA

    def test_conserva_un_campo_que_el_mapper_descarta(self, cola, monkeypatch):
        import core.ingesta_smartier as ing
        importlib.reload(ing)
        monkeypatch.setattr(ing, "poller_source", cola)

        registro = ing.nota_a_registro(NOTA)
        # El registro traducido no lleva ese campo...
        assert "CampoQueHoyIgnoramos" not in registro
        ing.encolar_registros("factura", [(registro["factura_id"], registro, NOTA)])

        with cola.get_source_session() as s:
            fila = s.query(cola.ColaSincronizacion).one()
            # ...pero el original si.
            assert fila.payload_original["CampoQueHoyIgnoramos"] == "valor importante"

    def test_encolar_sin_original_sigue_funcionando(self, cola):
        # Los scripts de prueba encolan pares, sin original: no deben romperse.
        import core.ingesta_smartier as ing
        importlib.reload(ing)
        ing.poller_source = cola
        ing.encolar_registros("factura", [("NE-1", {"factura_id": "NE-1"})])

        with cola.get_source_session() as s:
            assert s.query(cola.ColaSincronizacion).one().payload_original is None


class TestMigracion:
    def test_anade_la_columna_a_una_cola_que_ya_existia(self, tmp_path, monkeypatch):
        """
        create_all() crea tablas que faltan pero NUNCA altera una existente: sin
        la migracion, una base ya en marcha se queda sin la columna y todo
        insert falla.
        """
        import sqlite3
        db = tmp_path / "vieja.db"
        # Cola con el esquema ANTERIOR, sin payload_original.
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE cola_sincronizacion (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entidad VARCHAR(50) NOT NULL,
                id_origen VARCHAR(100) NOT NULL,
                payload JSON NOT NULL,
                estado VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE',
                error_detalle TEXT,
                creado_en DATETIME NOT NULL,
                procesado_en DATETIME
            )""")
        con.commit(); con.close()

        monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{db}")
        import core.poller_source as ps
        importlib.reload(ps)
        ps.init_source_db()

        con = sqlite3.connect(db)
        columnas = {r[1] for r in con.execute("pragma table_info(cola_sincronizacion)")}
        con.close()
        assert "payload_original" in columnas

    def test_repetir_la_migracion_no_falla(self, cola):
        # init_source_db() corre en cada arranque: debe ser idempotente.
        cola.init_source_db()
        cola.init_source_db()
