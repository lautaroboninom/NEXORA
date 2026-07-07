# Funciones para Editar Remitos en Bejerman

NEXORA tiene dos sistemas principales para editar remitos en Bejerman sin emitir uno nuevo (como lo hacía Codex):

---

## 1. **EDITAR IVA DE REMITOS** (`bejerman_remito_iva_fix.py`)

### Problema que resuelve
Un remito se generó sin IVA porque el artículo en Bejerman no tiene código de IVA configurado en el campo `arttiv_CodVta`.

### Funciones principales

#### `collect_local_remito_iva_targets()`
**Detecta remitos locales que faltan IVA**

```python
from service.bejerman_remito_iva_fix import collect_local_remito_iva_targets

# Busca todos los remitos sin IVA
targets = collect_local_remito_iva_targets(
    company_key="SEPID",        # Empresa (SEPID o MGBIO)
    from_date="2026-07-01",     # Opcional: fecha mínima
    to_date="2026-07-06",       # Opcional: fecha máxima
    limit=10,                   # Opcional: máximo de candidatos
    include_existing=False      # Si incluir remitos ya reconciliados
)

for target in targets:
    print(f"{target.remito_number} | {target.customer_name}")
```

**Qué detecta:**
- Remitos con estado `generated` o `reconciled`
- Que tengan 0 IVA (están vacíos en SegTotV)
- Que el artículo TIENE IVA en la tabla `Articulos` de Bejerman

#### `build_bejerman_iva_fix_sql()`
**Genera SQL para corregir el IVA**

```python
from service.bejerman_remito_iva_fix import build_bejerman_iva_fix_sql

# Genera SQL de corrección
sql = build_bejerman_iva_fix_sql(
    targets,  # Lista de RemitoIvaTarget
    database="SBDASEP",  # Base de Bejerman (SBDASEP para SEPID, SBDAMGBI para MGBIO)
    apply=True,  # False = dry-run, True = aplicar cambios
    backup_name="MI_BACKUP_IVA_20260706"
)

# El SQL:
# 1. Hace backup de SegDetV, SegTotV, SegCabV
# 2. Obtiene el código de IVA del artículo
# 3. Actualiza SegDetV (sdvtiv_Cod)
# 4. Recalcula SegTotV (totales de IVA)
```

**Qué actualiza en Bejerman:**
```sql
-- Tabla: SegDetV (detalles de remitos)
UPDATE SegDetV SET
  sdvtiv_Cod = '01'          -- Código de IVA del artículo
  sdvart_TipoTasaVta = '1'   -- Tipo de tasa

-- Tabla: SegTotV (totales)
DELETE SegTotV WHERE stvscv_ID = @ID
INSERT INTO SegTotV (
  stvtiv_Cod = '01',
  stv_Tasa1 = 21,            -- % de IVA
  stv_ImpNetoEmi = 100.00,   -- Monto neto
  stv_Imp1Emi = 21.00        -- Monto de IVA
)
```

#### `write_reports()`
**Genera reportes en JSON, CSV y SQL**

```python
from service.bejerman_remito_iva_fix import write_reports

write_reports(
    "informes/mi_correccion_iva",
    targets=targets,
    sql_script=sql,
    metadata={"company_key": "SEPID", "sql_apply": True}
)

# Genera:
# - candidatos.json     → Lista de remitos a corregir
# - candidatos.csv      → Formato CSV
# - summary.json        → Metadatos de la ejecución
# - bejerman_remitos_iva_fix.sql  → SQL para ejecutar
```

### Comando de CLI

```bash
cd api

# 1. DRY-RUN: Ver qué se va a corregir (sin aplicar cambios)
python manage.py prepare_bejerman_remito_iva_fix \
  --company-key SEPID \
  --skip-local-scan \
  --remito "RT R 00004-00004760" \
  --customer-code "RESPILIFE" \
  --customer-name "RESPILIFE S.R.L." \
  --order-numbers "OE-00081"

# 2. APLICAR: Ejecutar la corrección con @Apply=1
python manage.py prepare_bejerman_remito_iva_fix \
  --company-key SEPID \
  --skip-local-scan \
  --remito "RT R 00004-00004760" \
  --customer-code "RESPILIFE" \
  --customer-name "RESPILIFE S.R.L." \
  --order-numbers "OE-00081" \
  --production-database \
  --sql-apply
```

