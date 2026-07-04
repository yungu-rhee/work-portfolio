"""
  SIPS CONSUMOS LOADER - GAS
  ==========================
  ETL que extrae consumos de gas desde una API SIPS y los carga
  en una base de datos SQL Server mediante MERGE (upsert).

  Arquitectura:
  1. Conexión a SQL Server (credenciales via keyring).
  2. Obtención de CUPS activos mediante query configurable.
  3. Autenticación contra API SIPS (token Bearer).
  4. Iteración por CUPS con llamadas a la API (LoadConsumos=True).
  5. Parseo del JSON de respuesta y MERGE en tabla destino.

  Características:
  - Reintentos automáticos con backoff por CUPS.
  - Renovación automática de token si expira (HTTP 401).
  - Commits por lotes para equilibrar seguridad y rendimiento.
  - Logging completo a consola y fichero.

  Ver README.md para la estructura de entrada/salida esperada.
"""

import http.client
import json
import logging
import time
import sys
from datetime import datetime

import keyring
import pyodbc

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================

# --- API ---
API_HOST = "your-api-host.com"
API_AUTH_URL = "/api/v1/Usuario/Token"
API_AUTH_HEADERS = {
    'User': '',       # Base64-encoded
    'Password': '',   # Base64-encoded
}
API_GAS_URL = "/api/v1/SIPS/GAS/GetClientesPost"

# --- Base de datos ---
SQL_SERVER = 'your-server.database.windows.net'
SQL_DATABASE = 'Your_Database'
SQL_DRIVER = '{ODBC Driver 17 for SQL Server}'

# --- Comportamiento ---
DELAY_BETWEEN_CALLS_SECONDS = 0.5  # Pausa entre llamadas API por CUPS --> PARA NO SATURARLO
REQUEST_TIMEOUT_SECONDS = 60       # Timeout para las llamadas API --> EN CASO DE QUE ESTE CAIDO 
MAX_RETRIES = 3                    # Reintentos por CUPS en caso de error API --> NUMERO MAX DE INTENTOS EN CASO DE ERROR DE LLAMAD A LA API
RETRY_DELAY_SECONDS = 5            # Espera entre reintentos 
BATCH_COMMIT_SIZE = 100            # Commit cada N CUPS procesados  --> menor numero == mas seguridad pero menos eficientes

# --- Queries (adaptar a tu esquema) ---
# Ver README.md para la estructura esperada de las tablas

QUERY_CUPS_ACTIVOS = """
    -- Query que devuelve una columna con los códigos CUPS activos de gas.
    -- Debe filtrar por estado del contrato y tipo de suministro (gas).
    -- Ejemplo: SELECT cups_codigo FROM contratos WHERE estado IN (...) AND tipo = 'gas'
"""

MERGE_CONSUMOS_SQL = """
    -- MERGE (upsert) que inserta o actualiza los consumos de gas.
    -- Clave primaria: CUPS + fecha inicio.
    -- Parámetros (en orden): cups, fec_ini, fec_fin, peaje,
    --   consumo_p1, consumo_p2, caudal_medio, caudal_min, caudal_max,
    --   porcentaje_nocturno, tipo_lectura
    -- Ver README.md para el mapeo completo de campos API -> columnas.
"""

# ==============================================================================
# LOGGING --> DEPURACION DE ERRORES
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            f"sips_loader_gas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# CONEXIÓN A BASE DE DATOS --> CONEXION A LA BASE DE DATOS
# ==============================================================================

def get_db_connection():
    """Abre y devuelve una conexión a SQL Server usando keyring."""
    sql_cred = keyring.get_credential("sql", None)
    if sql_cred is None:
        raise RuntimeError("No se encontraron credenciales en keyring.")
    
    conn_str = (
        f'DRIVER={SQL_DRIVER};'
        f'SERVER={SQL_SERVER};'
        f'DATABASE={SQL_DATABASE};'
        f'UID={sql_cred.username};'
        f'PWD={sql_cred.password};'
        f'Encrypt=yes;'
        f'TrustServerCertificate=no;'
        f'Connection Timeout=30;'
    )
    conn = pyodbc.connect(conn_str)
    logger.info("Conexión a SQL Server establecida.")
    return conn

# ==============================================================================
# OBTENCIÓN DE CUPS ACTIVOS --> CONSULTA SQL PARA TENER LOS CUPS ACTIVOS
# ==============================================================================

def get_cups_gas(cursor):
    """Recupera CUPS activos de Gas ejecutando la query configurada."""
    cursor.execute(QUERY_CUPS_ACTIVOS)
    cups_list = [row[0] for row in cursor.fetchall()]
    logger.info(f"CUPS Gas activos encontrados: {len(cups_list)}")
    return cups_list

