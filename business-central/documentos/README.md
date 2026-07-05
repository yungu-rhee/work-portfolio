# Middleware ERP → Business Central: Documentos Contables

Middleware en Python que sincroniza facturas de venta desde un ERP basado en SQL Server hacia Microsoft Dynamics 365 Business Central a través de la API rawExdocs, en dos pasos.

## Contexto

Este proceso forma parte de una integración real que se ejecuta en producción en la empresa donde trabajo. Automatiza la exportación de facturas de venta desde el ERP de gestión hacia Business Central, donde se contabilizan. El proceso se divide en dos pasos independientes (dos scripts) para poder gestionar fallos parciales sin perder el trabajo ya hecho.

Por motivos de confidencialidad, se muestra únicamente la arquitectura y la lógica del proceso. Los datos de conexión, credenciales, queries y nombres de tablas han sido eliminados o reemplazados por placeholders.

## Arquitectura

```
                         STEP 1 (step1_envio.py)
                         ========================

┌──────────────┐     SQL Query      ┌──────────────────┐
│  ERP origen  │ ──────────────────> │    Facturas      │
│ (SQL Server) │                     │   pendientes     │
└──────┬───────┘                     │ (cabecera+líneas)│
       │                             └────────┬─────────┘
       │                                      │
       │                                      │ por cada factura
       │                                      v
       │                              ┌───────────────┐
       │                              │  Mapeo campos │
       │                              │  ERP → JSON   │
       │                              │ (cab + líneas)│
       │                              └───────┬───────┘
       │                                      │
       │    OAuth2 token              ┌───────────────┐
       │  ◄────────────────────────── │  POST a BC    │
       │                              │  (rawExdocs)  │
       │                              └───────┬───────┘
       │                                      │
       │    UPDATE IsExportado=0              │ BC devuelve
       │    + guardar id y packageID          │ packageID + id
       │ ◄────────────────────────────────────┘
└──────────────┘


                         STEP 2 (step2_procesamiento.py)
                         ================================

┌──────────────┐     SQL Query      ┌──────────────────┐
│  ERP origen  │ ──────────────────> │    Facturas      │
│ (SQL Server) │                     │ IsExportado = 0  │
└──────┬───────┘                     │ (con id + pkgID) │
       │                             └────────┬─────────┘
       │                                      │
       │                                      │ por cada factura
       │                                      v
       │    OAuth2 token              ┌───────────────┐
       │  ◄────────────────────────── │ POST Nav.Copy │
       │                              │   (ODataV4)   │
       │                              └───────┬───────┘
       │                                      │
       │    UPDATE IsExportado=1              │ BC procesa
       │ ◄────────────────────────────────────┘ el documento
└──────────────┘
```

## Control de estado (IsExportado)

| Valor | Significado |
|---|---|
| NULL | Factura no exportada (pendiente de Step 1) |
| 0 | Step 1 OK: enviada a BC, pendiente de procesamiento (Step 2) |
| 1 | Step 2 OK: procesada en BC, completada |

Si un step falla, no modifica IsExportado. El error se registra en un fichero JSON independiente y la factura queda en su estado actual para reintentar en la siguiente ejecución.

## Características

- **Proceso en dos pasos**: permite reanudar desde el punto de fallo sin reenviar documentos ya aceptados por BC.
- **OAuth2 (client_credentials)**: autenticación contra Microsoft Entra ID.
- **Mapeo cabecera + líneas**: construye el JSON completo con cabecera y líneas DOC_LINE/VAT_LINE en una sola estructura.
- **Cálculo de IVA por línea**: calcula el importe de IVA a partir de la base imponible y el porcentaje.
- **Step 2 via ODataV4 Nav.Copy**: lanza el procesamiento del documento RAW en BC usando la acción Nav.Copy.
- **Log de errores en JSON**: fichero independiente por ejecución con fase, status_code y mensaje.
- **Backup del JSON enviado**: cada documento enviado se guarda a disco antes del POST.
- **Logging completo**: consola + fichero con timestamp y nivel.

## Entrada esperada

### Step 1 — Query de cabeceras

La query de cabeceras debe devolver facturas no exportadas (`IsExportado IS NULL`) con los siguientes campos:

