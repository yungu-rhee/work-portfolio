# Middleware ERP → Business Central: Contrapartes

Middleware en Python que sincroniza clientes (contrapartes) desde un ERP basado en SQL Server hacia Microsoft Dynamics 365 Business Central a través de la API bciContacts.

## Contexto

Este script forma parte de un proceso de integración real que se ejecuta diariamente en producción en la empresa donde trabajo. Actúa como middleware entre el ERP de gestión (SQL Server) y Business Central, manteniendo sincronizadas las fichas de clientes (contrapartes) en ambos sistemas.

Por motivos de confidencialidad, se muestra únicamente la arquitectura y la lógica del proceso. Los datos de conexión (tenant, client_id, client_secret, servidor SQL, base de datos, queries) han sido eliminados o reemplazados por placeholders. El código es completamente funcional: basta con rellenar la configuración.

## Arquitectura

```
┌──────────────┐     SQL Query      ┌──────────────────┐
│  ERP origen  │ ──────────────────> │ Clientes nuevos  │
│ (SQL Server) │                     │  y modificados   │
└──────┬───────┘                     └────────┬─────────┘
       │                                      │
       │                                      │ por cada cliente
       │                                      v
       │                              ┌───────────────┐
       │                              │  Mapeo campos │
       │                              │  ERP → JSON   │
       │                              └───────┬───────┘
       │                                      │
       │                                      v
       │    OAuth2 token              ┌───────────────┐
       │  ◄────────────────────────── │  POST a BC    │
       │                              │ (bciContacts) │
       │                              └───────┬───────┘
       │                                      │
       │                                      │ polling con reintentos
       │                                      v
       │                              ┌───────────────┐
       │                              │ GET estado BC │
       │                              │ (Completed?)  │
       │                              └───────┬───────┘
       │                                      │
       │         UPDATE con IDs de BC         │
       │ ◄────────────────────────────────────┘
       │
└──────────────┘
```

## Características

- **Diferencia altas de modificaciones**: detecta si el cliente es nuevo (N) o modificado (M) y adapta el payload y la actualización posterior.
- **OAuth2 con renovación**: obtiene token via client_credentials y lo renueva automáticamente antes de que expire (umbral de 50 minutos).
- **Polling asíncrono**: BC procesa los registros de forma asíncrona; el script consulta el estado con reintentos configurables (por defecto 10 intentos, 10s de espera).
- **Log de errores en JSON**: cada error se registra en un fichero JSON independiente con fase, status_code y mensaje para trazabilidad.
- **Logging completo**: consola + fichero con timestamp y nivel.

## Entrada esperada

### Query de clientes pendientes

La query `QUERY_CLIENTES_PENDIENTES` debe devolver los siguientes campos:

| Columna | Tipo | Descripción |
|---|---|---|
| TIPO | CHAR(1) | 'N' = nuevo (sin código BC), 'M' = modificado |
| IdCliente | INT | ID interno del cliente en el ERP |
| CodigoExternoBC | VARCHAR | Código de contacto asignado por BC (null si es nuevo) |
| CodigoExterno1 | VARCHAR | requestID de la última sincronización exitosa |
| contactName1 | VARCHAR | Nombre o razón social del cliente |
| contactAddress1 | VARCHAR | Dirección completa (tipo vía + calle + número + aclarador) |
| contactCity | VARCHAR | Ciudad |
| contactPostcode | VARCHAR | Código postal |
| contactCountryCode | VARCHAR | Código país (ISO 2 letras, ej: ES) |
| contactCountyCode | VARCHAR | Código de provincia/municipio |
| contactEmailAddress | VARCHAR | Email de contacto |
| contactPhoneNo | VARCHAR | Teléfono de contacto |
| contactVATRegNo | VARCHAR | NIF/CIF del cliente |

### Criterios de selección

- **Nuevos (N)**: clientes cuyo código externo de BC es NULL (nunca sincronizados).
- **Modificados (M)**: clientes modificados desde la última ejecución (por ejemplo, comparando fecha de última modificación con el día anterior).

## Salida esperada

### Actualización tras alta exitosa (tipo N)

Tras recibir confirmación de BC (`status = Completed`), se actualizan dos campos en el ERP:

| Parámetro (orden) | Valor | Descripción |
|---|---|---|
| 1 | contactNo | Código de contacto asignado por BC |
| 2 | requestID | ID de la petición en BC |
| 3 | IdCliente | ID del cliente en el ERP (cláusula WHERE) |

### Actualización tras modificación exitosa (tipo M)

| Parámetro (orden) | Valor | Descripción |
|---|---|---|
| 1 | requestID | ID de la última petición exitosa en BC |
| 2 | IdCliente | ID del cliente en el ERP (cláusula WHERE) |

## Mapeo ERP → Business Central (bciContacts)

| Campo BC | Origen | Notas |
|---|---|---|
| interfaceType | Configuración fija | Identifica la interfaz/país |
| bcCompany | Configuración fija | Código de empresa en BC |
| contactType | "Company" | Fijo |
| duplicationCheck | "As Non-Legal Entity" | Fijo |
| sourceSysRecId | IdCliente | ID del ERP como string |
| bcOrigReqID | CodigoExterno1 | Solo en modificaciones |
| contactNo | CodigoExternoBC | Solo en modificaciones |
| contactName1 | Nombre/Razón social | Uppercase |
| contactAddress1 | Dirección compuesta | Uppercase |
| contactCity | Ciudad | Desde tabla de ciudades |
| contactPostcode | Código postal | Desde tabla de callejero |
| contactCountryCode | Nacionalidad | Default: "ES" |
| contactCountyCode | Código INE | Desde tabla de ciudades |
| contactEmailAddress | Email | Primer email de contacto |
| contactPhoneNo | Teléfono | Primer teléfono de contacto |
| contactVATRegNo | NIF/CIF | Identidad fiscal |
| contactCustTempCode | Configuración fija | Template de cliente en BC |

## Configuración

Antes de ejecutar, adaptar en el script:

1. **`BC_CONFIG`**: tenant_id, client_id, client_secret, environment, company_guid, interface_type, bc_company.
2. **`ERP_SERVER`**, **`ERP_DB`**: servidor y base de datos del ERP origen.
3. **`QUERY_CLIENTES_PENDIENTES`**: query que devuelve los clientes pendientes (13 columnas, ver tabla arriba).
4. **`QUERY_ACTUALIZAR_NUEVO`**: UPDATE para altas (3 parámetros).
5. **`QUERY_ACTUALIZAR_MODIFICACION`**: UPDATE para modificaciones (2 parámetros).
6. **Keyring**: almacenar las credenciales SQL con `keyring.set_password("sql_erp", "<usuario>", "<password>")`.

## Dependencias

```
pip install pyodbc keyring requests
```

Requiere ODBC Driver 17 for SQL Server instalado en el sistema.