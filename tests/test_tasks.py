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
    Expone core.tasks con asegurar_tenant parcheado. NO se recarga el modulo: las
    tareas Celery se registran una sola vez, y parchear por nombre con
    monkeypatch (auto-revertido) evita contaminar otros tests.
    """
    import core.tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "asegurar_tenant", lambda t: MagicMock())
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
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo: _res("F-2", 11))

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
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo: _res("F-3", 12))
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
        monkeypatch.setattr(tasks, "crear_factura", lambda reg, odoo: _res("F-9", 99))
        res = tasks.sincronizar_factura_task.apply(args=[{"f": 1}]).get()
        assert res["id_odoo"] == 99
        assert res["estado"] == "PROCESADO"


class TestRegistroDeTareas:
    """
    El worker debe conocer las tareas sin que nadie importe core.tasks por el.

    Este fallo no se veia en desarrollo: alli manda la API, que importa
    core.tasks al montar los routers y de paso las registra. Un worker en su
    propio contenedor no importa nada por su cuenta, y rechazaba todo lo que
    Beat le encolaba con "Received unregistered task".
    """

    def test_celery_sabe_donde_estan_las_tareas(self):
        from core.celery_app import celery_app
        assert "core.tasks" in (celery_app.conf.include or [])

    def test_las_tareas_programadas_existen(self):
        # Cada entrada de beat_schedule apunta a una tarea por NOMBRE; si el
        # nombre no coincide con ninguna registrada, Beat encola al vacio.
        import core.tasks  # noqa: F401 - registra las tareas
        from core.celery_app import celery_app

        programadas = {
            "core.tasks.poller_task",
            "core.tasks.ingesta_smartier_task",
            "core.tasks.sincronizar_maestros_task",
        }
        assert programadas <= set(celery_app.tasks)


class TestTenantEnElWorker:
    """
    El worker corre en su propio contenedor y NO importa api.py, que era donde
    se registraba el tenant. Sin esto, todas las tareas fallaban con
    KeyError("Tenant 'default' no registrado.") -- un fallo invisible en
    desarrollo, donde Celery corre dentro del proceso de la API.
    """

    def test_las_tareas_no_dependen_de_api_py(self):
        import inspect
        import core.tasks as tasks
        fuente = inspect.getsource(tasks)
        assert "import api" not in fuente

    def test_el_poller_tampoco(self):
        import inspect
        import core.poller as poller
        assert "import api" not in inspect.getsource(poller)

    def test_asegurar_tenant_registra_si_falta(self, monkeypatch):
        import core.tenants as tenants
        import odoo_universal

        odoo_universal._tenants.pop("prueba_worker", None)
        creado = object()
        monkeypatch.setattr(
            tenants, "registrar_tenant_por_defecto",
            lambda nombre="default": odoo_universal.register_tenant(nombre, creado) or creado,
        )
        assert tenants.asegurar_tenant("prueba_worker") is creado
        odoo_universal._tenants.pop("prueba_worker", None)
