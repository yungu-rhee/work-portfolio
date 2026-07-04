# SIPS Consumos Loader - Power (Electricidad)

ETL en Python que extrae consumos de electricidad desde una API SIPS y los carga en SQL Server mediante MERGE (upsert).

## Contexto

Este script forma parte de un proceso ETL real que se ejecuta en producción en la empresa donde trabajo. Se encarga de mantener actualizada la base de datos de consumos eléctricos a partir de la información que expone la API SIPS (Sistema de Información de Puntos de Suministro).

Por motivos de confidencialidad, se muestra únicamente la arquitectura y la lógica del proceso. Los nombres de servidores, bases de datos, tablas, credenciales y queries específicas han sido eliminados o reemplazados por placeholders. El código es completamente funcional: basta con rellenar la configuración y las queries adaptadas al esquema propio.

## Arquitectura

```
┌──────────────┐     SQL Query      ┌──────────────┐
│  SQL Server  │ ──────────────────> │  CUPS list   │
│  (contratos) │                     │  (activos)   │
└──────────────┘                     └──────┬───────┘
                                            │
                                            │ por cada CUPS
                                            v
                                     ┌──────────────┐
                                     │   API SIPS   │
                                     │  (POST/JSON) │
                                     └──────┬───────┘
                                            │
                                            │ JSON con consumos
                                            v
                                     ┌──────────────┐
                                     │    MERGE     │
                                     │   (upsert)   │
                                     └──────┬───────┘
                                            │
                                            v
                                     ┌──────────────┐
                                     │  SQL Server  │
                                     │  (consumos)  │
                                     └──────────────┘
```

## Características

- **Reintentos automáticos**: configurable (por defecto 3 intentos con 5s de espera).
- **Renovación de token**: si la API devuelve HTTP 401, renueva el token y reintenta.
- **Commits por lotes**: commit cada N CUPS procesados para equilibrar seguridad y rendimiento.
- **Rate limiting**: pausa configurable entre llamadas para no saturar la API.
- **Logging**: consola + fichero con timestamp, nivel y mensaje.

## Entrada esperada

### Tabla de contratos (origen de CUPS)

La query `QUERY_CUPS_ACTIVOS` debe devolver **una sola columna** con los códigos CUPS activos de electricidad. Debe filtrar por:

- Estado del contrato (por ejemplo: confirmado, en trámite, pendiente...).
- Tipo de suministro = electricidad (según la codificación que use tu esquema).

Ejemplo de estructura:

| Columna | Tipo | Descripción |
|---|---|---|
| cups_codigo | VARCHAR(25) | Código CUPS del punto de suministro |

## Salida esperada

### Tabla destino de consumos

El MERGE inserta o actualiza registros con la siguiente estructura. La **clave primaria** es `(cups, fecha_inicio)`.

| Parámetro (orden) | Campo API SIPS | Descripción | Tipo |
|---|---|---|---|
| 1 | - | Código CUPS | VARCHAR(25) |
| 2 | FechaInicio | Fecha inicio del periodo | DATE |
| 3 | FechaFin | Fecha fin del periodo | DATE |
| 4 | CodigoTarifaATR | Tarifa ATR aplicada | VARCHAR(10) |
| 5 | Activa1 | Consumo energía activa P1 | FLOAT |
| 6 | Activa2 | Consumo energía activa P2 | FLOAT |
| 7 | Activa3 | Consumo energía activa P3 | FLOAT |
| 8 | Activa4 | Consumo energía activa P4 | FLOAT |
| 9 | Activa5 | Consumo energía activa P5 | FLOAT |
| 10 | Activa6 | Consumo energía activa P6 | FLOAT |
| 11 | Reactiva1 | Consumo energía reactiva P1 | FLOAT |
| 12 | Reactiva2 | Consumo energía reactiva P2 | FLOAT |
| 13 | Reactiva3 | Consumo energía reactiva P3 | FLOAT |
| 14 | Reactiva4 | Consumo energía reactiva P4 | FLOAT |
| 15 | Reactiva5 | Consumo energía reactiva P5 | FLOAT |
| 16 | Reactiva6 | Consumo energía reactiva P6 | FLOAT |
| 17 | Potencia1 | Potencia P1 | FLOAT |
| 18 | Potencia2 | Potencia P2 | FLOAT |
| 19 | Potencia3 | Potencia P3 | FLOAT |
| 20 | Potencia4 | Potencia P4 | FLOAT |
| 21 | Potencia5 | Potencia P5 | FLOAT |
| 22 | Potencia6 | Potencia P6 | FLOAT |
| 23 | CodigoDHEquipoDeMedida | Código equipo de medida | VARCHAR(10) |
| 24 | CodigoTipoLectura | Tipo lectura: R (Real) o E (Estimada) | VARCHAR(5) |

## Configuración

Antes de ejecutar, adaptar en el script:

1. **`API_HOST`**, **`API_AUTH_HEADERS`**: host y credenciales de la API SIPS.
2. **`SQL_SERVER`**, **`SQL_DATABASE`**: servidor y base de datos SQL Server.
3. **`QUERY_CUPS_ACTIVOS`**: query que devuelve los CUPS activos de electricidad.
4. **`MERGE_CONSUMOS_SQL`**: MERGE adaptado a tu esquema de tabla destino (24 parámetros en el orden indicado arriba).
5. **Keyring**: almacenar las credenciales SQL con `keyring.set_password("sql", "<usuario>", "<password>")`.

## Dependencias

```
pip install pyodbc keyring
```

Requiere ODBC Driver 17 for SQL Server instalado en el sistema.