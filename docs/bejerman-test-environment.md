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

En SEPID, `all` valida RIS online, registración manual y RT valorizado SitIVA 6. En MGBIO valida el RT valorizado; los escenarios RIS se omiten porque ese tipo de comprobante no está definido en la base MGBIO TEST.

El RT valorizado verifica:

- total final 121,00 para un neto NEXORA de 100,00;
- IVA discriminado en cero para SitIVA 6;
- read-back único del comprobante;
- un único movimiento de salida de stock en `VAL`;
- punto de venta `00004` para SEPID y `00007` para MGBIO.

No ejecutar estos comandos con `docker-compose.prod.yml` ni contra el WSDL productivo.
