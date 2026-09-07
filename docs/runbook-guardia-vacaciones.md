# Guardia de NEXORA — qué mirar y qué hacer

**Para quién es esto:** la persona que queda a cargo mientras Lautaro no está.
No hace falta saber programar. Todo lo que se nombra acá se hace desde la pantalla
de NEXORA, con el navegador.

**La idea de fondo:** el sistema ya se destraba solo en la enorme mayoría de los
casos. Los procesos que fallan se reintentan solos, los que se quedan a mitad de
camino se recuperan solos, y los avisos salen solos. Lo que queda para una persona
son los casos donde **hay que decidir algo**, y son pocos. Este documento es la
lista de esos casos.

**La regla más importante está en la sección 4.** Si leés una sola cosa, que sea esa.

---

## 1. La revisión de todos los días (5 minutos)

Una vez por día, a la mañana. Con esto alcanza.

### 1.1 ¿El sistema está vivo?

Entrá a NEXORA como cualquier día. Si carga y podés ver la lista de ingresos, está
vivo. No hace falta nada más.

Si **no** carga, andá directo a la sección 6.

### 1.2 El panel de procesos de Bejerman

Menú lateral → **Sincronización Bejerman**.

Mirá la columna de estados. Lo que importa:

| Estado | Qué significa | Qué hacés |
|---|---|---|
| **Pendiente** | Está en la cola, le toca en un rato | Nada. Se resuelve solo |
| **Ejecutando** | Está trabajando ahora mismo | Nada |
| **Completado** | Salió bien | Nada |
| **Falló** | Falló pero va a reintentar solo | **Nada por 24 horas.** Si mañana sigue igual, sección 3 |
| **Bloqueado** | Se le acabaron los reintentos, necesita una persona | Sección 3 |
| **En revisión** | No se sabe si el remito salió o no | Sección 3.2 — **es el caso delicado** |
| **Cancelado** | Alguien lo cerró a mano | Nada |

**Un "Falló" recién aparecido no es una urgencia.** El sistema reintenta con esperas
cada vez más largas justamente para aguantar que Bejerman se caiga un rato. Recién
cuando pasa a **Bloqueado** dejó de intentar por su cuenta.

### 1.3 Tu correo

Si algo se bloquea de verdad, te llega un mail con asunto
**`[NEXORA] Sincronización Bejerman bloqueada`**. Ese mail sale solo, dos horas
después de que el proceso se bloqueó. Si no te llegó ninguno, no hay nada bloqueado.

También llegan todos los días:
- **07:30** — alertas de presupuestos pendientes y preventivos vencidos.
- **08:00** — resumen de facturación pendiente para Cobranzas.

**Si un día no llega ninguno de los tres, algo pasa.** Ver sección 6.

---

## 2. Lo que se arregla solo (no toques nada)

Esto está acá para que no pierdas tiempo con cosas que ya se están resolviendo.

- **Un proceso en "Falló" que reintenta.** Espera cada vez más entre intentos.
  Dejalo. La mayoría sale en el segundo o tercer intento.
- **Un proceso que quedó "Ejecutando" desde ayer.** A la media hora el sistema se da
  cuenta de que no quedó nadie atrás y lo devuelve a la cola solo.
- **Una emisión de remito que quedó "en curso" y nunca contestó.** A los 10 minutos
  la pantalla te dice que verifiques; a los 20 el sistema cierra la fila solo.
- **Un job bloqueado porque a alguien le faltaban las credenciales de Bejerman.** En
  cuanto la persona las carga en su perfil, el proceso se reabre solo.
- **Errores que dicen `ServiceActivationException` o "el servicio no responde".** Es
  Bejerman que se durmió. Se despierta solo y el reintento entra bien.

---

## 3. Los casos que sí necesitan una persona

### 3.1 Un proceso quedó **Bloqueado**

Abrí la fila. El panel te muestra tres cosas: **Qué ocurrió**, **Cómo resolverlo** y
el detalle técnico. Leé "Cómo resolverlo" — está escrito para este momento.

Los motivos que aparecen seguido:

