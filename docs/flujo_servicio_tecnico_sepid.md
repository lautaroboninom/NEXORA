# Flujo integral del servicio técnico SEPID

Documento fuente para el diagrama interno del circuito completo del servicio tecnico.

## Objetivo

Mostrar, en lenguaje operativo, el recorrido completo del equipo desde el ingreso hasta la entrega, incluyendo:

- ingreso por Recepción o por Logística
- remito de ingreso y alta administrativa en el sistema
- diagnóstico y definición técnica
- presupuesto con y sin stock propio
- consulta a proveedor, compra y espera por repuesto
- reparación, test, liberación, remito/factura y entrega
- excepciones de derivacion externa, baja y entrega especial/alquiler

## Estructura recomendada del PDF

La version visual ya no se plantea como una unica lamina. Para mejorar legibilidad y evitar flechas cruzadas:

- Pagina 1: flujo principal desde el ingreso hasta la entrega, incluyendo presupuesto directo y rechazo con cobro de diagnostico.
- Pagina 2: subflujo de proveedor/repuesto, espera o retiro sin reparar, mas las excepciones compactas.

Esta division sigue una logica mas cercana a BPMN/cross-functional flowcharts: un diagrama principal limpio y los desbordes complejos en una segunda hoja.

## Responsables principales

- Recepción: recibe equipos por mostrador y realiza la entrega final al cliente cuando aplica.
- Logística: puede iniciar el ingreso, mover físicamente equipos o repuestos y participar en recepciones/entregas especiales.
- Administración: recibe remitos, da alta la OS en el sistema y gestiona documentación de salida.
- Taller/Jefatura: diagnostica, define la necesidad técnica, emite presupuesto, sigue aprobación con cliente y gestiona proveedor/repuesto.
- Cliente: aprueba o rechaza presupuesto; también decide esperar repuesto o retirar el equipo.
- Proveedor: informa disponibilidad/precio y entrega repuestos cuando hay compra aprobada.

## Diagrama Mermaid de respaldo

```mermaid
flowchart LR
  subgraph CLI[Cliente]
    C_OK{Aprueba<br/>presupuesto?}
    C_WAIT{Esperar repuesto<br/>o retirar?}
  end

  subgraph REC[Recepción]
    R_IN[Ingreso por<br/>Recepción]
    R_REM[Remito de ingreso]
    R_OUT[Entrega al cliente]
  end

  subgraph LOG[Logística]
    L_IN[Ingreso por<br/>Logística]
    L_REM[Remito de ingreso]
    L_PART[Recibe repuesto y<br/>lo entrega a taller]
  end

  subgraph ADM[Administración]
    A_OS[Alta de OS / ingreso<br/>en sistema<br/>[ingresado]]
    A_COB[Cobro diagnóstico]
    A_LIB[Liberación, remito y factura<br/>[liberado]]
  end

  subgraph TALLER[Taller / Jefatura]
    T_DIA[Diagnóstico y definición técnica<br/>[diagnosticado]]
    T_NA{Presupuesto<br/>no aplica?}
    T_STOCK{Hay repuesto<br/>propio?}
    T_PRES[Emitir presupuesto al cliente<br/>[presupuestado]]
    T_ORDER[Pedir repuesto o reservar stock]
    T_REP[Reparación y test técnico<br/>[reparar / reparado]]
    T_WAIT[En espera de repuesto<br/>estado operativo manual]
    T_NR[No reparado<br/>[no_reparado]]
  end

  subgraph PROV[Proveedor]
    P_ASK[Consulta de disponibilidad<br/>y precio]
    P_OK{Disponible y con<br/>precio viable?}
    P_SEND[Proveedor despacha repuesto]
  end

  R_IN --> R_REM --> A_OS
  L_IN --> L_REM --> A_OS
  A_OS --> T_DIA

  T_DIA --> T_NA
  T_NA -- Sí --> T_REP
  T_NA -- No --> T_STOCK

  T_STOCK -- Sí --> T_PRES
  T_STOCK -- No --> P_ASK --> P_OK
  P_OK -- Sí --> T_PRES
  P_OK -- No --> C_WAIT

  T_PRES --> C_OK
  C_OK -- Sí --> T_ORDER
  C_OK -- No --> A_COB --> R_OUT

  T_ORDER -- Compra aprobada --> P_SEND --> L_PART --> T_REP
  T_ORDER -- Stock propio --> T_REP

  C_WAIT -- Esperar --> T_WAIT
  T_WAIT -. Reconsulta cuando hay stock .-> P_ASK
  C_WAIT -- Retirar --> T_NR --> R_OUT

  T_REP --> A_LIB --> R_OUT

  T_DIA -. Derivación externa .-> X_DER[Derivar a tercero]
  X_DER -. Retorna a taller .-> T_DIA
  T_DIA -. Baja .-> X_BAJA[Registrar baja y cerrar]
  A_LIB -. Entrega especial / alquiler .-> X_ALQ[Despacho especial / alquilado]
```

## Notas de interpretacion

- Convencion visual del PDF: las decisiones de aprobacion reciben el flujo principal desde arriba; la aprobacion baja y el rechazo sale por el lateral mas limpio.
- `En espera de repuesto` se muestra como estado operativo del proceso, no como un estado real implementado hoy en el sistema.
- `Cobro diagnóstico` aplica cuando el presupuesto es rechazado por el cliente.
- Si el proveedor no tiene disponibilidad o el precio no cierra, el cliente elige entre esperar o retirar el equipo.
- Si el cliente retira por falta de repuesto, el flujo termina como `No reparado` y sin cobro de diagnóstico.
- Taller/Jefatura concentra presupuesto, relación con cliente sobre aprobación y gestión con proveedor.
- Administración registra la OS y la documentación de salida, pero no compra repuestos ni emite presupuesto.

## Escenarios cubiertos

- ingreso por Recepción
- ingreso por Logística
- remito de ingreso y alta administrativa
- diagnóstico con presupuesto `no aplica`
- presupuesto usando stock propio
- presupuesto con consulta a proveedor
- proveedor con disponibilidad y compra aprobada
- proveedor sin disponibilidad o sin precio viable
- espera por repuesto
- retiro sin reparar por falta de repuesto
- rechazo de presupuesto con cobro de diagnóstico
- reparación, test, liberación y entrega
- derivación externa con retorno
- baja
- alquilado o entrega especial
