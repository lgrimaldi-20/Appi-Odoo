"""
Cliente HTTP de la API externa de Smartier (solo lectura).

Smartier es el sistema de gestion del cliente (produccion y logistica). Su API
externa expone GETs paginados; NO permite escribir, asi que el middleware solo
lee de aqui y lleva el control de lo sincronizado en su propia base.

Contrato observado contra la API real (2026-08-21):

  - Autenticacion: header X-Api-Key con el secret de la key.
  - Sobre de respuesta de los listados: {"Data": [...], "Count": N}
    (OJO: no es {"Items", "TotalCount"} como sugieren otros ejemplos).
  - Paginacion: Page (base 1) y PageSize (por defecto 50, MAXIMO 200).
  - Orden: Sort en formato JSON:API, p.ej. "-FechaReferencia,Id".
  - Rate limit: 5 solicitudes por segundo POR KEY -> 429 si se supera.
  - Trazabilidad: X-Correlation-Id; si no se envia, la API genera uno y lo
    devuelve en la respuesta. Se registra en el log para poder reportar
    incidencias a soporte de Smartier con ese identificador.

El limite de 5 req/s se respeta con un espaciado minimo entre llamadas; ante un
429 se reintenta con espera creciente (respetando Retry-After si viene).
"""

import logging
import os
import threading
import time
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger("api-odoo")

# Limite documentado: 5 req/s por key. Se deja margen (4 req/s) para absorber
# el jitter de red y no rozar el limite.
_REQ_POR_SEGUNDO = float(os.getenv("SMARTIER_REQ_POR_SEG", "4"))
_ESPACIADO_MIN = 1.0 / _REQ_POR_SEGUNDO if _REQ_POR_SEGUNDO > 0 else 0.0

# Tope duro de la API: PageSize no puede superar 200.
PAGE_SIZE_MAX = 200


class SmartierError(Exception):
    """Fallo al hablar con la API de Smartier (red, auth, formato o 4xx/5xx)."""

    def __init__(self, mensaje: str, status: int | None = None,
                 correlation_id: str | None = None):
        super().__init__(mensaje)
        self.status = status
        self.correlation_id = correlation_id


