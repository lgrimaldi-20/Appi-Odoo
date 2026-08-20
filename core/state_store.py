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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.models_db import Base, EstadoSync, SyncLog, SyncMap

# ---------------------------------------------------------------------------
# Engine / Session
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./control.db")

_es_sqlite = DATABASE_URL.startswith("sqlite")

# check_same_thread=False solo aplica a SQLite (FastAPI usa varios hilos).
_connect_args = {"check_same_thread": False} if _es_sqlite else {}

# SQLite serializa las escrituras con un lock de fichero: bajo concurrencia
# devuelve "database is locked" en vez de esperar. timeout le dice que espere.
if _es_sqlite:
    _connect_args["timeout"] = 15

# Pool solo para bases reales (SQLite usa SingletonThreadPool/NullPool y
# rechaza estos argumentos). pool_pre_ping descarta conexiones muertas, lo que
# importa con PostgreSQL gestionado (RDS corta las conexiones ociosas).
_pool_kwargs = {} if _es_sqlite else {
    "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}

engine = create_engine(
    DATABASE_URL, connect_args=_connect_args, future=True, **_pool_kwargs
)
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
        # Se conserva el mensaje para estados que llevan detalle (ERROR, ELIMINADO);
        # en el resto se limpia.
        mapa.error = error if estado in (EstadoSync.ERROR, EstadoSync.ELIMINADO) else None


def ya_procesado(entidad: str, id_origen: str) -> Optional[int]:
    """
    Atajo de idempotencia: si (entidad, id_origen) ya esta PROCESADO, devuelve
    su id_odoo; en cualquier otro caso devuelve None.
    """
    mapa = buscar_mapeo(entidad, id_origen)
    if mapa and mapa.estado == EstadoSync.PROCESADO.value:
        return mapa.id_odoo
    return None


class ReservaOcupada(Exception):
    """
    Otro proceso ya tiene reservado (entidad, id_origen) y lo esta procesando.

    Se usa para cortar la CARRERA de idempotencia: sin esto, dos peticiones
    simultaneas con el mismo id_origen leen "no procesado" a la vez y ambas
    crean el registro en Odoo (duplicado contable real, observado en pruebas).
    """
    def __init__(self, entidad: str, id_origen: str, estado: str = ""):
        self.entidad = entidad
        self.id_origen = id_origen
        self.estado = estado
        super().__init__(
            f"El registro ({entidad}, {id_origen}) ya esta reservado por otro "
            f"proceso (estado={estado or 'PROCESANDO'})."
        )


def reservar(
    entidad: str,
    id_origen: str,
    model_odoo: Optional[str] = None,
    hash_payload: Optional[str] = None,
) -> Optional[int]:
    """
    Reserva ATOMICA de (entidad, id_origen) antes de tocar Odoo.

    Es el candado de la idempotencia: se apoya en la UniqueConstraint
    (entidad, id_origen) de sync_map, de modo que el arbitro es la base de
    datos y no una comprobacion previa en Python (que deja una ventana de
    carrera entre el "compruebo" y el "actuo").

    Devuelve:
      - el id_odoo, si el registro YA estaba PROCESADO (idempotente, no hay
        nada que hacer).
      - None, si la reserva se tomo con exito y hay que procesar.

    Lanza ReservaOcupada si otro proceso lo tiene en PROCESANDO, o si dos
    inserciones compiten y esta pierde (IntegrityError).
    """
    # Camino rapido: ya procesado -> ni siquiera hace falta reservar.
    ya = ya_procesado(entidad, id_origen)
    if ya is not None:
        return ya

    id_origen = str(id_origen)
    try:
        with get_session() as session:
            mapa = (
                session.query(SyncMap)
                .filter_by(entidad=entidad, id_origen=id_origen)
                .one_or_none()
            )
            if mapa is None:
                # INSERT: si otro proceso inserta a la vez, uno de los dos
                # recibe IntegrityError por la UniqueConstraint. Ese pierde.
                mapa = SyncMap(
                    entidad=entidad, id_origen=id_origen,
                    model_odoo=model_odoo, hash_payload=hash_payload,
                    estado=EstadoSync.PROCESANDO.value,
                )
                session.add(mapa)
                session.flush()
                return None

            # Ya existia: solo se puede retomar si NO esta en curso.
            if mapa.estado == EstadoSync.PROCESADO.value:
                return mapa.id_odoo
            if mapa.estado == EstadoSync.PROCESANDO.value:
                raise ReservaOcupada(entidad, id_origen, mapa.estado)

            # PENDIENTE / ERROR / ELIMINADO -> se puede reintentar.
            mapa.estado = EstadoSync.PROCESANDO.value
            if model_odoo is not None:
                mapa.model_odoo = model_odoo
            if hash_payload is not None:
                mapa.hash_payload = hash_payload
            mapa.error = None
            return None
    except IntegrityError as e:
        # Perdio la carrera del INSERT: el ganador lo esta procesando.
        raise ReservaOcupada(entidad, id_origen) from e


def reservar_estricto(
    entidad: str,
    id_origen: str,
    model_odoo: Optional[str] = None,
    hash_payload: Optional[str] = None,
) -> Optional[SyncMap]:
    """
    Variante de reservar() para entidades cuyo id_odoo puede ser legitimamente
    None (p.ej. "conciliacion", que no tiene un unico id en Odoo).

    Devuelve el SyncMap previo si YA estaba PROCESADO (nada que hacer), o None
    si la reserva se tomo y hay que procesar. Lanza ReservaOcupada igual que
    reservar().
    """
    id_origen = str(id_origen)
    try:
        with get_session() as session:
            mapa = (
                session.query(SyncMap)
                .filter_by(entidad=entidad, id_origen=id_origen)
                .one_or_none()
            )
            if mapa is None:
                session.add(SyncMap(
                    entidad=entidad, id_origen=id_origen,
                    model_odoo=model_odoo, hash_payload=hash_payload,
                    estado=EstadoSync.PROCESANDO.value,
                ))
                session.flush()
                return None

            if mapa.estado == EstadoSync.PROCESADO.value:
                session.expunge(mapa)
                return mapa
            if mapa.estado == EstadoSync.PROCESANDO.value:
                raise ReservaOcupada(entidad, id_origen, mapa.estado)

            mapa.estado = EstadoSync.PROCESANDO.value
            if model_odoo is not None:
                mapa.model_odoo = model_odoo
            if hash_payload is not None:
                mapa.hash_payload = hash_payload
            mapa.error = None
            return None
    except IntegrityError as e:
        raise ReservaOcupada(entidad, id_origen) from e


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