| Dice algo como | Qué pasa de verdad | Qué hacer |
|---|---|---|
| "Falta stock" / "stock insuficiente" | En Bejerman el equipo no está en el depósito del que se lo quiere sacar | Fijate en Bejerman dónde está la partida. Si está en otro depósito, hay que moverla ahí primero |
| "No se encontró el artículo" | El equipo de NEXORA no está mapeado a un artículo de Bejerman | Botón **Resolver artículo Bejerman** en la misma fila. Te sugiere candidatos |
| "Faltan las credenciales" | El usuario que lo disparó no tiene cargada su clave de Bejerman | Que la persona entre a su perfil y las cargue. El proceso se reabre solo |
| "Tipo de sincronización sin implementación" | Basura vieja de una auditoría | **Cancelar**, dejando escrito el motivo |

Cuando arreglaste la causa, botón **Reintentar**.

### 3.2 Un proceso quedó **En revisión** — leé esto entero

Este es el caso delicado, y es el único donde apretar el botón equivocado hace daño
de verdad.

**Qué significa:** NEXORA le mandó el remito a Bejerman y **no sabe si llegó o no**.
Se cortó la conexión, o Bejerman contestó algo ambiguo. El remito puede estar emitido
o puede no estarlo, y las dos cosas son posibles.

**Por qué importa:** si reintentás y el remito ya había salido, queda **emitido dos
veces**. Eso es un problema real: dos remitos, dos movimientos de stock, y alguien
tiene que ir a Bejerman a anular uno.

**Qué hacer, en este orden:**

1. **Botón "Verificar en Bejerman".** Busca el comprobante sin emitir nada. Es
   gratis y no rompe nada. En la mayoría de los casos lo encuentra y cierra la fila
   solo. **Empezá siempre por acá.**

2. Si la verificación no lo resuelve, andá a Bejerman y buscalo a mano. El panel te
   dice qué buscar (cliente, fecha, número de serie).

3. Volvé al panel, botón **Resolver revisión**, y elegí lo que viste:
   - **"El remito existe en Bejerman"** → cargás el número y la fila se cierra. No
     se emite nada nuevo.
   - **"No se emitió: lo verifiqué en Bejerman"** → recién ahí el sistema se
     habilita a emitirlo de nuevo.

**Nunca elijas "no se emitió" sin haber mirado Bejerman con tus ojos.** Ese botón es
una declaración, no una suposición.

### 3.3 Un conflicto de ingreso

Un equipo volvió a entrar al taller y su orden anterior seguía abierta. El remito de
ingreso ya salió; lo que falta es decidir qué pasa con la orden vieja.

El panel te ofrece buscar en Bejerman el comprobante de salida de la vez anterior. Si
lo encuentra, cierra la orden vieja con ese respaldo. Si no aparece, podés cerrarla
sin comprobante y queda asentado en el historial quién lo hizo.

No es urgente. Puede esperar a que Lautaro vuelva si tenés dudas.

---

## 4. Lo que NO hay que hacer

**Esta es la sección importante.**

### ⛔ No reintentar un remito que puede haber salido

Ya está explicado en 3.2, pero va de nuevo porque es el error caro. Hay dos frases
de Bejerman que **parecen** un rechazo y no lo son:

- *"El comprobante no se importó debido a que ya existe en la empresa"*
- *"El comprobante se importó correctamente. La fecha de contabilización se
  modificó..."*

Las dos significan **el comprobante existe**. Reintentar lo duplica. El sistema ya
reconoce estos dos textos y no te deja reintentar a ciegas, pero si alguna vez te
aparece la opción: no.

### ⛔ Un error de conexión no significa "no se ejecutó"

Si el mensaje habla de conexión cortada, timeout, o el servicio que no responde, eso
dice que **NEXORA no llegó a escuchar la respuesta**. No dice que la operación no
haya pasado. Van a "En revisión" a propósito. Tratalos como 3.2.

### ⛔ No reiniciar contenedores ni tocar el servidor

Si algo parece trabado, no reinicies nada. El sistema tiene sus propias
recuperaciones y reiniciar en el medio puede dejar trabajo a mitad de camino. Ver
sección 6.

### ⛔ No cargar comprobantes a mano en Bejerman "para arreglar"

Si cargás en Bejerman un remito que NEXORA cree que no salió, las dos bases quedan
diciendo cosas distintas y después nadie entiende qué pasó. Si tenés que hacerlo,
anotalo y avisá.

---

## 5. Los avisos que llegan por mail

