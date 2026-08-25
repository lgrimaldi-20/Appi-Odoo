"""
Tests de las tareas Celery (Fase 5): flujo compuesto y rollback logico.

Corren en modo EAGER (sin broker): las tareas se ejecutan inline. Se mockean
las funciones de negocio y get_tenant dentro del modulo core.tasks para probar
la ORQUESTACION (orden, rollback) sin Odoo real.
"""

from unittest.mock import MagicMock

import pytest

from core.sincronizador import ResultadoSync, SincronizacionError
from core.conciliacion import ConciliacionError


@pytest.fixture()
def tasks(monkeypatch):
    """
    Expone core.tasks con get_tenant parcheado. NO se recarga el modulo: las
    tareas Celery se registran una sola vez, y parchear por nombre con
    monkeypatch (auto-revertido) evita contaminar otros tests.
    """
    import core.tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "get_tenant", lambda t: MagicMock())
    return tasks_mod


def _res(id_origen, id_odoo):
    return ResultadoSync(id_origen=id_origen, id_odoo=id_odoo,
                         estado="PROCESADO", idempotente=False)


# --- procesar_venta: flujo feliz ---


class TestProcesarVentaExitoso:
    def test_factura_pago_conciliacion_en_orden(self, tasks, monkeypatch):
        llamadas = []
        monkeypatch.setattr(tasks, "crear_factura",
                            lambda reg, odoo: llamadas.append("factura") or _res("F-1", 10))
        monkeypatch.setattr(tasks, "crear_pago",
                            lambda reg, odoo: llamadas.append("pago") or _res("P-1", 20))
        monkeypatch.setattr(tasks, "conciliar",
                            lambda *a, **k: llamadas.append("conciliar") or {"conciliado": True})
        cancel = MagicMock()
        monkeypatch.setattr(tasks, "cancelar_factura", cancel)

        res = tasks.procesar_venta_task.apply(args=[{"f": 1}, {"p": 1}]).get()

        assert llamadas == ["factura", "pago", "conciliar"]
        assert res["factura"]["id_odoo"] == 10
        cancel.assert_not_called()  # sin rollback en el flujo feliz


# --- procesar_venta: rollback ---


class TestRollback:
    def test_rollback_si_falla_el_pago(self, tasks, monkeypatch):
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo, entidad="factura": _res("F-2", 11))

        def pago_falla(reg, odoo):
            raise SincronizacionError("cliente del pago no existe")
        monkeypatch.setattr(tasks, "crear_pago", pago_falla)

        cancel = MagicMock()
        monkeypatch.setattr(tasks, "cancelar_factura", cancel)

        with pytest.raises(SincronizacionError):
            tasks.procesar_venta_task.apply(args=[{"f": 1}, {"p": 1}]).get()

        # Se compenso la factura 11.
        cancel.assert_called_once()
        assert cancel.call_args.args[0] == 11

    def test_rollback_si_falla_la_conciliacion(self, tasks, monkeypatch):
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo, entidad="factura": _res("F-3", 12))
        monkeypatch.setattr(tasks, "crear_pago", lambda reg, odoo: _res("P-3", 22))

        def conc_falla(*a, **k):
            raise ConciliacionError("sin apuntes conciliables")
        monkeypatch.setattr(tasks, "conciliar", conc_falla)

        cancel = MagicMock()
        monkeypatch.setattr(tasks, "cancelar_factura", cancel)

        with pytest.raises(ConciliacionError):
            tasks.procesar_venta_task.apply(args=[{"f": 1}, {"p": 1}]).get()

        cancel.assert_called_once()
        assert cancel.call_args.args[0] == 12


# --- tareas simples ---


class TestTareasSimples:
    def test_sincronizar_factura_task_devuelve_dict(self, tasks, monkeypatch):
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo, entidad="factura": _res("F-9", 99))
        res = tasks.sincronizar_factura_task.apply(args=[{"f": 1}]).get()
        assert res["id_odoo"] == 99
        assert res["estado"] == "PROCESADO"
