"""
MIDDLEWARE ERP → BUSINESS CENTRAL: Contrapartes (bciContacts)
Script diario para el programador de tareas.

Arquitectura:
1. Conecta al ERP origen (SQL Server) y obtiene clientes nuevos/modificados.
2. Obtiene token OAuth2 de Microsoft Entra ID.
3. Para cada cliente, mapea los campos y hace POST a la API de Business Central.
4. Verifica el estado del registro en BC con polling (reintentos).
5. Si BC confirma "Completed", actualiza el ERP origen con los IDs generados.

Características:
- Diferencia altas (N) de modificaciones (M) y actúa en consecuencia.
- Renovación automática de token OAuth2 antes de que expire (50 min).
- Polling con reintentos para esperar el procesamiento asíncrono de BC.
- Log de errores en JSON independiente para trazabilidad.
- Logging completo a consola y fichero.

Ver README.md para la estructura de entrada/salida esperada.
"""

import requests
import json
import sys
import pyodbc
import keyring
import logging
import time
from datetime import datetime

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename=f"middleware_ctpy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)
log = logging.getLogger()

# También mostrar en consola
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
    "company_guid": "your-company-guid",
    "interface_type": "YOUR_INTERFACE_TYPE",
    "bc_company": "YOUR_BC_COMPANY_CODE",
}

# ============================================================
# CONFIGURACIÓN ERP ORIGEN (SQL Server)
# ============================================================

ERP_SERVER = "your-erp-server"
ERP_DB = "Your_ERP_Database"

# ============================================================
# ARCHIVO DE ERRORES
# ============================================================

ERROR_FILE = f"middleware_ctpy_errores_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

def guardar_error(id_cliente, tipo, fase, status_code, mensaje):
    """Añade un error al archivo JSON de errores."""
    try:
        with open(ERROR_FILE, "r", encoding="utf-8") as f:
            errores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        errores = []

    errores.append({
        "fecha": datetime.now().isoformat(),
        "IdCliente": id_cliente,
        "tipo": tipo,
        "fase": fase,
        "status_code": status_code,
        "mensaje": mensaje,
    })

    with open(ERROR_FILE, "w", encoding="utf-8") as f:
        json.dump(errores, f, indent=2, ensure_ascii=False)

# ============================================================
# QUERIES SQL
# ============================================================

# QUERY 1: Obtener clientes nuevos (N) y modificados (M)
# Debe devolver las columnas documentadas en el README.
# - Tipo 'N' (nuevo): clientes sin código externo asignado aún.
# - Tipo 'M' (modificación): clientes modificados desde la última ejecución.
QUERY_CLIENTES_PENDIENTES = """
    -- Query que devuelve clientes pendientes de sincronizar con BC.
    -- Debe devolver: TIPO (N/M), IdCliente, CodigoExternoBC, CodigoExterno1,
    --   contactName1, contactAddress1, contactCity, contactPostcode,
    --   contactCountryCode, contactCountyCode, contactEmailAddress,
    --   contactPhoneNo, contactVATRegNo
    -- Ver README.md para detalle de cada campo.
"""

# QUERY 2: Actualizar códigos externos tras alta exitosa en BC
QUERY_ACTUALIZAR_NUEVO = """
    -- UPDATE que graba el código de contacto asignado por BC y el requestID
    -- en el registro del cliente del ERP.
    -- Parámetros: (1) contactNo de BC, (2) requestID, (3) IdCliente
"""

# QUERY 3: Actualizar código externo tras modificación exitosa en BC
QUERY_ACTUALIZAR_MODIFICACION = """
    -- UPDATE que graba el requestID de la última modificación exitosa.
    -- Parámetros: (1) requestID, (2) IdCliente
"""

# ============================================================
# CONEXIÓN AL ERP ORIGEN
# ============================================================

def conectar_erp():
    log.info("Conectando al ERP origen...")

    sql = keyring.get_credential("sql_erp", None)

    conning = (
        f'DRIVER={{ODBC Driver 17 for SQL Server}};'
        f'SERVER={ERP_SERVER};'
        f'DATABASE={ERP_DB};'
        f'UID={sql.username};'
        f'PWD={sql.password};'
        f'Encrypt=yes;'
        f'TrustServerCertificate=yes;'
        f'Connection Timeout=30;'
    )

    conn = pyodbc.connect(conning)
    log.info("Conexion al ERP correcta")
    return conn

# ============================================================
# OBTENER CLIENTES PENDIENTES DEL ERP
# ============================================================

