"""
Tests de la idempotencia bajo CONCURRENCIA y de las cacheas de datos maestros.

El bug que motiva este fichero: con el patron "comprobar y luego actuar", dos
peticiones simultaneas con el mismo id_origen leian ambas "no procesado" y las
dos creaban el registro en Odoo (duplicado contable observado contra una
instancia real). La reserva atomica lo corta apoyandose en la UniqueConstraint.
"""

import importlib
import threading

import pytest


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    from core import state_store
    importlib.reload(state_store)
    state_store.init_db()
    return state_store


class TestReservaAtomica:
    def test_primera_reserva_devuelve_none(self, entorno):
        """Reservar algo nuevo concede el turno (None = procesa tu)."""
        assert entorno.reservar("factura", "F-1", model_odoo="account.move") is None

    def test_segunda_reserva_en_curso_es_rechazada(self, entorno):
        """Mientras esta PROCESANDO, otro no puede entrar."""
        entorno.reservar("factura", "F-2", model_odoo="account.move")
        with pytest.raises(entorno.ReservaOcupada):
            entorno.reservar("factura", "F-2", model_odoo="account.move")

    def test_ya_procesado_devuelve_id_odoo(self, entorno):
        """Si ya se completo, la reserva devuelve el id existente."""
        entorno.reservar("factura", "F-3", model_odoo="account.move")
        entorno.registrar_mapeo(
            "factura", "F-3", id_odoo=77,
            estado=entorno.EstadoSync.PROCESADO,
        )
        assert entorno.reservar("factura", "F-3") == 77

    def test_tras_error_se_puede_reintentar(self, entorno):
        """Un registro en ERROR no queda bloqueado para siempre."""
        entorno.reservar("factura", "F-4", model_odoo="account.move")
        entorno.marcar_estado(
            "factura", "F-4", entorno.EstadoSync.ERROR, error="fallo"
        )
        assert entorno.reservar("factura", "F-4") is None

    def test_solo_un_hilo_gana_la_carrera(self, entorno):
        """
        El test que reproduce el bug: 8 hilos compiten por el mismo id_origen.
        Exactamente uno debe obtener el turno; el resto, ReservaOcupada.
        """
        ganadores, perdedores = [], []
        barrera = threading.Barrier(8)

        def intentar():
            barrera.wait()  # maximiza el solapamiento
            try:
                if entorno.reservar("factura", "RACE", model_odoo="account.move") is None:
                    ganadores.append(1)
            except entorno.ReservaOcupada:
                perdedores.append(1)
            except Exception:
                pass  # errores de lock de SQLite cuentan como no-ganador

        hilos = [threading.Thread(target=intentar) for _ in range(8)]
        for h in hilos: h.start()
        for h in hilos: h.join()

        assert len(ganadores) == 1, (
            f"{len(ganadores)} hilos creerian tener el turno -> duplicados en Odoo"
        )


class TestCacheDatosMaestros:
    def test_segunda_resolucion_no_vuelve_a_consultar_odoo(self, tmp_path, monkeypatch):
        """Resolver dos veces la misma FK debe costar UNA sola llamada."""
        from unittest.mock import MagicMock
        from core import mapper
        mapper.limpiar_cache_fk()

        odoo = MagicMock()
        odoo.execute.return_value = [{"id": 42}]
        regla = {"desde": "cliente_nif", "model": "res.partner",
                 "buscar_por": "vat", "obligatorio": True}

        a = mapper._resolver_fk(odoo, "partner_id", regla, {"cliente_nif": "B1"})
        b = mapper._resolver_fk(odoo, "partner_id", regla, {"cliente_nif": "B1"})

        assert a == b == 42
        assert odoo.execute.call_count == 1, "la cache no evito la segunda llamada"

    def test_no_cachea_los_fallos(self, tmp_path, monkeypatch):
        """Un maestro ausente no se cachea: puede crearse un instante despues."""
        from unittest.mock import MagicMock
        from core import mapper
        mapper.limpiar_cache_fk()

        odoo = MagicMock()
        odoo.execute.return_value = []          # no existe
        regla = {"desde": "cliente_nif", "model": "res.partner",
                 "buscar_por": "vat", "obligatorio": False}

        mapper._resolver_fk(odoo, "partner_id", regla, {"cliente_nif": "NUEVO"})
        odoo.execute.return_value = [{"id": 9}]  # ahora si existe
        assert mapper._resolver_fk(odoo, "partner_id", regla, {"cliente_nif": "NUEVO"}) == 9

    def test_tenants_distintos_no_comparten_ids(self):
        """Dos conectores = dos bases Odoo: sus ids NO son intercambiables."""
        from unittest.mock import MagicMock
        from core import mapper
        mapper.limpiar_cache_fk()

        regla = {"desde": "cliente_nif", "model": "res.partner",
                 "buscar_por": "vat", "obligatorio": True}
        odoo_a, odoo_b = MagicMock(), MagicMock()
        odoo_a.execute.return_value = [{"id": 1}]
        odoo_b.execute.return_value = [{"id": 999}]

        assert mapper._resolver_fk(odoo_a, "partner_id", regla, {"cliente_nif": "B1"}) == 1
        assert mapper._resolver_fk(odoo_b, "partner_id", regla, {"cliente_nif": "B1"}) == 999


