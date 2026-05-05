# AGENTS.md

## Reglas permanentes de trabajo

1. Si una modificación impacta la base de datos (tablas, columnas, índices, constraints, tipos, triggers, etc.), **siempre** ejecutar el comando `apply` correspondiente en el entorno activo para materializar el cambio.
2. Todo cambio de estructura de base de datos debe quedar reflejado también en `sql/schema.sql` dentro del mismo trabajo.
3. No cerrar una tarea con cambios de DB sin validar que el objeto exista/esté aplicado (por ejemplo, comprobando tabla/índice/campo en PostgreSQL).
4. Si no existe un `apply_*_schema` para el cambio requerido, crear el mecanismo equivalente (migración o comando de esquema) y ejecutarlo igualmente.
5. Tenés que trabajar siempre en UTF-8, tenés prohibido introducir mojibakes de caracteres y no podés tener faltas de ortografía. Tiene que estar todo escrito correctamente en español.
6. Regla obligatoria y sin excepción: cada cambio que afecte DB exige dos acciones en la misma tarea: (a) ejecutar su `apply` y (b) actualizar `sql/schema.sql`.
