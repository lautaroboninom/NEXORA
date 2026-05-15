ALTER TABLE ingresos MODIFY COLUMN motivo TEXT NOT NULL;
UPDATE ingresos
SET motivo='reparación'
WHERE HEX(motivo)='7265706172616369EFBFBD';
UPDATE ingresos
SET motivo='reparación alquiler'
WHERE HEX(motivo)='7265706172616369EFBFBD20616C7175696C6572';
-- mantener 'otros' cuando no hay equivalencia
ALTER TABLE ingresos MODIFY COLUMN motivo ENUM('reparación','service preventivo','baja alquiler','reparación alquiler','urgente control','devolución demo','otros') NOT NULL;