| Columna | Tipo | Descripción |
|---|---|---|
| IdFactura | INT | ID interno de la factura en el ERP |
| AccDoc_Type_Id | VARCHAR | Tipo de documento: 'SA-INV' (factura) o 'SA-CM' (abono), según signo del importe |
| POSTING_DATE | DATE | Fecha de contabilización |
| DOC_DATE | DATE | Fecha del documento |
| VAT_DATE | DATE | Fecha de IVA |
| Due_Date | DATE | Fecha de vencimiento |
| Contact_Id | VARCHAR | Código del cliente en BC (previamente sincronizado) |
| Currency | VARCHAR | Código de divisa (ej: EUR) |
| Total_Net_Amount | DECIMAL | Suma de importes base |
| VAT_Amount | DECIMAL | Suma de importes de IVA |
| Total_Amount | DECIMAL | Suma de importes totales |
| Delivery_Period_Start | DATE | Inicio del periodo de consumo |
| Delivery_Period_End | DATE | Fin del periodo de consumo |
| Payment_Method | VARCHAR | Método de pago: 'TRANSF' o 'BANK' |
| IBAN | VARCHAR | IBAN del cliente |
| Ref_AccDoc_Id | INT/VARCHAR | ID de la factura origen (para abonos) |
| External_Document_Id | VARCHAR | Número de factura externo (serie + número) |

### Step 1 — Query de líneas

Para cada factura, la query de líneas (parametrizada por IdFactura) debe devolver:

| Columna | Tipo | Descripción |
|---|---|---|
| tipo_linea | VARCHAR | 'DOC' para líneas de documento |
| AccDoc_Line_Type_Id | INT/VARCHAR | Código del concepto de facturación |
| Description | VARCHAR | Descripción de la línea |
| Quantity | DECIMAL | Cantidad (puede extraerse de XML para ciertos conceptos) |
| Unit_Price | DECIMAL | Precio unitario (puede extraerse de XML para ciertos conceptos) |
| ImporteBase | DECIMAL | Importe base de la línea |
| VAT_Identifier | INT | Identificador de IVA (mapeo numérico del porcentaje) |
| VAT_Percentage | DECIMAL | Porcentaje de IVA |
| Line_Delivery_Period_Start | DATE | Inicio del periodo de la línea |
| Line_Delivery_Period_End | DATE | Fin del periodo de la línea |
| Del_Point | VARCHAR | Código del punto de suministro (CUPS) |
| Ref_AccDoc_Id | INT/VARCHAR | Referencia a factura origen |

### Step 2 — Query de documentos pendientes

Debe devolver facturas con `IsExportado = 0`:

| Columna | Tipo | Descripción |
|---|---|---|
| IdFactura | INT | ID interno de la factura |
| bc_id | INT/VARCHAR | ID asignado por BC en Step 1 |
| bc_packageID | VARCHAR | packageID asignado por BC en Step 1 |

## Salida esperada

### Step 1 — Actualización tras envío exitoso

| Parámetro (orden) | Valor | Descripción |
|---|---|---|
| 1 | id | ID asignado por BC |
| 2 | packageID | Package ID asignado por BC |
| 3 | IdFactura | ID de la factura en el ERP (WHERE) |

Además, se marca `IsExportado = 0`.

### Step 2 — Actualización tras procesamiento exitoso

| Parámetro (orden) | Valor | Descripción |
|---|---|---|
| 1 | IdFactura | ID de la factura en el ERP (WHERE) |

Se marca `IsExportado = 1`.

## Mapeo de IVA

El porcentaje de IVA del ERP se mapea a un identificador numérico para BC:

| Porcentaje | VAT_Identifier |
|---|---|
| 21% | 1 |
| 0% | 2 |
| 10% | 5 |
| 5% | 6 |
| 7% | 7 |
| 3% | 8 |
| 1% | 9 |

## Configuración

Antes de ejecutar, adaptar en ambos scripts:

1. **`BC_CONFIG`**: tenant_id, client_id, client_secret, environment, company_guid (Step 1) / company_name (Step 2), interface_type.
2. **`ERP_SERVER`**, **`ERP_DB`**: servidor y base de datos del ERP origen.
3. **Queries SQL**: adaptar las queries placeholder a tu esquema de tablas.
4. **Keyring**: almacenar las credenciales SQL con `keyring.set_password("sql_erp", "<usuario>", "<password>")`.

## Dependencias

```
pip install pyodbc keyring requests
```

Requiere ODBC Driver 17 for SQL Server instalado en el sistema.