| Aviso | Cuándo | Qué hacer |
|---|---|---|
| Sincronización Bejerman bloqueada | 2 h después de bloquearse | Sección 3.1 |
| Presupuestos pendientes | Todos los días 07:30 | Es para el equipo comercial |
| Preventivos vencidos / próximos | Todos los días 07:30 | Es para Servicio Técnico |
| Resumen de facturación pendiente | Todos los días 08:00 | Es para Cobranzas |
| Remito listo / equipo liberado | Cuando pasa | Circuito normal, no es una alerta |

Si **dejan de llegar** los de horario fijo, es una señal de que algo se cayó. No es
que "ese día no había nada": cuando no hay nada, el mail igual llega diciendo que no
hay nada.

---

## 6. Si se cae todo

**Síntomas:** NEXORA no carga, o carga pero nada se guarda, o dejaron de llegar
todos los mails.

**Lo primero, y muchas veces lo único:** el servidor SERVERDATA se reinició y no
terminó de levantar. Después de un reinicio tarda **unos 10 minutos** en volver solo.
Esperá 10 minutos antes de hacer nada.

**Si a los 10 minutos sigue caído:**

1. Entrá a SERVERDATA (por pantalla o por escritorio remoto).
2. En el escritorio hay un acceso directo: **"Rebuild NEXORA Producción"**.
3. Abrilo. Te muestra el estado y te ofrece reconstruir.

Eso arregla la enorme mayoría de las caídas. Tarda entre 10 y 40 minutos y muestra
en pantalla en qué paso va.

**Si el acceso directo tampoco lo levanta**, ahí sí es momento de llamar. No sigas
probando cosas: cada intento fallido puede dejar procesos colgados que después hay
que limpiar.

---

## 7. A quién llamar

| Situación | A quién |
|---|---|
| Duda sobre un caso del panel | Puede esperar. Anotalo |
| Un remito se emitió dos veces | Avisar a Cobranzas para que anulen uno en Bejerman |
| El sistema no levanta ni con el rebuild | Lautaro |
| Algo que huele a pérdida de datos | Lautaro, sin esperar |

Para todo lo demás: **anotarlo y esperar**. Un caso que queda sin resolver una semana
no rompe nada. Un caso mal resuelto sí.

---

## 8. Para el que sepa técnico

Comandos de diagnóstico, todos de sólo lectura, desde el contenedor `nexora-api`:

```bash
python manage.py bejerman_pdf_queue_status          # estado de la cola de PDFs y latido del worker
python manage.py recover_stuck_bejerman_work        # sin --apply: sólo informa qué está trabado
python manage.py limpiar_procesos_bejerman_trabados # sin --apply: simula la limpieza del panel
```

Ninguno de los tres escribe nada si no le pasás `--apply`.

**Suplencia temporal** — pone y saca a alguien como reemplazo del jefe. Cambia el rol,
agrega el permiso de presupuestos y prende las notificaciones de jefatura, guardando
cómo estaba la persona antes para poder devolverla exactamente ahí:

```bash
python manage.py suplencia --estado                              # qué hay abierto
python manage.py suplencia --activar --usuario <mail>            # simula
python manage.py suplencia --activar --usuario <mail> --apply    # aplica
python manage.py suplencia --cerrar  --usuario <mail> --apply    # revierte todo
```

Cerrar no es "volver a poner lo que parecía que tenía": reescribe el estado guardado
al abrir. Los permisos que la persona ya tenía por su cuenta nunca se le quitan.

Salud del sistema: `https://nexora.tail7bb880.ts.net/api/health/`

Los procesos que corren solos, y cada cuánto:

| Contenedor | Qué hace | Cada cuánto |
|---|---|---|
| `nexora-bejerman-sync` | Cola de stock + recuperación de trabados | 30 s (recuperación cada 5 min) |
| `nexora-bejerman-pdf` | Busca los PDF de los comprobantes | 2 s |
| `nexora-email-outbox` | Manda los correos encolados | 10 s |
| `nexora-billing-postponements` | Vencimientos de facturación pospuesta | 60 s |
| `nexora-daily-alerts` | Alertas de la mañana | 07:30 |
| `nexora-billing-summary` | Resumen de Cobranzas | 08:00 |
| `nexora-bejerman-articulos-sync` | Alinea descripciones SEPID → MG BIO | 6 h |