class TestSesionHttpReutilizada:
    def test_el_conector_reutiliza_una_sola_sesion(self):
        """Todas las llamadas deben salir de la MISMA Session (keep-alive)."""
        from unittest.mock import MagicMock, patch
        import odoo_universal

        def responder(*a, **k):
            metodo = (k.get("json") or {}).get("params", {}).get("method")
            r = MagicMock()
            r.raise_for_status.return_value = None
            # common.version -> dict; authenticate -> uid; execute_kw -> datos
            r.json.return_value = {"result": {"server_serie": "19.0"} if metodo == "version" else 7}
            return r

        with patch("requests.Session.post", side_effect=responder) as post:
            api = odoo_universal.OdooUniversalAPI("http://x", "db", "u", "p")
            api.execute("res.partner", "search_read", [])

        assert post.called
        # Una unica Session viva en el conector.
        assert isinstance(api._session, odoo_universal.requests.Session)


class TestFalloDeConexionTrasCrear:
    """
    Odoo no participa en la transaccion del middleware: si la conexion se corta
    DESPUES del create/post, el registro queda vivo en Odoo aunque el proceso
    aborte. La reserva debe liberarse y el reintento adoptar ese registro.
    """

    def _entorno(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
        import core.state_store as state_store
        importlib.reload(state_store)
        state_store.init_db()
        import core.sincronizador as sincronizador
        importlib.reload(sincronizador)
        return sincronizador, state_store

    def _registro(self, ref="NET-1"):
        return {"factura_id": ref, "cliente_nif": "B1",
                "fecha": "2026-08-21", "total": 100.0, "lineas": []}

    def test_la_reserva_se_libera_para_poder_reintentar(self, tmp_path, monkeypatch):
        """
        Sin esto la fila quedaria en PROCESANDO y reservar() la rechazaria para
        siempre: bloqueada de forma permanente.
        """
        from unittest.mock import MagicMock
        from odoo_universal import OdooConnectionError
        sincronizador, state_store = self._entorno(tmp_path, monkeypatch)

        odoo = MagicMock()
        def execute(model, method, *a, **k):
            if model == "res.partner":
                return [{"id": 7}]
            if method == "create":
                return 555
            if method == "action_post":
                raise OdooConnectionError("red caida tras postear")
            return None
        odoo.execute.side_effect = execute

        with pytest.raises(OdooConnectionError):
            sincronizador.sincronizar_entidad("factura", self._registro("NET-LIBERAR"), odoo)

        mapa = state_store.buscar_mapeo("factura", "NET-LIBERAR")
        assert mapa.estado == state_store.EstadoSync.PENDIENTE.value, (
            "quedaria bloqueada en PROCESANDO"
        )
        assert mapa.id_odoo == 555, "se pierde el rastro del registro huerfano"

    def test_el_reintento_adopta_el_registro_y_no_lo_duplica(self, tmp_path, monkeypatch):
        """El segundo intento debe continuar sobre el id ya creado."""
        from unittest.mock import MagicMock
        from odoo_universal import OdooConnectionError
        sincronizador, state_store = self._entorno(tmp_path, monkeypatch)

        creates = []
        fallar = {"si": True}
        estado = {"valor": "draft"}

        odoo = MagicMock()
        def execute(model, method, *a, **k):
            if model == "res.partner":
                return [{"id": 7}]
            if method == "create":
                creates.append(1)
                return 555
            if method == "action_post":
                if fallar["si"]:
                    # Odoo SI llego a postear; lo que se pierde es la respuesta.
                    estado["valor"] = "posted"
                    raise OdooConnectionError("red caida")
                return True
            if method == "read":
                return [{"state": estado["valor"], "amount_total": 100.0}]
            return None
        odoo.execute.side_effect = execute

        with pytest.raises(OdooConnectionError):
            sincronizador.sincronizar_entidad("factura", self._registro("NET-ADOPTA"), odoo)

        fallar["si"] = False
        res = sincronizador.sincronizar_entidad("factura", self._registro("NET-ADOPTA"), odoo)

        assert res.id_odoo == 555
        assert res.estado == state_store.EstadoSync.PROCESADO.value
        assert len(creates) == 1, f"se creo {len(creates)} veces: duplicado en Odoo"

    def test_no_repostea_lo_que_ya_esta_posteado(self, tmp_path, monkeypatch):
        """
        action_post NO es idempotente en Odoo 19: sobre un asiento ya posteado
        responde "debe ser un borrador". Al retomar hay que saltarse el paso.
        """
        from unittest.mock import MagicMock
        from odoo_universal import OdooConnectionError
        sincronizador, state_store = self._entorno(tmp_path, monkeypatch)

        posts = []
        fallar = {"si": True}
        estado = {"valor": "draft"}
        odoo = MagicMock()
        def execute(model, method, *a, **k):
            if model == "res.partner":
                return [{"id": 7}]
            if method == "create":
                return 555
            if method == "action_post":
                if fallar["si"]:
                    estado["valor"] = "posted"   # Odoo lo posteo de verdad
                    raise OdooConnectionError("red caida")
                posts.append(1)
                return True
            if method == "read":
                return [{"state": estado["valor"], "amount_total": 100.0}]
            return None
        odoo.execute.side_effect = execute

        with pytest.raises(OdooConnectionError):
            sincronizador.sincronizar_entidad("factura", self._registro("NET-REPOST"), odoo)
        fallar["si"] = False
        sincronizador.sincronizar_entidad("factura", self._registro("NET-REPOST"), odoo)

        assert posts == [], "reposteo un asiento ya posteado -> error de Odoo"
