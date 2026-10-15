# Entorno Bejerman TEST para desarrollo

Este entorno permite probar integraciones reales desde NEXORA dev sin utilizar las bases productivas de Bejerman.

## Configuración segura

El archivo `.env` de desarrollo debe contener:

```env
BEJERMAN_WSDL_URL=http://45.173.2.155/Bejerman-SDK-WS-TEST/EFlexSDK_Service.svc
BEJERMAN_COMPANY_SEPID=SEP
BEJERMAN_COMPANY_MGBIO=MGBI
BEJERMAN_COMPANY_TEST=SEP
```

El usuario y la contraseña del SDK se mantienen únicamente en `.env`; no deben incorporarse al repositorio.

El modo de prueba se define en el IIS `Bejerman-SDK-WS-TEST` mediante `paramDbPrueba=1`. Por eso las empresas continúan siendo `SEP` y `MGBI`; `MODE` no es una empresa válida en este entorno.

Los comandos de seed y smoke rechazan la ejecución si el WSDL no contiene `Bejerman-SDK-WS-TEST`.

## Reconstrucción del API dev

```powershell
docker compose -f docker-compose.yml build api
docker compose -f docker-compose.yml up -d --no-deps api
```

## Seeds idempotentes

Aplicar en las dos bases TEST:

```powershell
docker compose -f docker-compose.yml exec -T api python manage.py seed_bejerman_test_environment --company-key SEPID --apply
docker compose -f docker-compose.yml exec -T api python manage.py seed_bejerman_test_environment --company-key MGBIO --apply
```

El seed verifica o crea:

- cliente responsable inscripto `NXT001`;
- cliente SitIVA 6 `NXT006`;
- cliente exento SitIVA 5 `NXT005`;
- artículo con partida `NXTPART`;
- artículo sin partida `NXTNPRT`;
- stock mínimo configurable de `NXTNPRT` en `VAL` —10 unidades por defecto—;
- disponibilidad de los depósitos operativos `STR`, `STL` y `VAL`.

`STC` y `DES` existen en las bases TEST, aunque `ObtenerDepositos` puede no enumerarlos mientras no tengan movimientos visibles.

Repetir los comandos sin `--apply` verifica que el seed quedó aplicado sin crear datos adicionales.

## Smoke real

```powershell
docker compose -f docker-compose.yml exec -T api python manage.py smoke_bejerman_remitos_test --company-key SEPID --scenario all
docker compose -f docker-compose.yml exec -T api python manage.py smoke_bejerman_remitos_test --company-key MGBIO --scenario all
```

En SEPID, `all` valida RIS online, registración manual y RT valorizados SitIVA 6 y SitIVA 5. En MGBIO valida los dos RT valorizados; los escenarios RIS se omiten porque ese tipo de comprobante no está definido en la base MGBIO TEST.

El escenario `emission` relee el RIS emitido: exige `ActualizaStock=S`, un renglón del artículo y partida de prueba con cantidad `-1` en ambas unidades de medida, y exactamente una unidad final de ese artículo en `STR`.

El RT valorizado verifica:

- SitIVA 6: neto 100,00, IVA 21,00 y total 121,00; precio unitario e `Item_Importe` con IVA incluido, con neto e IVA discriminados;
- SitIVA 5: neto 40,00, IVA 8,40 y total 48,40; precio unitario e `Item_Importe` con IVA incluido, con neto e IVA discriminados;
- read-back único del comprobante;
- read-back de neto, IVA, alícuota y total iguales a los importes enviados;
- un único movimiento de salida de stock en `VAL`;
- punto de venta `00004` para SEPID y `00007` para MGBIO.

No ejecutar estos comandos con `docker-compose.prod.yml` ni contra el WSDL productivo.

Para verificar monotributistas con precios en USD, dos unidades y descuento del 10 %:

```powershell
docker compose -f docker-compose.yml exec -T api python manage.py smoke_bejerman_remitos_test --company-key SEPID --scenario rt-valued --price-currency USD --quantity 2 --discount-percent 10
```

El caso conserva precio neto unitario 100,00, neto descontado 180,00, IVA 37,80 y total 217,80. La cotización 1535 usada por este smoke es un dato fijo del ensayo TEST.

