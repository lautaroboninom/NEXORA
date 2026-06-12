# Mejoras futuras para SERVERDATA

## Placa PCIe USB 3.0 / USB 3.1 Gen 1

Queda como mejora recomendada a futuro instalar una placa PCIe USB 3.0 o USB 3.1 Gen 1 para acelerar los backups hacia el SSD externo.

### Motivo

- El IBM System x3100 M4 tiene puertos USB 2.0 integrados.
- USB 2.0 limita mucho la velocidad de copia hacia discos externos modernos.
- Una placa PCIe USB 3.0/3.1 permite aprovechar mejor un SSD externo de backup.
- No modifica Bejerman, SQL, MySQL ni las carpetas compartidas.

### Requisitos de compra

- Formato PCIe, preferentemente PCIe x1.
- Compatibilidad con Windows Server 2019 o, como mínimo, drivers Windows 10/11 64-bit.
- Puertos USB-A si el disco externo usa cable USB-A.
- Puerto USB-C solo si el disco externo lo requiere.
- Preferible que tenga alimentación adicional SATA o Molex para dar energía estable a discos externos.

### Prioridad

No es urgente para la migración. Prioridad recomendada:

1. RAM ECC UDIMM hasta 32 GB.
2. SSD SATA interno para Windows Server y servicios.
3. Placa PCIe USB 3.0/3.1 para mejorar velocidad de backup externo.
