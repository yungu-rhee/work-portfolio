"""
MIDDLEWARE ERP → BUSINESS CENTRAL: Documentos Contables — STEP 2 (Procesamiento)
===================================================================================
Lee facturas con IsExportado = 0 (Step 1 OK) y lanza el procesamiento en BC.
Si OK: marca IsExportado = 1.
Si falla: no toca IsExportado, vuelca el error a archivo.
"""

import requests
import json
import sys
import pyodbc
import keyring
import logging
from datetime import datetime

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename=f"erp_bc_accdoc_step2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)
log = logging.getLogger()

console = logging.StreamHandler()
console.setLevel(logging.INFO)
log.addHandler(console)

# ============================================================
# CONFIGURACIÓN BC
# ============================================================

BC_CONFIG = {
    "tenant_id": "your-tenant-id",
    "client_id": "your-client-id",
    "client_secret": "your-client-secret",
    "scope": "https://api.businesscentral.dynamics.com/.default",
    "environment": "YOUR_ENVIRONMENT",
    "company_name": "Your Company Name",
}

# ============================================================
# CONFIGURACIÓN ERP ORIGEN (SQL Server)
# ============================================================

ERP_SERVER = "your-erp-server"
ERP_DB = "Your_ERP_Database"

# ============================================================
# ARCHIVO DE ERRORES
# ============================================================

ERROR_FILE = f"erp_bc_accdoc_step2_errores_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

def guardar_error(id_factura, package_id, bc_id, status_code, mensaje):
    """Añade un error al archivo JSON de errores."""
    try:
        with open(ERROR_FILE, "r", encoding="utf-8") as f:
            errores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        errores = []

    errores.append({
        "fecha": datetime.now().isoformat(),
        "IdFactura": id_factura,
        "packageID": package_id,
        "id": bc_id,
        "status_code": status_code,
        "mensaje": mensaje,
    })

    with open(ERROR_FILE, "w", encoding="utf-8") as f:
        json.dump(errores, f, indent=2, ensure_ascii=False)

# ============================================================
# CONEXIÓN AL ERP
# ============================================================

def conectar_erp():
    """Conecta al SQL Server del ERP usando credenciales de keyring."""
    log.info("Conectando al ERP...")

    sql = keyring.get_credential("sql_erp", None)

    conn_str = (
        f'DRIVER={{ODBC Driver 17 for SQL Server}};'
        f'SERVER={ERP_SERVER};'
        f'DATABASE={ERP_DB};'
        f'UID={sql.username};'
        f'PWD={sql.password};'
        f'Encrypt=yes;'
        f'TrustServerCertificate=yes;'
        f'Connection Timeout=30;'
    )

    conn = pyodbc.connect(conn_str)
    log.info("Conexión al ERP correcta")
    return conn

# ============================================================
# OBTENER DOCUMENTOS PENDIENTES DE STEP 2
# ============================================================

def obtener_documentos_pendientes_step2(conn):
    """
    Obtiene facturas con IsExportado = 0 (Step 1 OK, Step 2 pendiente).
    Lee el packageID y el id guardados en Step 1.

    Query esperada: devuelve IdFactura, bc_id, bc_packageID.
    Ver README.md para detalle.
    """
    cursor = conn.cursor()

    query = """
        -- Query que devuelve facturas pendientes de Step 2.
        -- Filtro: IsExportado = 0, bc_id y bc_packageID no nulos.
        -- Columnas: IdFactura, bc_id, bc_packageID
    """

    cursor.execute(query)
    columns = [col[0] for col in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    log.info(f"Documentos pendientes de Step 2: {len(rows)}")
    return rows

# ============================================================
# OBTENER TOKEN OAUTH2
# ============================================================

def obtener_token():
    """Pide un token OAuth2 a Azure AD."""
    url = f"https://login.microsoftonline.com/{BC_CONFIG['tenant_id']}/oauth2/v2.0/token"

    payload = {
        "grant_type": "client_credentials",
        "client_id": BC_CONFIG["client_id"],
        "client_secret": BC_CONFIG["client_secret"],
        "scope": BC_CONFIG["scope"],
    }

    resp = requests.post(url, data=payload)
    resp.raise_for_status()

    token = resp.json()["access_token"]
    log.info("Token OAuth2 obtenido")
    return token

# ============================================================
# INDICAR PROCESAMIENTO EN BC (Step 2)
# ============================================================

def indicar_procesamiento(token, package_id, doc_id):
    """
    Indica a BC que procese el documento RAW via ODataV4 Nav.Copy.
    """
    base_url = "https://api.businesscentral.dynamics.com/v2.0"
    env = BC_CONFIG["environment"]
    company_name = BC_CONFIG["company_name"].replace(" ", "%20")

    url = (
        f"{base_url}/{env}/ODataV4/Company('{company_name}')"
        f"/RawExtDocHeadAPI('{package_id}',{doc_id})/Nav.Copy"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json={})

    if resp.status_code in (200, 201, 204):
        log.info(f"Procesamiento OK — packageID: {package_id}, id: {doc_id}")
        return True
    else:
        log.error(f"Error en Step 2: {resp.status_code} — {resp.text}")
        return {"error": True, "status_code": resp.status_code, "mensaje": resp.text}

# ============================================================
# ACTUALIZAR ERP TRAS STEP 2
# ============================================================

def actualizar_erp_step2(conn, id_factura):
    """
    Marca IsExportado = 1 (Step 2 OK, completado).

    Query esperada (1 parámetro):
      1. IdFactura (cláusula WHERE)
    """
    cursor = conn.cursor()

    query_update = """
        -- UPDATE que marca la factura como completada (IsExportado = 1).
        -- Parámetro: (1) IdFactura
    """

    cursor.execute(query_update, (id_factura,))
    conn.commit()

    log.info(f"ERP actualizado (Step 2 OK) para documento: {id_factura}")

# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=" * 60)
    log.info("INICIO — Step 2: Procesamiento de Documentos en BC")
    log.info("=" * 60)

    try:
        # 1. Conectar al ERP
        conn = conectar_erp()

        # 2. Obtener documentos pendientes de Step 2
        documentos = obtener_documentos_pendientes_step2(conn)

        if not documentos:
            log.info("No hay documentos pendientes de Step 2. Fin.")
            conn.close()
            return

        # 3. Obtener token
        token = obtener_token()

        # 4. Procesar cada documento
        procesados = 0
        errores = 0

        for doc in documentos:
            id_factura = doc["IdFactura"]
            package_id = doc["bc_packageID"]
            bc_id = doc["bc_id"]
            log.info(f"Procesando documento: {id_factura} (packageID: {package_id}, id: {bc_id})")

            try:
                resultado = indicar_procesamiento(token, package_id, bc_id)

                if resultado is True:
                    actualizar_erp_step2(conn, id_factura)
                    procesados += 1
                else:
                    guardar_error(id_factura, package_id, bc_id, resultado["status_code"], resultado["mensaje"])
                    errores += 1

            except Exception as e:
                log.error(f"Error procesando {id_factura}: {e}")
                guardar_error(id_factura, package_id, bc_id, None, str(e))
                errores += 1

        conn.close()

        log.info("=" * 60)
        log.info(f"FIN — Procesados: {procesados}, Errores: {errores}")
        if errores > 0:
            log.info(f"Detalle de errores en: {ERROR_FILE}")
        log.info("=" * 60)

    except Exception as e:
        log.error(f"Error fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()