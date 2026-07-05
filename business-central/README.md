# Business Central — Integraciones

Middlewares que sincronizan datos desde el ERP de gestión (SQL Server) hacia Microsoft Dynamics 365 Business Central a través de sus APIs REST.

## Contenido

### [contrapartes/](contrapartes/)
Sincronización diaria de clientes (contrapartes) entre el ERP y BC. Detecta altas y modificaciones, envía a la API bciContacts, verifica el estado con polling y actualiza el ERP con los IDs generados por BC.

### [documentos-contables/](documentos-contables/)
Exportación de facturas de venta en dos pasos. Step 1 envía el documento (cabecera + líneas) a la API rawExdocs. Step 2 lanza el procesamiento en BC via ODataV4 Nav.Copy. El control de estado (`IsExportado`) permite reanudar desde el punto de fallo.