### Aceptación de monotributistas del 14/10/2026

El contrato anterior con precio unitario neto fue rechazado por SEPID TEST por diferencias entre ítems y cabecera. Con precio unitario e `Item_Importe` finales, manteniendo neto, tasa e IVA discriminados, la lectura posterior confirmó:

| Empresa TEST | RT sin descuento | RT en USD con dos unidades y 10 % de descuento |
| --- | --- | --- |
| SEPID | R 00004-00004391: neto 100,00 + IVA 21,00 = 121,00 | R 00004-00004392: neto 180,00 + IVA 37,80 = 217,80 |
| MGBIO | R 00007-00000351: neto 100,00 + IVA 21,00 = 121,00 | R 00007-00000352: neto 180,00 + IVA 37,80 = 217,80 |

Los cuatro casos conservaron `Cliente_SitIVA=6`, la tasa del 21 % y la salida exacta de stock. Esta aceptación se ejecutó exclusivamente contra Bejerman TEST.

## Aceptación segura del circuito COMPRAS

El ingreso de mercadería es un circuito independiente de VENTAS. Su perfil fijo es:

- empresa NEXORA `SEPID`;
- circuito Bejerman `COMPRAS`;
- comprobante `RT`;
- operación `MC`;
- depósito `VAL`;
- numeración externa `N`;
- registración `R`.

Para prevalidaciones y ensayos masivos mantener siempre:

```env
BEJERMAN_PURCHASE_EMISSION_ENABLED=0
BEJERMAN_PURCHASE_CURRENCY=1
BEJERMAN_PURCHASE_EXCHANGE_TYPE=UNI
BEJERMAN_PURCHASE_EXCHANGE_RATE=1
```

La combinación `1 / UNI / 1` corresponde a la moneda local y al tipo de cambio
existentes en la base Bejerman TEST. El preflight falla de forma cerrada si
alguno de esos tres valores falta.

En RT/MC valorizados, el importe de cabecera es `neto + IVA`. La tasa y el
importe de IVA deben viajar también discriminados en cada renglón; NEXORA
valida localmente que la suma de ítems e IVA coincida con la cabecera antes de
llamar al SDK.

Los activos oficiales para una aceptación controlada son:

- proveedor `MGBIO`;
- artículo con partida `NXTPART`;
- depósito `VAL`.

La primera escritura debe ser un único RT de una unidad y sólo puede ejecutarse
contra `Bejerman-SDK-WS-TEST`, con autorización explícita. Antes de habilitarla,
comprobar en la pantalla de Mercadería que el banner indique `Entorno Bejerman:
TEST`.

Después de registrar el RT:

1. no volver a emitir mientras el lote esté `running`, `verifying` o
   `failed_review`;
2. usar **Verificar en Bejerman** hasta obtener evidencia de cabecera RT/MC,
   proveedor, fecha, total e ítems;
3. confirmar el incremento exacto de stock en `VAL` y la partida utilizada;
4. aceptar el lote como terminado únicamente cuando su estado sea `generated`;
5. volver a configurar `BEJERMAN_PURCHASE_EMISSION_ENABLED=0` y recrear el API
   de desarrollo.

### Aceptación ejecutada el 29/07/2026

La aceptación mínima autorizada se realizó en Bejerman TEST con:

- RT `R 00001-29120948`;
- proveedor `MGBIO`;
- artículo `NXTPART`;
- partida `NX260729120948`;
- depósito `VAL`;
- cantidad `1`;
- neto `100,00`, IVA `21,00` y total `121,00`.

El read-back encontró una única cabecera `RT/MC`, fecha y total exactos, un
único ítem con fingerprint coincidente, stock `NXTPART` en `VAL` de `0` a `1`
y una única partida en `VAL`. El lote quedó `generated` con todas las
postcondiciones confirmadas.

Antes de la aceptación hubo dos rechazos explícitos de Bejerman que indicaron
que el comprobante no se importó: primero por moneda/tipo de cambio vacíos y
luego por IVA no discriminado en el renglón. Ambos casos mantuvieron RT, stock
y partida ausentes. Las dos condiciones quedaron corregidas y cubiertas por
validaciones locales antes de la llamada al SDK.

No se realizan pruebas del circuito COMPRAS en producción.
