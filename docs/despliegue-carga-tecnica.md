# Datos iniciales para el futuro despliegue de carga técnica

Esta funcionalidad sigue en `feature/presupuestos-carga-tecnica`. Preparar este mecanismo no despliega ni modifica producción. Las comprobaciones de esta etapa se ejecutan exclusivamente en desarrollo.

## Qué se entrega como seed de negocio

El archivo `api/service/sql/technical_interventions.sql` incluye el seed inmutable **`technical_pricing_20261016_v1`**. La misma sección figura en `sql/schema.sql`, por lo que una instalación nueva y una actualización reciben los mismos datos:

- Once grupos de tarifas por horas, distribuidos en 33 referencias de tipos o marca: primera hora, segunda a sexta y desde la séptima. Los valores de la tabla impresa incluyen el 34 % confirmado, redondeado a centavos, una sola vez.
- Prioridad de tarifa por marca para Inogen.
- Cuatro trabajos con precio completo: Polywatch, cánula nasal $35.000; pila $30.000; pila y software $40.000; reparación de compresores $140.000. Incluyen mano de obra y repuestos, sin aumento.

No se cargan usuarios de demostración, clientes, equipos, órdenes, repuestos, costos ficticios ni stock. EverFlo utiliza el repuesto existente y no se duplica ni se modifica con este seed. La tarifa general de diagnóstico y limpieza conserva la configuración administrativa existente; no se inventa un importe cuando falta.

## Cómo se aplicará al actualizar

El arranque de la API ya llama a `prepare_production_schema`. Este comando incluye `apply_technical_interventions_schema`, que aplica las tablas y el seed en una transacción. Por lo tanto, cuando se incorpore esta rama al despliegue autorizado, la preparación existente materializará los precios iniciales en PostgreSQL; no habrá que ejecutar los seeds de demostración ni depender de un usuario de prueba.

La tabla `technical_data_seeds` registra el nombre y la fecha de aplicación. Un bloqueo transaccional serializa el seed y el registro se escribe al terminar. Si hay un error, sus cambios se revierten y se puede reintentar.

En la primera aplicación, las tarifas horarias existentes tienen prioridad. Los trabajos se vinculan por nombre normalizado: se completan los importes vacíos, conservando los que ya estén configurados y sin reactivar trabajos desactivados. Después de registrar la versión, las siguientes preparaciones no vuelven a cargar los importes: tampoco restauran precios retirados, tarifas eliminadas ni valores anteriores a una edición administrativa. No se recalculan presupuestos ni se alteran documentos emitidos.

Los cambios posteriores de precios se realizan desde administración y se guardan en la base. El seed conserva los valores iniciales aprobados; no es una copia automática de todas las configuraciones o pruebas hechas en desarrollo.

## Comprobación en el futuro despliegue

Dentro del entorno de la API que se esté desplegando, el comando existente permite exigir que cualquier fallo de preparación se informe como error:

```sh
python manage.py prepare_production_schema --strict
```

Después se puede consultar el registro y la configuración persistida, en modo de solo lectura:

```sql
SELECT nombre, applied_at
FROM technical_data_seeds
WHERE nombre = 'technical_pricing_20261016_v1';

SELECT tipo_equipo, marca, primera_hora, hora_adicional, hora_desde_septima
FROM technical_labor_rates
ORDER BY tipo_equipo;

SELECT nombre, precio_final, activo
FROM technical_work_catalog
WHERE precio_final IS NOT NULL
ORDER BY nombre;
```

No ejecutar pruebas ni `seed_technical_dev_preview` en producción. Los comandos `load_technical_hourly_rates` y `load_technical_fixed_prices` continúan restringidos a la base aislada de desarrollo y no son necesarios para desplegar.
