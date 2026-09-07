# Suplencia temporal — diseño

**Problema:** cuando el jefe no está, sus notificaciones siguen saliendo hacia su rol
y las decisiones que sólo él puede tomar se acumulan. Hoy la única forma de que otra
persona las reciba es cambiarle el rol, que le da de más (incluida la configuración
de usuarios y permisos) y que después alguien tiene que acordarse de revertir.

**Solución:** una fila que dice *"esta persona está cubriendo a esta otra, desde tal
fecha, hasta que se cierre"*, con tres efectos y un solo botón para deshacerlos todos.

---

## 1. Qué NO hace

Conviene empezar por acá, porque define el alcance:

- **No le cambia el rol a nadie.** La suplente sigue siendo lo que era. Si alguien
  mira la ficha del usuario, ve su rol de siempre.
- **No da acceso a la configuración de usuarios ni de permisos.** Es la exclusión
  explícita que pidió el titular.
- **No es una delegación de identidad.** La suplente actúa como ella misma; el
  historial registra su nombre, no el del titular.

---

## 2. Los tres efectos

### 2.1 Permisos

Al abrir la suplencia se calculan los permisos efectivos del titular (los de su rol
más sus overrides personales), se les resta la lista de exclusión, se les resta lo
que la suplente **ya tenía**, y el resto se otorga como overrides `allow` en
`user_permission_overrides` — la tabla que ya existe y ya está en producción.

La fila de suplencia guarda **la lista exacta de códigos otorgados**. Eso es lo que
permite devolver todo al cerrar sin pisar nada: si la suplente ya tenía
`page.bejerman_sync` por su propio rol, ese código nunca se otorgó y por lo tanto
nunca se revoca.

**Lista de exclusión** (`SUPLENCIA_PERMISOS_EXCLUIDOS`):

| Código | Por qué queda afuera |
|---|---|
| `page.users` | Ver el padrón de usuarios |
| `action.users.manage` | Crear, editar y desactivar usuarios |
| `action.users.manage_permissions` | Editar permisos por usuario |

Que la lista viva en un solo lugar del código, y no repartida, es a propósito: es lo
primero que alguien va a querer revisar.

### 2.2 Notificaciones

`active_users_for_notification` arma la audiencia y después la filtra por el rol de
cada candidato (`_role_allowed_for_notification`). Ese filtro mira el rol **propio**
de la suplente, que es justamente el que no alcanza — por eso no basta con darle
permisos ni con cargarle una preferencia.

La suplencia se resuelve un paso antes: donde el titular entre en la audiencia, entra
también su suplente, salteando el filtro por rol. Vale para campana y para mail.

Dos detalles que importan:

- **La suplente no deja de recibir lo suyo.** Se suma, no se reemplaza.
- **El titular sigue recibiendo todo.** Si mira el mail desde el teléfono en el
  aeropuerto, lo va a ver. La suplencia es para que alguien más *también* lo vea, no
  para silenciarlo. Si en algún momento se quiere lo contrario, es un campo más
  (`silenciar_titular`), no un rediseño.

### 2.3 Rastro

Quién la abrió y cuándo, quién la cerró y cuándo, y el motivo. Al abrirse le llega a
la suplente un aviso que dice qué está cubriendo, desde cuándo y que es temporal.

---

## 3. Cómo se cierra

Dos caminos, y la diferencia importa:

- **A mano**, cuando el titular avisa que volvió. Es el camino normal.
- **Por fecha**, si se cargó `hasta`.

**Recomendación fuerte: cargar `hasta` unos días DESPUÉS de la vuelta prevista.**
El modo de falla que hay que evitar no es "la suplencia quedó abierta de más" —eso se
cierra en diez segundos— sino "la suplencia se venció el martes y el titular vuelve
el jueves", que deja tres días sin nadie mirando y sin que nadie se entere. Vencer
tarde es barato; vencer temprano no.

Al cerrarse se borran **exactamente** los overrides que se habían otorgado. La
suplente queda como estaba.

---

## 4. Esquema

```sql
CREATE TABLE IF NOT EXISTS suplencias (
  id                 INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  titular_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  suplente_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  motivo             TEXT NOT NULL DEFAULT '',
  desde              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  hasta              TIMESTAMPTZ NULL,
  estado             TEXT NOT NULL DEFAULT 'activa',
  permisos_otorgados JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_by          INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  closed_at          TIMESTAMPTZ NULL,
  CONSTRAINT chk_suplencias_estado CHECK (estado IN ('activa', 'cerrada')),
  CONSTRAINT chk_suplencias_distintos CHECK (titular_user_id <> suplente_user_id)
);

-- Una sola suplencia activa por titular: dos personas cubriendo lo mismo es un lío
-- de permisos que después nadie sabe desarmar.
CREATE UNIQUE INDEX IF NOT EXISTS uq_suplencias_titular_activa
  ON suplencias (titular_user_id) WHERE estado = 'activa';
```

El vencimiento por fecha lo barre el mismo ciclo de recuperación que ya corre cada
cinco minutos en el worker de sincronización, junto con el resto de lo que hay que
cerrar solo.

---

## 5. Qué falta decidir

1. **Quién es la suplente.** Tiene que ser un usuario que ya exista en NEXORA.
2. **Si el titular quiere seguir recibiendo los avisos** mientras no está. El diseño
   asume que sí (se suma, no se reemplaza).
3. **Fecha de cierre.** Ver la recomendación de la sección 3.