# ==============================================================================
# AUTENTICACIÓN API --> OBTENER EL TOKEN 
# ==============================================================================

def get_api_token():
    """Obtiene token de autenticación de la API SIPS."""
    conn_api = http.client.HTTPSConnection(API_HOST, timeout=REQUEST_TIMEOUT_SECONDS)
    conn_api.request(
        method="GET",
        url=API_AUTH_URL,
        body='',
        headers=API_AUTH_HEADERS
    )
    response = conn_api.getresponse()
    data = response.read()
    conn_api.close()

    if response.status != 200:
        raise RuntimeError(
            f"Error obteniendo token: HTTP {response.status} - {data.decode('utf-8')}"
        )

    token = data.decode("utf-8").replace('"', '')
    logger.info("Token de API obtenido correctamente.")
    return token

# ==============================================================================
# LLAMADAS A LA API
# ==============================================================================

def build_payload(cups_code):
    """Construye el payload dinámico para un CUPS dado."""
    return json.dumps({
        "CodigoCUPS": cups_code,
        "NombreEmpresaDistribuidora": "",
        "MunicipioPS": "",
        "CodigoProvinciaPS": "",
        "CodigoPostalPS": "",
        "CodigoTarifaATREnVigor": "",
        "ListCUPS": "",
        "LoadAllDatosCliente": True,
        "LoadConsumos": True,
        "IsExist": True
    })

def call_api(token, url, cups_code):
    """
    Llama a la API SIPS para un CUPS dado.
    Devuelve el JSON parseado o None si falla tras los reintentos.
    """
    headers = {
        'Authorization': token,
        'Content-Type': 'application/json',
    }
    payload = build_payload(cups_code)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn_api = http.client.HTTPSConnection(
                API_HOST, timeout=REQUEST_TIMEOUT_SECONDS
            )
            conn_api.request(
                method="POST",
                url=url,
                body=payload,
                headers=headers
            )
            response = conn_api.getresponse()
            data = response.read()
            conn_api.close()

            if response.status == 401:
                logger.warning(f"  [{cups_code}] Token expirado (HTTP 401).")
                return "TOKEN_EXPIRED"

            if response.status != 200:
                logger.warning(
                    f"  [{cups_code}] Intento {attempt}/{MAX_RETRIES}: "
                    f"HTTP {response.status}"
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue

            result = json.loads(data.decode("utf-8"))
            return result

        except Exception as e:
            logger.warning(
                f"  [{cups_code}] Intento {attempt}/{MAX_RETRIES}: "
                f"Excepción - {e}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"  [{cups_code}] Fallido tras {MAX_RETRIES} intentos.")
    return None

# ==============================================================================
# PROCESAMIENTO E INSERCIÓN - GAS
# ==============================================================================

def parse_date(date_str):
    """Convierte una fecha ISO del JSON a un objeto date. Devuelve None si es null."""
    if date_str is None:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '')).date()
    except (ValueError, AttributeError):
        return None

