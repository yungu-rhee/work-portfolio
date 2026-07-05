"""
MIDDLEWARE ERP → BUSINESS CENTRAL: Documentos Contables — STEP 1 (Envío)
==========================================================================
Lee facturas pendientes del ERP y las envía a BC (rawExdocs).
Si OK: marca IsExportado = 0, guarda id y packageID.
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
    filename=f"erp_bc_accdoc_step1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
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
    "company_guid": "your-company-guid",
    "interface_type": "YOUR_INTERFACE_TYPE",
}

# ============================================================
# CONFIGURACIÓN ERP ORIGEN (SQL Server)
# ============================================================

ERP_SERVER = "your-erp-server"
ERP_DB = "Your_ERP_Database"

# ============================================================
# ARCHIVO DE ERRORES
# ============================================================

ERROR_FILE = f"erp_bc_accdoc_step1_errores_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

def guardar_error(id_factura, status_code, mensaje):
    """Añade un error al archivo JSON de errores."""
    try:
        with open(ERROR_FILE, "r", encoding="utf-8") as f:
            errores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        errores = []

    errores.append({
        "fecha": datetime.now().isoformat(),
        "IdFactura": id_factura,
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
# OBTENER DOCUMENTOS PENDIENTES DEL ERP
# ============================================================

def obtener_documentos_pendientes(conn):
    cursor = conn.cursor()

    # ----------------------------------------------------------
    # QUERY CABECERA: obtener documentos pendientes
    # ----------------------------------------------------------
    # Debe devolver facturas no exportadas con los campos:
    #   IdFactura, AccDoc_Type_Id (SA-INV o SA-CM según signo del importe),
    #   POSTING_DATE, DOC_DATE, VAT_DATE, Due_Date,
    #   Contact_Id (código externo del cliente en BC),
    #   Currency, Total_Net_Amount, VAT_Amount, Total_Amount,
    #   Delivery_Period_Start, Delivery_Period_End,
    #   Payment_Method (TRANSF o BANK), IBAN,
    #   Ref_AccDoc_Id, External_Document_Id
    # Filtro: IsExportado IS NULL, fecha >= umbral, número asignado.
    # Ver README.md para detalle de cada campo.
    query_header = """
        -- Query de cabeceras de factura pendientes de exportar.
        -- Debe agrupar por cabecera y sumar importes desde la tabla de totales.
        -- Ver README.md para el mapeo completo de campos.
    """

    # ----------------------------------------------------------
    # QUERY LÍNEAS DOC_LINE
    # ----------------------------------------------------------
    # Debe devolver las líneas de una factura con los campos:
    #   tipo_linea ('DOC'), AccDoc_Line_Type_Id, Description,
    #   Quantity, Unit_Price, ImporteBase,
    #   VAT_Identifier (mapeado numérico del % IVA),
    #   VAT_Percentage,
    #   Line_Delivery_Period_Start, Line_Delivery_Period_End,
    #   Del_Point (código CUPS), Ref_AccDoc_Id,
    #   VAT_Net_Amount, VAT_Amount_Line, VAT_Total_Amount
    # Parámetro: IdFactura
    # Ver README.md para detalle de cada campo.
    query_doc_lines = """
        -- Query de líneas de factura para un IdFactura dado (?).
        -- Incluye joins a tablas de impuestos, contratos y puntos de suministro.
        -- Para ciertos conceptos, extrae cantidad/precio de campos XML.
        -- Ver README.md para el mapeo completo de campos.
    """

    cursor.execute(query_header)
    columns = [col[0] for col in cursor.description]
    headers = [dict(zip(columns, row)) for row in cursor.fetchall()]

    log.info(f"Documentos pendientes encontrados: {len(headers)}")

    documentos = []
    for header in headers:
        id_factura = header["IdFactura"]
        cursor.execute(query_doc_lines, id_factura)
        line_columns = [col[0] for col in cursor.description]
        lines = [dict(zip(line_columns, row)) for row in cursor.fetchall()]

        documentos.append({
            "header": header,
            "lines": lines
        })

    return documentos

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
# MAPEAR DOCUMENTO ERP → JSON BC
# ============================================================

def mapear_documento(doc):
    """
    Convierte un documento del ERP al JSON que espera BC (rawExdocs).
    
    doc = { "header": {...}, "lines": [{...}, ...] }
    """
    h = doc["header"]

    # ----------------------------------------------------------
    # MAPEO CABECERA
    # ----------------------------------------------------------
    json_header = {
        # --- packageID e id los asigna BC, no se envían ---
        "documentType": "_x0020_",

        # --- Campos mapeados desde ERP ---
        "interfaceType": BC_CONFIG["interface_type"],
        "documentNo": str(h.get("IdFactura", "")),
        "movementCode": str(h.get("AccDoc_Type_Id", "")),
        "postingDate": str(h.get("POSTING_DATE", "0001-01-01")),
        "documentDate": str(h.get("DOC_DATE", "0001-01-01")),
        "vatDate": str(h.get("VAT_DATE", "0001-01-01")),
        "dueDate": str(h.get("Due_Date", "0001-01-01")),
        "externalPartnerNo": str(h.get("Contact_Id", "")),
        "currencyCode": str(h.get("Currency", "")),
        "amount": float(h.get("Total_Net_Amount", 0)),
        "vatAmount": float(h.get("VAT_Amount", 0)),
        "amountInclVAT": float(h.get("Total_Amount", 0)),
        "deliveryPeriodStartingDate": str(h.get("Delivery_Period_Start", "0001-01-01")),
        "deliveryPeriodEndingDate": str(h.get("Delivery_Period_End", "0001-01-01")),
        "externalField1": str(h.get("Payment_Method", "")),
        "iban": str(h.get("IBAN", "")),
        "externalField2": str(h.get("Ref_AccDoc_Id", "")),
        "externalField6": str(h.get("External_Document_Id", "")),

        # --- Campos fijos según documentación (Null / 0 / False) ---
        "storno": False,
        "appliedToDocumentNo": "",
        "externalField4": "",
        "externalField7": "",
        "documentNoInternal": "",
        "externalField8": "",

        # --- Campos con valor por defecto ---
        "importDate": "0001-01-01",
        "paymentDate": "0001-01-01",
        "externalDate": "0001-01-01",
        "customerNo": "",
        "vendorNo": "",
        "externalField3": "",
        "externalField5": "",
        "externalField9": "",
        "externalField10": "",
        "externalField11": "",
        "externalField12": "",
        #"status": "Inserted", # NO DEJA _x0020_ -- NO INCLUIDO EN EL MENSAJE (PERO EN EL EXCEL SI APARECE?)
        "transactionType": "Sales", # NO DEJA _x0020_ -- SALES OR PURCHASE
        "internalPartnerName": "",
        #"documentTypeInternal": "Quote", # NO DEJA _x0020_ -- NO INCLUIDO EN EL MENSAJE (PERO EN EL EXCEL SI APARECE?)
        "invoiceType": "",
        "documentNumber": 0,
        "deliveryDate": "0001-01-01",
        "forceNewPurchaseHeader": False,
        "currencyFactor": 0,
        "extStatus": "",
        "reasonCode": "",
        "externalType": "",
        "originalDocumentNo": "",
        "division": "",
        "amountLCY": 0,
        "publishingDateTime": "0001-01-01T00:00:00Z",
        "validUntil": "0001-01-01",
        "postedDocumentNo": "",
        "calculatedAmount": 0,
        "calculatedAmountInclVAT": 0,
        "stornoReasonCode": "",
        "startedBy": "",
        "reversed": False,
        "reversedByPackageID": "",
        "reversedByID": 0,
        "reversingDate": "0001-01-01",
        "purchTaxCountry": "",
        "loadingCountry": "",
        "saleTaxCountry": "",
        "dischCountry": "",
        "externalBankCode": "",
        "swiftCode": "",
        "bankAccountNo": "",
        "correspSwiftCode": "",
        "titleTransferDateEstimated": "0001-01-01",
        "titleTransferDateActual": "0001-01-01",
        "creditLimit": False,
        "rawData": False,
        "workflowLevel": 0,
        "reverseID": "",
        "toReverseID": "",
        "originalID": 0,
        "idFilter": 0,
        "notToProcess": False,
        "readyToPost": False,
        "reversalType": "",
        "extApprover1": "",
        "extApprover2": "",
        "extSenderId": "",
        "nothingToPost": False,
    }

    # ----------------------------------------------------------
    # MAPEO LÍNEAS (DOC_LINE y VAT_LINE mezcladas)
    # ----------------------------------------------------------
    json_lines = []
    for ln in doc["lines"]:
        tipo = ln.get("tipo_linea", "DOC")

        if tipo == "DOC":
            # Cálculo de IVA por línea según documentación
            line_amount = float(ln.get("ImporteBase", 0))
            vat_pct = float(ln.get("VAT_Percentage", 0))
            line_vat_amount = round(line_amount * vat_pct / 100, 2)
            line_total_amount = round(line_amount + line_vat_amount, 2)

            json_line = {
                # --- Campos mapeados desde ERP (DOC_LINE) ---
                "externalItemNo": str(ln.get("AccDoc_Line_Type_Id", "")),
                "externalItemDescription": str(ln.get("Description", "")),
                "quantity": float(ln.get("Quantity", 0)),
                "calculatedUnitAmount": float(ln.get("Unit_Price", 0)),
                "amount": line_amount,
                "vatAmount": line_vat_amount,
                "amountInclVAT": line_total_amount,
                "externalField3": str(ln.get("VAT_Identifier", "")),
                "vat": vat_pct,
                "deliveryPeriodStartingDate": str(ln.get("Line_Delivery_Period_Start", "0001-01-01")),
                "deliveryPeriodEndingDate": str(ln.get("Line_Delivery_Period_End", "0001-01-01")),
                "dimension2ValueCode": str(ln.get("Del_Point", "")),
                "externalField7": str(ln.get("Ref_AccDoc_Id", "")),

                # --- Campos fijos Null según documentación ---
                "externalField6": 0,
                "externalItemDescription2": "",
                "externalUnitOfMeasure": "",
                "dimension3ValueCode": "",
                "dimension6ValueCode": "",
                "externalField11": "",
                "externalField12": "",

                # --- Campos no usados en DOC_LINE ---
                "externalField1": "",
            }

        elif tipo == "VAT":
            json_line = {
                # --- Campos mapeados desde ERP (VAT_LINE) ---
                "externalField1": str(ln.get("VAT_Identifier", "")),
                "vat": float(ln.get("VAT_Percentage", 0)),
                "amount": float(ln.get("VAT_Net_Amount", 0)),
                "vatAmount": float(ln.get("VAT_Amount_Line", 0)),
                "amountInclVAT": float(ln.get("VAT_Total_Amount", 0)),

                # --- Campos no usados en VAT_LINE ---
                "externalItemNo": "",
                "externalItemDescription": "",
                "externalItemDescription2": "",
                "quantity": 0,
                "calculatedUnitAmount": 0,
                "externalField3": "",
                "externalField6": 0,
                "externalField7": "",
                "externalUnitOfMeasure": "",
                "deliveryPeriodStartingDate": "0001-01-01",
                "deliveryPeriodEndingDate": "0001-01-01",
                "dimension2ValueCode": "",
                "dimension3ValueCode": "",
                "dimension6ValueCode": "",
                "externalField11": "",
                "externalField12": "",
            }
        else:
            log.warning(f"Tipo de línea desconocido: {tipo}, saltando")
            continue

        # --- Campos comunes con valor por defecto (ambos tipos) ---
        # lineNo, packageID, id NO se envían — los asigna BC
        json_line.update({
            "interfaceType": BC_CONFIG["interface_type"],
            "documentNo": "",
            #"status": "Inserted", # NO DEJA _x0020_
            "description": "",
            "unitOfMeasure": "",
            "no": "",
            "type": "_x0020_",
            "customerNo": "",
            "vendorNo": "",
            #"postingDocumentType": "Invoice", # NO DEJA _x0020_ -- NO INCLUIDO EN EL MENSAJE (PERO EN EL EXCEL SI APARECE?)
            "externalField2": "",
            "externalField4": "",
            "externalField5": "",
            "externalField8": "",
            "externalField9": "",
            "externalField10": "",
            "externalField13": "",
            "externalField14": "",
            "movementCode": "",
            "invoiceType": "",
            "externalPartnerNo": "",
            "reasonCode": "",
            "postingDate": "0001-01-01",
            "division": "",
            "storno": False,
            "currencyCode": "",
            "amountLCY": 0,
            "reversed": False,
            "reversedByPackageID": "",
            "reversedByID": 0,
            "loadingCountry": "",
            "saleTaxCountry": "",
            "dischCountry": "",
            "itemNo": "",
            "gLAccount": "",
            "productGroupCode": "",
            "paymentGroupCode": "",
            "dimension1ValueCode": "",
            "dimension4ValueCode": "",
            "dimension5ValueCode": "",
            "dimension7ValueCode": "",
            "dimension8ValueCode": "",
            "dimension9ValueCode": "",
            "dimension10ValueCode": "",
            "dimension11ValueCode": "",
            "dimension12ValueCode": "",
            "dimension13ValueCode": "",
            "dimension14ValueCode": "",
            "dimension15ValueCode": "",
            "dimension16ValueCode": "",
            "dimension17ValueCode": "",
            "dimension18ValueCode": "",
            "dimension19ValueCode": "",
            "dimension20ValueCode": "",
            "calculatedUnitAmtInclVAT": 0,
            "vatProdPostingGroup": "",
            "custLedgerEntryNo": 0,
            "vendLedgerEntryNo": 0,
            "customerPostingGroup": "",
            "vendorPostingGroup": "",
            "technical": False,
            "genBusPostingGroup": "",
            "genProdPostingGroup": "",
            "externalLocationID": "",
            "locationCode": "",
            "paymentTypeCode": "",
            "reference": False,
            "entryType": "",
            "appliedTo": "",
            "postingNo": "",
            "postingLineNo": 0,
            "description2": "",
            "unitPriceLCY": 0,
            "deliveryPointID": 0,
            "reversedLine": 0,
            "workflowLevel": 0,
            "exchRate": 0,
            "originalAmount": 0,
            "accruedAmount": 0,
            "costPercentage": 0,
            "purchIncoterm": "",
            "saleIncoterm": "",
            "purchTaxCountry": "",
            "originalQuantity": 0,
            "originalID": 0,
            "idFilter": 0,
            "allQty": 0,
            "allTotalQty": 0,
            "aggregationKey": "",
            "extStatus": "",
        })

        json_lines.append(json_line)

    json_header["lines"] = json_lines
    return json_header

# ============================================================
# ENVIAR DOCUMENTO A BC (Step 1 — rawExdocs)
# ============================================================

def enviar_documento_bc(token, json_doc):
    """
    Retorna la respuesta de BC (con packageID e id asignados).
    """
    base_url = "https://api.businesscentral.dynamics.com/v2.0"
    tenant = BC_CONFIG["tenant_id"]
    env = BC_CONFIG["environment"]
    company_guid = BC_CONFIG["company_guid"]

    url = (
        f"{base_url}/{tenant}/{env}/api/iTAdviseAG/BCISrawExdoc/v2.0"
        f"/companies({company_guid})/rawExdocs"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Guardar JSON enviado a archivo
    archivo_json = f"erp_bc_accdoc_step1_enviado_{json_doc.get('documentNo', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(archivo_json, "w", encoding="utf-8") as f:
        json.dump(json_doc, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"JSON enviado guardado en: {archivo_json}")

    resp = requests.post(url, headers=headers, json=json_doc)

    if resp.status_code in (200, 201):
        data = resp.json()
        log.info(f"Documento enviado OK — packageID: {data.get('packageID')}, id: {data.get('id')}")
        return data
    else:
        log.error(f"Error al enviar documento: {resp.status_code} — {resp.text}")
        return {"error": True, "status_code": resp.status_code, "mensaje": resp.text}

# ============================================================
# ACTUALIZAR ERP CON EL RESULTADO
# ============================================================

def actualizar_erp(conn, documento_erp, respuesta_bc):
    """
    Marca IsExportado = 0 (Step 1 OK, pendiente Step 2).
    Guarda id y packageID en campos de control del ERP.

    Query esperada (3 parámetros):
      1. id devuelto por BC
      2. packageID devuelto por BC
      3. IdFactura del ERP (cláusula WHERE)
    Ver README.md para detalle.
    """
    cursor = conn.cursor()

    query_update = """
        -- UPDATE que marca la factura como enviada (IsExportado = 0)
        -- y guarda el id y packageID de BC para Step 2.
        -- Parámetros: (1) bc_id, (2) bc_packageID, (3) IdFactura
    """

    cursor.execute(query_update, (
        respuesta_bc.get("id"),
        respuesta_bc.get("packageID"),
        documento_erp["header"]["IdFactura"],
    ))
    conn.commit()

    log.info(f"ERP actualizado para documento: {documento_erp['header'].get('IdFactura', '?')}")

# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=" * 60)
    log.info("INICIO — Step 1: Envío de Documentos Contables a BC")
    log.info("=" * 60)

    try:
        # 1. Conectar al ERP
        conn = conectar_erp()

        # 2. Obtener documentos pendientes
        documentos = obtener_documentos_pendientes(conn)

        if not documentos:
            log.info("No hay documentos pendientes. Fin.")
            conn.close()
            return

        # 3. Obtener token
        token = obtener_token()

        # 4. Procesar cada documento
        enviados = 0
        errores = 0

        for doc in documentos:
            doc_no = doc["header"].get("IdFactura", "?")
            log.info(f"Procesando documento: {doc_no}")

            try:
                # 4a. Mapear
                json_doc = mapear_documento(doc)

                # 4b. Enviar (Step 1)
                respuesta = enviar_documento_bc(token, json_doc)

                if respuesta.get("error"):
                    # Fallo: guardar error a archivo, no tocar ERP
                    guardar_error(doc_no, respuesta["status_code"], respuesta["mensaje"])
                    errores += 1
                else:
                    # OK: actualizar ERP
                    actualizar_erp(conn, doc, respuesta)
                    enviados += 1

            except Exception as e:
                log.error(f"Error procesando {doc_no}: {e}")
                guardar_error(doc_no, None, str(e))
                errores += 1

        conn.close()

        log.info("=" * 60)
        log.info(f"FIN — Enviados: {enviados}, Errores: {errores}")
        if errores > 0:
            log.info(f"Detalle de errores en: {ERROR_FILE}")
        log.info("=" * 60)

    except Exception as e:
        log.error(f"Error fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()