"""
State Store: acceso a la base de datos de CONTROL del middleware.

Expone una API sencilla sobre las tablas sync_map / sync_log para:
  - Idempotencia:  buscar_mapeo() antes de enviar nada a Odoo.
  - Trazabilidad de estado:  registrar_mapeo(), marcar_procesando/procesado/error().
  - Auditoria:  log().

La URL de la base de datos se toma de DATABASE_URL (por defecto SQLite local,
cero infraestructura para arrancar; en produccion se apunta a PostgreSQL).
"""

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.models_db import Base, EstadoSync, SyncLog, SyncMap

# ---------------------------------------------------------------------------
# Engine / Session
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./control.db")

# check_same_thread=False solo aplica a SQLite (FastAPI usa varios hilos).
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Crea las tablas de control si no existen. Idempotente."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session() -> Session:
    """Context manager transaccional: commit al salir, rollback ante excepcion."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def calcular_hash(payload) -> str:
    """
    sha256 estable del payload de origen. Sirve para detectar si un registro
    ya sincronizado cambio (mismo id_origen pero datos distintos).
    """
    serializado = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Operaciones sobre sync_map
# ---------------------------------------------------------------------------

def buscar_mapeo(entidad: str, id_origen: str) -> Optional[SyncMap]:
    """
    Devuelve el SyncMap de (entidad, id_origen) o None si no existe.
    Primera comprobacion de idempotencia antes de tocar Odoo.
    """
    with get_session() as session:
        return (
            session.query(SyncMap)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .one_or_none()
        )


def registrar_mapeo(
    entidad: str,
    id_origen: str,
    model_odoo: Optional[str] = None,
    id_odoo: Optional[int] = None,
    estado: EstadoSync = EstadoSync.PENDIENTE,
    hash_payload: Optional[str] = None,
) -> SyncMap:
    """
    Crea o actualiza el mapeo de (entidad, id_origen). Devuelve el SyncMap.
    Si ya existe, actualiza los campos provistos (no los deja en None).
    """
    with get_session() as session:
        mapa = (
            session.query(SyncMap)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .one_or_none()
        )
        if mapa is None:
            mapa = SyncMap(entidad=entidad, id_origen=str(id_origen))
            session.add(mapa)

        if model_odoo is not None:
            mapa.model_odoo = model_odoo
        if id_odoo is not None:
            mapa.id_odoo = id_odoo
        if hash_payload is not None:
            mapa.hash_payload = hash_payload
        mapa.estado = estado.value
        if estado != EstadoSync.ERROR:
            mapa.error = None

        session.flush()
        session.refresh(mapa)
        return mapa


def marcar_estado(
    entidad: str,
    id_origen: str,
    estado: EstadoSync,
    error: Optional[str] = None,
) -> None:
    """Actualiza solo el estado (y el error si aplica) de un mapeo existente."""
    with get_session() as session:
        mapa = (
            session.query(SyncMap)
            .filter_by(entidad=entidad, id_origen=str(id_origen))
            .one_or_none()
        )
        if mapa is None:
            raise KeyError(f"No existe mapeo para {entidad}/{id_origen}")
        mapa.estado = estado.value
        # El texto se conserva en ERROR y en PENDIENTE: un registro pendiente
        # necesita explicar QUE le falta (p.ej. un cliente creado en Odoo pero
        # sin RIF, que todavia no se puede facturar). En los demas estados se
        # limpia, porque ya no hay nada que advertir.
        if estado in (EstadoSync.ERROR, EstadoSync.PENDIENTE):
            mapa.error = error
        else:
            mapa.error = None


def ya_procesado(entidad: str, id_origen: str) -> Optional[int]:
    """
    Atajo de idempotencia: si (entidad, id_origen) ya esta PROCESADO, devuelve
    su id_odoo; en cualquier otro caso devuelve None.
    """
    mapa = buscar_mapeo(entidad, id_origen)
    if mapa and mapa.estado == EstadoSync.PROCESADO.value:
        return mapa.id_odoo
    return None


# ---------------------------------------------------------------------------
# Operaciones sobre sync_log
# ---------------------------------------------------------------------------

def log(
    entidad: str,
    accion: str,
    resultado: str = "OK",
    id_origen: Optional[str] = None,
    detalle: Optional[str] = None,
) -> None:
    """Agrega una fila a la bitacora de auditoria (append-only)."""
    with get_session() as session:
        session.add(
            SyncLog(
                entidad=entidad,
                id_origen=str(id_origen) if id_origen is not None else None,
                accion=accion,
                resultado=resultado,
                detalle=detalle,
            )
        )