def safe_numeric(value):
    """Convierte a float preservando decimales, devuelve 0 si es None."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0

def process_gas_consumos(cursor, cups_code, json_data):
    """
    Procesa los consumos de gas de un CUPS y los inserta/actualiza
    en la tabla destino mediante MERGE.
    Devuelve el número de registros procesados.

    Campos extraídos del JSON de la API SIPS (Gas):
      - FechaInicioMesConsumo / FechaFinMesConsumo  --> fechas del periodo
      - CodigoTarifaPeaje                           --> tarifa de peaje
      - ConsumoEnWhP1, ConsumoEnWhP2                --> consumos por periodo
      - CaudalMedioEnWhDia                          --> caudal medio diario
      - CaudalMinimoDiario, CaudalMaximoDiario      --> caudales min/max
      - PorcentajeConsumoNocturno                   --> % nocturno
      - CodigoTipoLectura                           --> R (Real) o E (Estimada)
    """
    consumos = json_data.get("ConsumosSips")
    if not consumos:
        return 0

    count = 0
    for c in consumos:
        # Para Gas, usamos FechaInicioMesConsumo / FechaFinMesConsumo
        fec_ini = parse_date(c.get("FechaInicioMesConsumo"))
        fec_fin = parse_date(c.get("FechaFinMesConsumo"))

        if fec_ini is None:
            continue  # Sin fecha inicio, no podemos insertar (es PK)

        # Tipo lectura es varchar(1), CodigoTipoLectura puede ser "R" o "E"
        tipo_lectura = c.get("CodigoTipoLectura")
        if tipo_lectura and len(tipo_lectura) > 1:
            tipo_lectura = tipo_lectura[:1]

        params = (
            cups_code,                                      # CUPS
            fec_ini,                                        # Fecha inicio
            fec_fin,                                        # Fecha fin
            c.get("CodigoTarifaPeaje"),                     # Peaje
            safe_numeric(c.get("ConsumoEnWhP1")),           # Consumo P1
            safe_numeric(c.get("ConsumoEnWhP2")),           # Consumo P2
            safe_numeric(c.get("CaudalMedioEnWhDia")),      # Caudal medio
            safe_numeric(c.get("CaudalMinimoDiario")),      # Caudal mínimo
            safe_numeric(c.get("CaudalMaximoDiario")),      # Caudal máximo
            safe_numeric(c.get("PorcentajeConsumoNocturno")), # % nocturno
            tipo_lectura,                                    # Tipo lectura
        )

        cursor.execute(MERGE_CONSUMOS_SQL, params)
        count += 1

    return count

# ==============================================================================
# PROGRAMA PRINCIPAL
# ==============================================================================

def process_sector(cursor, conn_db, token, cups_list, api_url, sector_name, process_func):
    """
    Procesa un sector (Power o Gas) completo:
      - Itera sobre los CUPS
      - Llama a la API
      - Procesa e inserta los consumos
      - Hace commit por lotes
    """
    total_cups = len(cups_list)
    total_registros = 0
    cups_ok = 0
    cups_error = 0
    cups_sin_datos = 0

    logger.info(f"--- Inicio procesamiento {sector_name}: {total_cups} CUPS ---")

    for idx, cups_code in enumerate(cups_list, 1):
        if idx % 50 == 0 or idx == 1:
            logger.info(f"  {sector_name}: Procesando CUPS {idx}/{total_cups}...")

        # Llamada a la API
        json_data = call_api(token, api_url, cups_code)

        # Renovar token si expiró
        if json_data == "TOKEN_EXPIRED":
            logger.info("Renovando token de API...")
            token = get_api_token()
            json_data = call_api(token, api_url, cups_code)
            if json_data == "TOKEN_EXPIRED":
                logger.error(f"  [{cups_code}] Token recién renovado sigue dando 401.")
                cups_error += 1
                continue

        if json_data is None:
            cups_error += 1
            continue

        # Procesar consumos
        try:
            count = process_func(cursor, cups_code, json_data)
            if count > 0:
                total_registros += count
                cups_ok += 1
            else:
                cups_sin_datos += 1
        except Exception as e:
            logger.error(f"  [{cups_code}] Error procesando consumos: {e}")
            cups_error += 1
            # Rollback parcial por si hay datos inconsistentes del CUPS actual
            # No hacemos rollback completo para no perder los anteriores
            continue

        # Commit por lotes
        if idx % BATCH_COMMIT_SIZE == 0:
            conn_db.commit()
            logger.info(f"  {sector_name}: Commit parcial en CUPS {idx}/{total_cups}")

        # Pausa entre llamadas
        time.sleep(DELAY_BETWEEN_CALLS_SECONDS)

    # Commit final
    conn_db.commit()

    logger.info(
        f"--- Fin {sector_name}: "
        f"OK={cups_ok}, Sin datos={cups_sin_datos}, Error={cups_error}, "
        f"Registros insertados/actualizados={total_registros} ---"
    )

    return cups_ok, cups_sin_datos, cups_error, total_registros

def main():
    """Punto de entrada principal del proceso."""
    start_time = datetime.now()
    logger.info("=" * 70)
    logger.info("INICIO PROCESO SIPS CONSUMOS LOADER - GAS")
    logger.info(f"Fecha/hora: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    conn_db = None
    try:
        # 1. Conexión a base de datos
        conn_db = get_db_connection()
        cursor = conn_db.cursor()

        # 2. Obtener token API
        token = get_api_token()

        # 3. Procesar GAS
        cups_gas = get_cups_gas(cursor)
        if cups_gas:
            process_sector(
                cursor, conn_db, token,
                cups_gas, API_GAS_URL,
                "GAS", process_gas_consumos
            )
        else:
            logger.warning("No se encontraron CUPS activos de Gas.")

    except Exception as e:
        logger.critical(f"Error crítico en el proceso: {e}", exc_info=True)
        if conn_db:
            conn_db.rollback()
        sys.exit(1)

    finally:
        if conn_db:
            conn_db.close()
            logger.info("Conexión a SQL Server cerrada.")

    end_time = datetime.now()
    elapsed = end_time - start_time
    logger.info("=" * 70)
    logger.info(f"FIN PROCESO - Duración: {elapsed}")
    logger.info("=" * 70)

if __name__ == "__main__":
    main()