class SmartierClient:
    """
    Cliente de solo lectura sobre la API externa de Smartier.

    Uso:
        cli = SmartierClient()                       # lee la config del entorno
        for cliente in cli.paginar("/external/clientes"):
            ...
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        self.base_url = (base_url or os.getenv("SMARTIER_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("SMARTIER_API_KEY", "")
        self.timeout = timeout

        if not self.base_url or not self.api_key:
            raise SmartierError(
                "Smartier no configurado: define SMARTIER_BASE_URL y "
                "SMARTIER_API_KEY en el entorno."
            )

        self._sesion = requests.Session()
        self._sesion.headers.update({
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        })
        # Momento de la ultima llamada, para espaciar y respetar el rate limit.
        self._ultima_llamada = 0.0
        self._cerrojo = threading.Lock()

    # -- rate limiting ------------------------------------------------------

    def _esperar_turno(self) -> None:
        """Espacia las llamadas para no superar el limite de la API."""
        with self._cerrojo:
            transcurrido = time.monotonic() - self._ultima_llamada
            if transcurrido < _ESPACIADO_MIN:
                time.sleep(_ESPACIADO_MIN - transcurrido)
            self._ultima_llamada = time.monotonic()

    # -- peticion base ------------------------------------------------------

    def get(
        self,
        ruta: str,
        params: Optional[dict] = None,
        reintentos: int = 3,
    ) -> Any:
        """
        GET contra la API. Devuelve el cuerpo ya deserializado.

        Reintenta ante 429 (rate limit) y 5xx transitorios, con espera creciente.
        Lanza SmartierError en cualquier otro fallo.
        """
        url = f"{self.base_url}{ruta}"
        espera = 1.0

        for intento in range(1, reintentos + 1):
            self._esperar_turno()
            try:
                r = self._sesion.get(url, params=params or {}, timeout=self.timeout)
            except requests.RequestException as e:
                if intento == reintentos:
                    raise SmartierError(f"Error de red llamando a {ruta}: {e}") from e
                time.sleep(espera)
                espera *= 2
                continue

            correlation = r.headers.get("X-Correlation-Id")

            # 429: rate limit. Se respeta Retry-After si la API lo indica.
            if r.status_code == 429:
                if intento == reintentos:
                    raise SmartierError(
                        f"Rate limit de Smartier agotado en {ruta} tras "
                        f"{reintentos} intentos.",
                        status=429, correlation_id=correlation,
                    )
                pausa = float(r.headers.get("Retry-After") or espera)
                logger.warning(
                    "SMARTIER_429 | ruta=%s intento=%s espera=%ss correlation=%s",
                    ruta, intento, pausa, correlation,
                )
                time.sleep(pausa)
                espera *= 2
                continue

            # 5xx: puede ser transitorio.
            if r.status_code >= 500:
                if intento == reintentos:
                    raise SmartierError(
                        f"Smartier devolvio {r.status_code} en {ruta}.",
                        status=r.status_code, correlation_id=correlation,
                    )
                time.sleep(espera)
                espera *= 2
                continue

            # 401/403: credenciales o permisos de la key. No se reintenta.
            if r.status_code in (401, 403):
                raise SmartierError(
                    f"Smartier rechazo la API Key en {ruta} ({r.status_code}). "
                    "Revisa que la key este activa y tenga habilitado ese recurso.",
                    status=r.status_code, correlation_id=correlation,
                )

            if not r.ok:
                raise SmartierError(
                    f"Smartier devolvio {r.status_code} en {ruta}: {r.text[:200]}",
                    status=r.status_code, correlation_id=correlation,
                )

            try:
                return r.json()
            except ValueError as e:
                raise SmartierError(
                    f"Respuesta no-JSON de Smartier en {ruta}.",
                    status=r.status_code, correlation_id=correlation,
                ) from e

        # Inalcanzable: el bucle siempre sale por return o raise.
        raise SmartierError(f"No se pudo completar la llamada a {ruta}.")

    # -- listados paginados -------------------------------------------------

    @staticmethod
    def _extraer_datos(cuerpo: Any) -> tuple[list, int | None]:
        """
        Saca (filas, total) del sobre de un listado.

        La API real devuelve {"Data": [...], "Count": N}. Se aceptan tambien
        otras formas habituales por si algun endpoint difiere.
        """
        if isinstance(cuerpo, list):
            return cuerpo, len(cuerpo)
        if not isinstance(cuerpo, dict):
            return [], None
        for clave in ("Data", "Items", "Results", "data", "items"):
            if isinstance(cuerpo.get(clave), list):
                total = cuerpo.get("Count", cuerpo.get("TotalCount", cuerpo.get("Total")))
                return cuerpo[clave], total
        return [], None

    def listar(
        self,
        ruta: str,
        page: int = 1,
        page_size: int = 100,
        sort: Optional[str] = None,
        filtros: Optional[dict] = None,
    ) -> tuple[list, int | None]:
        """Devuelve (filas, total) de UNA pagina del listado."""
        params = dict(filtros or {})
        params["Page"] = page
        params["PageSize"] = min(page_size, PAGE_SIZE_MAX)
        if sort:
            params["Sort"] = sort
        return self._extraer_datos(self.get(ruta, params))

    def paginar(
        self,
        ruta: str,
        page_size: int = 100,
        sort: Optional[str] = None,
        filtros: Optional[dict] = None,
        max_paginas: int = 1000,
    ) -> Iterator[dict]:
        """
        Recorre TODAS las paginas de un listado y va cediendo las filas.

        Corta cuando una pagina viene vacia, cuando se alcanza el total que
        declara la API, o al llegar a max_paginas (tope de seguridad para no
        entrar en un bucle infinito si la API pagina de forma inesperada).
        """
        page_size = min(page_size, PAGE_SIZE_MAX)
        vistos = 0
        for page in range(1, max_paginas + 1):
            filas, total = self.listar(ruta, page, page_size, sort, filtros)
            if not filas:
                return
            for fila in filas:
                yield fila
            vistos += len(filas)
            # Ultima pagina: vino incompleta o ya se leyo todo lo que hay.
            if len(filas) < page_size:
                return
            if isinstance(total, int) and vistos >= total:
                return
        logger.warning(
            "SMARTIER_PAGINACION | %s supero max_paginas=%s; puede faltar data.",
            ruta, max_paginas,
        )

    # -- atajos de conveniencia --------------------------------------------

    def obtener(self, ruta: str, id_recurso: Any) -> Any:
        """GET del detalle de un recurso concreto."""
        return self.get(f"{ruta}/{id_recurso}")

    def close(self) -> None:
        self._sesion.close()


def smartier_habilitado() -> bool:
    """True si hay configuracion de Smartier (URL + key) en el entorno."""
    return bool(os.getenv("SMARTIER_BASE_URL") and os.getenv("SMARTIER_API_KEY"))
