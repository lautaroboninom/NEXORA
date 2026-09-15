# Copia de NS x cliente a SEPID PRUEBA

Aplicada el 08/09/2026 por pedido del usuario. No se modificaron comprobantes,
movimientos, saldos ni conexiones de otros escenarios. No se hicieron pruebas
operativas en producción.

## Configuración guardada

- Origen: `manager.dbo.AsciiVista`, consulta `1ITEMSSEGVENTAS`, vista `015`,
  escenario `SEP`. Nodo original `10128`.
- Destino: misma consulta madre, vista independiente `037`, escenario `SEPP`.
  Nodo nuevo `10314`, padre `43`, bajo `Ventas` (nodo `40`).
- Ruta: **Ventas → Items Seguimiento → NS x cliente**.
- Conexión de SEPP: sólo base externa `SBDPSEP`, servidor `SAMTRONIC`,
  sistema `WFLEX`. Sin empresas internas asociadas.

Se insertaron 30 registros de configuración: una cabecera, cuatro filtros,
18 columnas, seis entradas de agrupación/agregación y un nodo de árbol.
Los identificadores de filtros los generó SQL Server. No se alteraron los
filtros originales, incluido `scv_ActStock = 1`.

## Aplicación y comprobación

Mecanismo: `python scripts/apply_ns_cliente_sepp.py --apply`.
Sin `--apply`, sólo muestra el estado. La aplicación es transaccional, verifica
el destino de prueba y compara la copia antes de confirmar. Una repetición
verifica la copia existente sin duplicarla.

La lectura posterior comparó todos los campos de las cinco tablas: original
intacto y copia idéntica salvo identificadores y escenario. Respaldo:
`reportes/ns_cliente_sep_origen_copia_20260908.json`. Resultado:
`reportes/ns_cliente_sepp_copia_verificacion_20260908.json`.

La reproducción de la consulta sobre SBDPSEP devuelve NOV/MZJ5S121193 = +1.
Esto no sustituye la comprobación visual de Queries: el usuario debe cerrar y
volver a abrir Queries, seleccionar SEPP y ejecutar la vista recién copiada.
No hubo cambios de estructura de DB; `sql/schema.sql` documenta la operación
externa sin agregar objetos Bejerman al esquema PostgreSQL de NEXORA.
