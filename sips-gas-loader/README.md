# SIPS Consumos Loader - Gas

ETL en Python que extrae consumos de gas natural desde una API SIPS y los carga en SQL Server mediante MERGE (upsert).

## Contexto

Este script forma parte de un proceso ETL real que se ejecuta en producción en la empresa donde trabajo. Se encarga de mantener actualizada la base de datos de consumos de gas a partir de la información que expone la API SIPS (Sistema de Información de Puntos de Suministro).

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

La query `QUERY_CUPS_ACTIVOS` debe devolver **una sola columna** con los códigos CUPS activos de gas. Debe filtrar por:

- Estado del contrato (por ejemplo: confirmado, en trámite, pendiente...).
- Tipo de suministro = gas (según la codificación que use tu esquema).

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
| 2 | FechaInicioMesConsumo | Fecha inicio del periodo | DATE |
| 3 | FechaFinMesConsumo | Fecha fin del periodo | DATE |
| 4 | CodigoTarifaPeaje | Tarifa de peaje aplicada | VARCHAR(10) |
| 5 | ConsumoEnWhP1 | Consumo periodo 1 (Wh) | FLOAT |
| 6 | ConsumoEnWhP2 | Consumo periodo 2 (Wh) | FLOAT |
| 7 | CaudalMedioEnWhDia | Caudal medio diario | FLOAT |
| 8 | CaudalMinimoDiario | Caudal mínimo diario | FLOAT |
| 9 | CaudalMaximoDiario | Caudal máximo diario | FLOAT |
| 10 | PorcentajeConsumoNocturno | % consumo nocturno | FLOAT |
| 11 | CodigoTipoLectura | Tipo lectura: R (Real) o E (Estimada) | VARCHAR(1) |

## Configuración

Antes de ejecutar, adaptar en el script:

1. **`API_HOST`**, **`API_AUTH_HEADERS`**: host y credenciales de la API SIPS.
2. **`SQL_SERVER`**, **`SQL_DATABASE`**: servidor y base de datos SQL Server.
3. **`QUERY_CUPS_ACTIVOS`**: query que devuelve los CUPS activos de gas.
4. **`MERGE_CONSUMOS_SQL`**: MERGE adaptado a tu esquema de tabla destino (11 parámetros en el orden indicado arriba).
5. **Keyring**: almacenar las credenciales SQL con `keyring.set_password("sql", "<usuario>", "<password>")`.

## Dependencias

```
pip install pyodbc keyring
```

Requiere ODBC Driver 17 for SQL Server instalado en el sistema.