**Parámetros:**
- `--company-key`: SEPID o MGBIO
- `--remito`: Número completo (ej: "RT R 00004-00004760")
- `--customer-code`: Código cliente en Bejerman
- `--customer-name`: Nombre del cliente
- `--order-numbers`: Órdenes NEXORA asociadas
- `--delivery-type`: sale | service_release (default: sale)
- `--correction-mode`: sale_article | repair_21 (se calcula automáticamente)
- `--production-database`: Usa base productiva (SBDASEP)
- `--sql-apply`: Aplica cambios (sin esto es dry-run)

**Modos de corrección:**
- `sale_article`: Toma IVA del artículo en Bejerman
- `repair_21`: Fuerza 21% (para reparaciones/service release)

---

## 2. **EDITAR MONEDA DE REMITOS USD** (`bejerman_remito_currency_fix.py`)

### Problema que resuelve
Un remito en USD se generó sin actualizar correctamente el tipo de cambio o la moneda en Bejerman.

### Funciones principales

#### `collect_local_usd_remito_targets()`
**Detecta remitos USD sin factura**

```python
from service.bejerman_remito_currency_fix import collect_local_usd_remito_targets

# Busca remitos USD que no fueron facturados
targets = collect_local_usd_remito_targets(
    company_key="SEPID",
    from_date="2026-07-01",
    to_date="2026-07-06",
    limit=10,
    include_all_delivery_types=False  # Solo sale por defecto
)

for target in targets:
    print(f"{target.remito_number} | USD {target.commercial_exchange_rate}")
```

**Qué detecta:**
- Remitos con `price_currency = 'USD'`
- Sin invoice_number (no facturados)
- Estado `generated`

#### `build_bejerman_currency_fix_sql()`
**Genera SQL para corregir moneda/tipo de cambio**

```python
from service.bejerman_remito_currency_fix import build_bejerman_currency_fix_sql

sql = build_bejerman_currency_fix_sql(
    targets,
    database="SBDASEP",
    apply=True,
    backup_name="MI_BACKUP_USD_20260706"
)

# El SQL actualiza en Bejerman:
# - Comprobante_Moneda
# - Comprobante_TipoCambio
# - Totales en moneda USD
```

### Comando de CLI

```bash
# Busca y corrige automáticamente remitos USD
python manage.py prepare_bejerman_remito_currency_fix \
  --company-key SEPID \
  --production-database \
  --sql-apply
```

---

## 3. **REMITOS DE ALQUILER** (`bejerman_rental_remito.py`)

Módulo especial para editar remitos de equipment rental/devolución.

```python
from service.bejerman_rental_remito import process_rental_remito_for_delivery

# Procesa remito de alquiler/devolución
result = process_rental_remito_for_delivery(
    remito_group_id="xxxxx",
    delivery_id="yyyyy"
)
```

---

## Flujo típico para editar un remito

### Caso 1: Sin IVA (como el remito 4760)

```python
# 1. Detectar
targets = collect_local_remito_iva_targets(company_key="SEPID", limit=5)

# 2. Ver candidatos
for t in targets:
    if "4760" in t.remito_number:
        print(t)  # RemitoIvaTarget

# 3. Generar SQL (dry-run primero)
sql = build_bejerman_iva_fix_sql(
    [target_para_4760],
    database="SBDASEP",
    apply=False  # ← DRY-RUN
)

# 4. Revisar reportes
# informes/bejerman_remitos_iva_XXXX/
#   - candidatos.json
#   - bejerman_remitos_iva_fix.sql (con @Apply=0)

# 5. Aplicar cambios
sql_final = build_bejerman_iva_fix_sql(
    [target_para_4760],
    database="SBDASEP",
    apply=True  # ← APLICAR
)

# 6. Ejecutar SQL en Bejerman
_remote_bejerman_sql_csv(sql_final, company_key="SEPID")
```