def obtener_clientes_pendientes(conn):
    log.info("Consultando clientes pendientes...")

    cursor = conn.cursor()
    cursor.execute(QUERY_CLIENTES_PENDIENTES)
    columns = [desc[0] for desc in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    log.info(f"Clientes encontrados: {len(rows)}")
    for row in rows:
        log.info(f"  - IdCliente={row.get('IdCliente')}, Tipo={row.get('TIPO')}")

    return rows

# ============================================================
# OBTENER TOKEN OAUTH2
# ============================================================

def obtener_token():
    log.info("Pidiendo token OAuth2...")

    url = f"https://login.microsoftonline.com/{BC_CONFIG['tenant_id']}/oauth2/v2.0/token"

    response = requests.post(url, data={
        "grant_type": "client_credentials",
        "client_id": BC_CONFIG["client_id"],
        "client_secret": BC_CONFIG["client_secret"],
        "scope": BC_CONFIG["scope"],
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})

    if response.status_code != 200:
        log.error(f"Error obteniendo token: {response.status_code} - {response.text}")
        raise Exception("No se pudo obtener el token OAuth2")

    token = response.json()["access_token"]
    log.info(f"Token recibido ({len(token)} chars)")
    return token

# ============================================================
# MAPEAR CLIENTE ERP → JSON BC
# ============================================================

def mapear_contraparte(row):
    """
    Transforma un registro del ERP al JSON que espera la API bciContacts de BC.

    Campos fijos: interfaceType, bcCompany, contactType, duplicationCheck, status.
    Campos de control: sourceSysRecId, bcOrigReqID, contactNo.
    Campos del cliente: name, address, city, postcode, country, county, email, phone, VAT.
    Template codes: contactCustTempCode, contactVendTempCode.

    Ver README.md para el mapeo completo.
    """
    es_modificacion = row.get("TIPO") == "M"

    json_bc = {
        # Campos fijos
        "interfaceType": BC_CONFIG["interface_type"],
        "bcCompany": BC_CONFIG["bc_company"],
        "contactType": "Company",
        "duplicationCheck": "As Non-Legal Entity",
        "status": "",

        # Control
        "sourceSysRecId": str(row.get("IdCliente", "")),
        "bcOrigReqID": int(row.get("CodigoExterno1", 0) or 0) if es_modificacion else 0,
        "contactNo": str(row.get("CodigoExternoBC", "") or "") if es_modificacion else "",

        # Datos del cliente
        "contactName1": str(row.get("contactName1", "") or ""),
        "contactName2": "",
        "contactAddress1": str(row.get("contactAddress1", "") or ""),
        "contactAddress2": "",
        "contactCity": str(row.get("contactCity", "") or ""),
        "contactPostcode": str(row.get("contactPostcode", "") or ""),
        "contactCountryCode": str(row.get("contactCountryCode", "ES") or "ES"),
        "contactCountyCode": str(row.get("contactCountyCode", "") or ""),
        "contactEmailAddress": str(row.get("contactEmailAddress", "") or ""),
        "contactPhoneNo": str(row.get("contactPhoneNo", "") or ""),
        "contactVATRegNo": str(row.get("contactVATRegNo", "") or ""),
        "contactLocalVATRegNo": "",
        "contactTradeRegNo": "",
        "contactSalespersonCode": "",

        # Template codes
        "contactCustTempCode": "YOUR_TEMPLATE_CODE",
        "contactVendTempCode": "",

        # No aplica para España
        "sirenNo": "",
    }

    return json_bc

# ============================================================
# ENVIAR CONTRAPARTE A BC
# ============================================================

def enviar_contraparte(token, json_bc):
    url = (
        f"https://api.businesscentral.dynamics.com/v2.0"
        f"/{BC_CONFIG['tenant_id']}/{BC_CONFIG['environment']}"
        f"/api/iTadviseAG/master_data/v2.0"
        f"/companies({BC_CONFIG['company_guid']})/bciContacts"
    )

    response = requests.post(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, json=json_bc)

    return response

# ============================================================
# CONSULTAR ESTADO EN BC (con reintentos)
# ============================================================

def consultar_estado(token, request_id, max_intentos=10, espera=10):
    url = (
        f"https://api.businesscentral.dynamics.com/v2.0"
        f"/{BC_CONFIG['tenant_id']}/{BC_CONFIG['environment']}"
        f"/api/iTadviseAG/master_data/v2.0"
        f"/companies({BC_CONFIG['company_guid']})/bciContacts"
        f"?$filter=requestID eq {request_id}"
    )

    for intento in range(1, max_intentos + 1):
        try:
            response = requests.get(url, headers={
                "Authorization": f"Bearer {token}",
            })

            if response.status_code == 200:
                data = response.json()
                values = data.get("value", [])
                if values:
                    status = values[0].get("status")
                    if status != "In_x0020_Process":
                        return {"ok": True, "status": status, "contactNo": values[0].get("contactNo", ""), "response": response}
                    log.info(f"  Intento {intento}/{max_intentos}: status=In Process, esperando {espera}s...")
            else:
                log.warning(f"  Intento {intento}/{max_intentos}: GET falló con {response.status_code}")

        except Exception as e:
            log.warning(f"  Intento {intento}/{max_intentos}: Error en GET: {e}")

        time.sleep(espera)

    return {"ok": False, "status": "Timeout", "contactNo": "", "response": None}

# ============================================================
# ACTUALIZAR ERP TRAS ENVÍO EXITOSO
# ============================================================

def actualizar_nuevo(conn, id_cliente, contact_no, request_id):
    cursor = conn.cursor()
    cursor.execute(QUERY_ACTUALIZAR_NUEVO, (contact_no, str(request_id), id_cliente))
    conn.commit()
    log.info(f"  ERP actualizado (NUEVO): IdCliente={id_cliente} -> CodigoExternoBC={contact_no}, CodigoExterno1={request_id}")

def actualizar_modificacion(conn, id_cliente, request_id):
    cursor = conn.cursor()
    cursor.execute(QUERY_ACTUALIZAR_MODIFICACION, (str(request_id), id_cliente))
    conn.commit()
    log.info(f"  ERP actualizado (MODIFICACION): IdCliente={id_cliente} -> CodigoExterno1={request_id}")

# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("INICIO: Middleware ERP -> BC (Contrapartes)")
    log.info(f"Fecha: {datetime.now().isoformat()}")
    log.info("=" * 60)

    try:
        # Paso 1: Conectar al ERP
        conn = conectar_erp()

        # Paso 2: Obtener clientes pendientes
        clientes = obtener_clientes_pendientes(conn)

        if not clientes:
            log.info("No hay clientes pendientes. Fin.")
            conn.close()
            sys.exit(0)

        # Paso 3: Obtener token
        token = obtener_token()
        token_time = datetime.now()

        # Procesar cada cliente
        resultados = {"completado": 0, "error": 0}

        for row in clientes:
            id_cliente = row.get("IdCliente", "?")
            tipo = row.get("TIPO", "?")

            # Renovar token si lleva más de 50 minutos
            if (datetime.now() - token_time).total_seconds() > 3000:
                log.info("Token a punto de caducar, renovando...")
                token = obtener_token()
                token_time = datetime.now()

            log.info(f"Procesando IdCliente={id_cliente} (Tipo={tipo})...")

            try:
                # Paso 4: Mapear
                json_bc = mapear_contraparte(row)
                log.info(f"  JSON generado: {json.dumps(json_bc, ensure_ascii=False)[:200]}...")

                # Paso 5: Enviar
                response = enviar_contraparte(token, json_bc)
                log.info(f"  POST Status: {response.status_code}")

                if response.status_code not in (200, 201):
                    log.error(f"  FALLO en POST: {response.status_code} - {response.text[:500]}")
                    guardar_error(id_cliente, tipo, "POST", response.status_code, response.text[:500])
                    resultados["error"] += 1
                    continue

                data = response.json()
                request_id = data.get("requestID")
                log.info(f"  POST OK: requestID={request_id}")

                # Paso 6: Verificar estado con reintentos
                resultado = consultar_estado(token, request_id)

                if not resultado["ok"]:
                    log.error(f"  Timeout esperando procesamiento de BC para requestID={request_id}")
                    guardar_error(id_cliente, tipo, "GET_TIMEOUT", None, f"BC no completó tras reintentos. requestID={request_id}")
                    resultados["error"] += 1
                    continue

                final_status = resultado["status"]
                final_contact_no = resultado["contactNo"]
                log.info(f"  Verificacion: status={final_status}, contactNo={final_contact_no}")

                if final_status != "Completed":
                    log.error(f"  BC devolvió status={final_status} para requestID={request_id}")
                    guardar_error(id_cliente, tipo, "STATUS", None, f"BC status={final_status}, contactNo={final_contact_no}, requestID={request_id}")
                    resultados["error"] += 1
                    continue

                # Paso 7: Actualizar ERP
                if tipo == "N":
                    if final_contact_no:
                        actualizar_nuevo(conn, id_cliente, final_contact_no, request_id)
                        resultados["completado"] += 1
                    else:
                        log.error(f"  BC completó pero no devolvió contactNo para requestID={request_id}")
                        guardar_error(id_cliente, tipo, "NO_CONTACT_NO", None, f"Completed pero contactNo vacío. requestID={request_id}")
                        resultados["error"] += 1
                elif tipo == "M":
                    actualizar_modificacion(conn, id_cliente, request_id)
                    resultados["completado"] += 1

            except Exception as e:
                log.error(f"  ERROR procesando IdCliente={id_cliente}: {e}")
                guardar_error(id_cliente, tipo, "EXCEPCION", None, str(e))
                resultados["error"] += 1

        # Cerrar conexión ERP
        conn.close()

        # Resumen
        log.info("")
        log.info("=" * 60)
        log.info("RESUMEN")
        log.info("=" * 60)
        log.info(f"  Total procesados: {len(clientes)}")
        log.info(f"  Completados: {resultados['completado']}")
        log.info(f"  Errores: {resultados['error']}")
        if resultados["error"] > 0:
            log.info(f"  Detalle de errores en: {ERROR_FILE}")
        log.info("=" * 60)

    except Exception as e:
        log.error(f"ERROR FATAL: {e}")
        sys.exit(1)