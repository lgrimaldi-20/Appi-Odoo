"""
Consultas de OBSERVABILIDAD sobre la base de datos de control (state store).

Capa de solo lectura para el panel de monitoreo: resume y lista los registros de
sync_map (estado de cada sincronizacion) y sync_log (bitacora de auditoria), con
filtros. NO modifica nada: es el reverso de state_store, pensado para observar.

Se apoya en la misma sesion/engine que state_store, asi que respeta DATABASE_URL.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import func

from core.models_db import SyncLog, SyncMap
from core.state_store import get_session


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Serializa un datetime a ISO 8601 (o None)."""
    return dt.isoformat() if dt else None


def _map_a_dict(m: SyncMap) -> dict:
    """Convierte un SyncMap a dict serializable para el panel."""
    return {
        "id": m.id,
        "entidad": m.entidad,
        "id_origen": m.id_origen,
        "model_odoo": m.model_odoo,
        "id_odoo": m.id_odoo,
        "estado": m.estado,
        "error": m.error,
        "creado": _iso(m.creado),
        "actualizado": _iso(m.actualizado),
    }


def _log_a_dict(l: SyncLog) -> dict:
    """Convierte un SyncLog a dict serializable para el panel."""
    return {
        "id": l.id,
        "entidad": l.entidad,
        "id_origen": l.id_origen,
        "accion": l.accion,
        "resultado": l.resultado,
        "detalle": l.detalle,
        "timestamp": _iso(l.timestamp),
    }


def resumen() -> dict:
    """
    Devuelve un resumen agregado para las tarjetas del panel:
      - totales por estado (PROCESADO, ERROR, PROCESANDO, PENDIENTE)
      - totales por entidad
      - conteo de logs y de errores en la bitacora
    """
    with get_session() as session:
        por_estado = dict(
            session.query(SyncMap.estado, func.count(SyncMap.id))
            .group_by(SyncMap.estado)
            .all()
        )
        por_entidad = dict(
            session.query(SyncMap.entidad, func.count(SyncMap.id))
            .group_by(SyncMap.entidad)
            .all()
        )
        total_map = session.query(func.count(SyncMap.id)).scalar() or 0
        total_log = session.query(func.count(SyncLog.id)).scalar() or 0
        total_errores_log = (
            session.query(func.count(SyncLog.id))
            .filter(SyncLog.resultado == "ERROR")
            .scalar()
            or 0
        )

    return {
        "total": total_map,
        "por_estado": {
            "PROCESADO": por_estado.get("PROCESADO", 0),
            "ERROR": por_estado.get("ERROR", 0),
            "PROCESANDO": por_estado.get("PROCESANDO", 0),
            "PENDIENTE": por_estado.get("PENDIENTE", 0),
            "ELIMINADO": por_estado.get("ELIMINADO", 0),
        },
        "por_entidad": por_entidad,
        "logs": {"total": total_log, "errores": total_errores_log},
    }


def listar_sincronizaciones(
    estado: Optional[str] = None,
    entidad: Optional[str] = None,
    id_origen: Optional[str] = None,
    limite: int = 100,
    offset: int = 0,
) -> dict:
    """
    Lista los registros de sync_map, mas recientes primero, con filtros opcionales
    por estado, entidad e id_origen (coincidencia parcial). Devuelve {total, items}.
    """
    limite = max(1, min(limite, 500))
    with get_session() as session:
        q = session.query(SyncMap)
        if estado:
            q = q.filter(SyncMap.estado == estado)
        if entidad:
            q = q.filter(SyncMap.entidad == entidad)
        if id_origen:
            q = q.filter(SyncMap.id_origen.like(f"%{id_origen}%"))

        total = q.with_entities(func.count(SyncMap.id)).scalar() or 0
        items = (
            q.order_by(SyncMap.actualizado.desc())
            .offset(max(0, offset))
            .limit(limite)
            .all()
        )
    return {"total": total, "items": [_map_a_dict(m) for m in items]}


def listar_logs(
    entidad: Optional[str] = None,
    id_origen: Optional[str] = None,
    resultado: Optional[str] = None,
    limite: int = 100,
    offset: int = 0,
) -> dict:
    """
    Lista la bitacora sync_log, mas reciente primero, con filtros opcionales.
    Devuelve {total, items}.
    """
    limite = max(1, min(limite, 500))
    with get_session() as session:
        q = session.query(SyncLog)
        if entidad:
            q = q.filter(SyncLog.entidad == entidad)
        if id_origen:
            q = q.filter(SyncLog.id_origen.like(f"%{id_origen}%"))
        if resultado:
            q = q.filter(SyncLog.resultado == resultado)

        total = q.with_entities(func.count(SyncLog.id)).scalar() or 0
        items = (
            q.order_by(SyncLog.timestamp.desc())
            .offset(max(0, offset))
            .limit(limite)
            .all()
        )
    return {"total": total, "items": [_log_a_dict(l) for l in items]}


def detalle_registro(entidad: str, id_origen: str) -> dict:
    """
    Devuelve el mapeo de un registro concreto junto con toda su bitacora,
    para la vista de detalle del panel. {mapeo, logs}.
    """
    with get_session() as session:
        mapa = (
            session.query(SyncMap)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .one_or_none()
        )
        logs = (
            session.query(SyncLog)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .order_by(SyncLog.timestamp.asc())
            .all()
        )
    return {
        "mapeo": _map_a_dict(mapa) if mapa else None,
        "logs": [_log_a_dict(l) for l in logs],
    }
