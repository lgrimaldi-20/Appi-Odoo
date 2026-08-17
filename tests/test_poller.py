"""
Tests del poller (modo pull: DB del cliente -> Odoo).

Aislan tanto la base de CONTROL como la de ORIGEN con SQLite temporal y mockean
Odoo, verificando:
  - una fila PENDIENTE se sincroniza y queda PROCESADO en la cola del cliente,
  - un fallo de datos aisla la fila (ERROR) sin abortar el resto del lote,
  - un fallo de conexion aborta el lote y deja la fila PENDIENTE (para reintento),
  - la idempotencia: reprocesar no duplica en Odoo.
"""

import importlib
from unittest.mock import MagicMock

import pytest

from odoo_universal import OdooConnectionError


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """
    state_store + poller_source con SQLite temporal (control y origen separados),
    y el poller recargado contra ellos. Registra un tenant mock 'default'.
    """
    control_db = tmp_path / "control.db"
    source_db = tmp_path / "source.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{control_db}")
    monkeypatch.setenv("SOURCE_DATABASE_URL", f"sqlite:///{source_db}")

    import core.state_store as state_store
    importlib.reload(state_store)
    state_store.init_db()

    import core.poller_source as poller_source
    importlib.reload(poller_source)
    poller_source.init_source_db()

    import core.poller as poller
    importlib.reload(poller)

    return poller, poller_source, state_store


def _odoo(existe_partner=True):
    """
    Mock de Odoo para una factura simple:
      res.partner search_read -> id 7 (por vat) si existe_partner
      account.move create -> 999 ; action_post -> OK ; read (amount_total) -> ok
    """
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            return [{"id": 7}] if existe_partner else []
        if model == "res.currency" and method == "search_read":
            return [{"id": 2}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            return [{"id": 999, "amount_total": 121.0}]
        return True

    odoo.execute.side_effect = execute
    return odoo


def _insertar_factura(poller_source, id_origen="F-1", total=121.0, nif="B123"):
    """Inserta una fila 'factura' PENDIENTE en la cola del cliente."""
    with poller_source.get_source_session() as s:
        s.add(
            poller_source.ColaSincronizacion(
                entidad="factura",
                id_origen=id_origen,
                payload={
                    "factura_id": id_origen,
                    "cliente_nif": nif,
                    "fecha": "2026-07-01",
                    "total": total,
                    "lineas": [],
                },
            )
        )


def _estado_cola(poller_source, id_origen):
    with poller_source.get_source_session() as s:
        fila = (
            s.query(poller_source.ColaSincronizacion)
            .filter_by(id_origen=id_origen)
            .one()
        )
        return fila.estado, fila.error_detalle


def test_fila_pendiente_se_sincroniza_y_queda_procesada(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)
    odoo = _odoo()
    monkey_tenant(poller, odoo)

    res = poller.procesar_lote()

    assert res.leidas == 1
    assert res.procesadas == 1
    assert res.con_error == 0
    assert _estado_cola(poller_source, "F-1")[0] == "PROCESADO"
    # Y quedo mapeado en el state store de control.
    assert state_store.ya_procesado("factura", "F-1") == 999


def test_fallo_de_datos_aisla_la_fila_sin_abortar_el_lote(entorno):
    poller, poller_source, state_store = entorno
    # F-1 valida; F-2 con NIF que no existe en Odoo -> MapeoError -> ERROR.
    _insertar_factura(poller_source, id_origen="F-1")
    _insertar_factura(poller_source, id_origen="F-2", nif="NOEXISTE")

    # partner existe solo para B123; para NOEXISTE devuelve vacio.
    odoo = MagicMock()

    def execute(model, method, *args, **kwargs):
        if model == "res.partner" and method == "search_read":
            vat = args[0][0][2]  # dominio [["vat","=",valor]]
            return [{"id": 7}] if vat == "B123" else []
        if model == "res.currency" and method == "search_read":
            return [{"id": 2}]
        if model == "account.move" and method == "create":
            return 999
        if model == "account.move" and method == "read":
            return [{"id": 999, "amount_total": 121.0}]
        return True

    odoo.execute.side_effect = execute
    monkey_tenant(poller, odoo)

    res = poller.procesar_lote()

    assert res.leidas == 2
    assert res.procesadas == 1
    assert res.con_error == 1
    assert _estado_cola(poller_source, "F-1")[0] == "PROCESADO"
    estado, error = _estado_cola(poller_source, "F-2")
    assert estado == "ERROR"
    assert error  # trae el detalle del fallo


def test_fallo_de_conexion_aborta_el_lote_y_deja_pendiente(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)

    odoo = MagicMock()
    odoo.execute.side_effect = OdooConnectionError("Odoo caido")
    monkey_tenant(poller, odoo)

    with pytest.raises(OdooConnectionError):
        poller.procesar_lote()

    # La fila NO se marca: sigue PENDIENTE para el reintento.
    assert _estado_cola(poller_source, "F-1")[0] == "PENDIENTE"


def test_idempotencia_no_duplica_en_odoo(entorno):
    poller, poller_source, state_store = entorno
    _insertar_factura(poller_source)
    odoo = _odoo()
    monkey_tenant(poller, odoo)

    poller.procesar_lote()

    # El cliente reinserta la MISMA factura (id_origen repetido) como PENDIENTE.
    _insertar_factura(poller_source, id_origen="F-1")
    creates_antes = _contar_creates(odoo)

    res = poller.procesar_lote()

    assert res.procesadas == 1
    # No hubo un segundo create en Odoo: la idempotencia lo cortocircuito.
    assert _contar_creates(odoo) == creates_antes


def test_polling_deshabilitado_devuelve_lote_vacio(entorno, monkeypatch):
    poller, poller_source, state_store = entorno
    monkeypatch.setattr(poller_source, "SOURCE_DATABASE_URL", "")

    res = poller.procesar_lote()

    assert res.leidas == 0
    assert res.procesadas == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def monkey_tenant(poller, odoo):
    """Hace que get_tenant (resuelto dentro de poller) devuelva el mock."""
    poller.get_tenant = lambda *a, **k: odoo


def _contar_creates(odoo):
    return sum(
        1
        for c in odoo.execute.call_args_list
        if c.args[:2] == ("account.move", "create")
    )