### Caso 2: Remito USD con tipo de cambio incorrecto

```python
targets = collect_local_usd_remito_targets(company_key="SEPID")
sql = build_bejerman_currency_fix_sql(targets, database="SBDASEP", apply=True)
_remote_bejerman_sql_csv(sql, company_key="SEPID")
```

---

## Tablas que se modifican en Bejerman

| Tabla | Campo | Qué se edita |
|-------|-------|-----------|
| **SegDetV** | sdvtiv_Cod | Código de IVA del artículo |
| **SegDetV** | sdvart_TipoTasaVta | Tipo de tasa de IVA |
| **SegTotV** | stvtiv_Cod | Código de IVA |
| **SegTotV** | stv_Tasa1 | % de IVA |
| **SegTotV** | stv_ImpNetoEmi | Monto neto |
| **SegTotV** | stv_Imp1Emi | Monto de IVA |
| **SegCabV** | Comprobante_Moneda | Moneda (USD/ARS) |
| **SegCabV** | Comprobante_TipoCambio | Tipo de cambio |

**IMPORTANTE:** Siempre hace BACKUP antes de modificar:
- `NEXORA_BKP_REMITOS_IVA_XXXXX_SegDetV`
- `NEXORA_BKP_REMITOS_IVA_XXXXX_SegTotV`
- `NEXORA_BKP_REMITOS_IVA_XXXXX_SegCabV`

---

## Ejemplos de uso

### Editar IVA de un remito específico por código

```python
from service.bejerman_remito_iva_fix import (
    target_from_row,
    build_bejerman_iva_fix_sql,
    correction_mode_from_delivery_types,
)

# Crear target manualmente
target = target_from_row({
    "group_id": "manual-1",
    "company_key": "SEPID",
    "remito_number": "RT R 00004-00004760",
    "customer_code": "RESPILIFE",
    "customer_name": "RESPILIFE S.R.L.",
    "order_numbers": "OE-00081",
    "delivery_types": "sale",
    "correction_mode": correction_mode_from_delivery_types("sale"),
})

# Generar y ejecutar SQL
sql = build_bejerman_iva_fix_sql([target], database="SBDASEP", apply=True)
from service.bejerman_delivery import _remote_bejerman_sql_csv
_remote_bejerman_sql_csv(sql, company_key="SEPID")
```

### Editar todos los remitos sin IVA en un rango de fechas

```bash
python manage.py prepare_bejerman_remito_iva_fix \
  --company-key SEPID \
  --from-date 2026-07-01 \
  --to-date 2026-07-06 \
  --production-database \
  --sql-apply
```

### Ver reportes antes de aplicar

```bash
# Genera reportes (sin aplicar)
python manage.py prepare_bejerman_remito_iva_fix \
  --company-key SEPID \
  --from-date 2026-07-01 \
  --limit 20

# Revisar
cat api/informes/bejerman_remitos_iva_*/candidatos.json
cat api/informes/bejerman_remitos_iva_*/bejerman_remitos_iva_fix.sql
```

---

## Validaciones

Antes de aplicar cambios, el script valida:

✅ El remito existe en Bejerman (SegCabV)
✅ El cliente coincide
✅ El artículo existe
✅ El IVA del artículo está configurado
✅ Los códigos de IVA están mapeados (01=21%, 03=10.5%, 04=0%)
✅ No hay líneas con base negativa

Si alguna validación falla, genera un reporte `REVISAR` con los detalles.

---

## Backup y recuperación

Si algo sale mal:

```sql
-- Restaurar desde backup
SELECT * INTO SegDetV_NEW 
FROM NEXORA_BKP_REMITOS_IVA_20260706_172002_SegDetV;

DELETE FROM SegDetV WHERE scv_ID IN (...);
INSERT INTO SegDetV SELECT * FROM SegDetV_NEW;
```

Todos los backups están en la base `SBDASEP` con prefijo `NEXORA_BKP_REMITOS_*`.
