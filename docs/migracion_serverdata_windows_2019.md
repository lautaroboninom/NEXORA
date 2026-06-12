# Migración SERVERDATA a Windows Server 2019

Fecha: 2026-06-12

## Objetivo

Migrar SERVERDATA desde Windows Server 2008 Standard SP2 a Windows Server 2019 Standard, preferentemente con instalación limpia sobre SSD nuevo, conservando el HDD actual como rollback.

## Hardware actual relevado

- Equipo: IBM System x3100 M4 Type 2582.
- CPU: Intel Xeon E3-1220 v2, 4 núcleos / 4 hilos.
- RAM: 4 GB ECC DDR3, 1 módulo Samsung 4 GB.
- Slots RAM: 4.
- Máximo RAM soportado: 32 GB.
- Disco principal: HDD 1 TB, unidad C: con datos y sistema.
- Backup externo: existe backup externo en SSD, pendiente validar restauración.

## Compra recomendada

- RAM: 4 módulos iguales de 8 GB DDR3 ECC UDIMM, PC3-10600E 1333 MHz o PC3-12800E 1600 MHz, 240 pines, unbuffered, no registered.
- SSD: SATA 2.5 pulgadas, ideal clase enterprise con power-loss protection.
- Adaptador: soporte/caddy 2.5 a 3.5 pulgadas para montar el SSD en bahía del servidor.
- Licencia: Windows Server 2019 Standard y CALs correspondientes si aplican.

## Estrategia recomendada

1. No actualizar encima del Windows Server 2008 actual.
2. Hacer imagen completa o clon del HDD actual.
3. Retirar o preservar el HDD actual sin modificarlo.
4. Instalar Windows Server 2019 limpio en SSD nuevo.
5. Migrar servicios, datos y configuraciones.
6. Probar con usuarios antes de declarar el cambio productivo.

## Migrar además de los datos

### Recursos compartidos

- `C:\Datos` -> share `Datos`.
- `C:\Datos\Servicio Tecnico` -> share `Servicio Tecnico`.
- `C:\Gerencia` -> share `Gerencia`.
- `C:\Imagenes` -> share `Imagenes`.
- `C:\Jazz` -> share `Jazz`.
- `C:\PASE` -> share `PASE`.
- `C:\utiles` -> share `utiles`.
- `C:\Program Files (x86)\Trend Micro\Security Server\PCCSRV` -> share `ofcscan`, si Trend Micro sigue en uso.
- `C:\Users\Administrador\Desktop\radmin` -> share `radmin`, si todavía se usa.

Para cada share migrar:

- Ruta física.
- Nombre del recurso compartido.
- Permisos de recurso compartido.
- Permisos NTFS.
- Grupos y usuarios asociados.
- Mapas de unidades en PCs, especialmente `Z:` apuntando a `\\SERVERDATA\Datos`.

### Impresoras

- Impresora compartida `2040` / Brother HL-2040.
- Drivers de impresión.
- Share `print$`.
- Configuración de cola de impresión y PCs que imprimen por SERVERDATA.

### IIS / Web

- Rol IIS.
- Sitios y bindings.
- Puerto 80 actualmente abierto.
- Configuración de aplicaciones web existentes.
- Certificados, si existieran.
- Archivos y configuraciones bajo IIS.

### Bases de datos

- SQL Server Express: instancia `SQLEXPRESS`.
- SQL Server Browser.
- SQL Server VSS Writer.
- Bases, logins, usuarios, permisos y jobs si existen.
- MySQL 5.0 en `C:\MySQL\MySQL Server 5.0`.
- Archivo `my.ini`.
- Datadir de MySQL.
- Usuarios, claves, permisos y servicios que dependan de MySQL.

### Antivirus / seguridad

- Trend Micro Security Server.
- Trend Micro Security Agent.
- Apache2 de Trend Micro en `C:\Program Files (x86)\Trend Micro\Security Server\PCCSRV\Apache2`.
- Configuración de consola, agentes y repositorios.
- Validar si conviene reinstalar versión moderna compatible con Server 2019 en vez de migrar la instalación vieja.

### Backups

- Cobian Backup 11.
- Servicio `cbVSCService11`.
- Tareas configuradas.
- Destinos de backup.
- Credenciales guardadas.
- Validar restauración desde el SSD externo antes de migrar.
- Crear nuevo plan de backup para Server 2019.

### Acceso remoto

- RDP publicado en puerto 3900.
- Terminal Services.
- Licenciamiento de Terminal Services.
- TeamViewer 8, si todavía se usa.
- Radmin/VNC están instalados pero detenidos/deshabilitados; confirmar si se eliminan o reinstalan.

### Red

- Nombre del equipo: `SERVERDATA`.
- Dominio/grupo: `MGBIO`.
- IPs actuales:
  - `10.0.0.200`
  - `10.10.10.200`
- Gateway: `10.0.0.2`.
- DNS actuales:
  - `8.8.8.8`
  - `8.8.4.4`
- MAC de interfaz principal: `34:40:B5:8F:7C:8E`.
- Reglas de firewall.
- Puertos actualmente detectados abiertos: 80, 135, 139, 445, 3306, 3900.

### Usuarios y permisos

- Usuarios locales.
- Grupos locales.
- Contraseñas o plan de rotación de credenciales.
- Permisos administrativos.
- Tareas programadas.
- Servicios configurados con usuarios específicos.

### Aplicaciones heredadas

- Jazz.
- PASE.
- Aplicaciones o accesos usados desde `C:\Datos`.
- Componentes antiguos que puedan depender de Windows Server 2008, MySQL 5.0, SQL Express o rutas absolutas.

## Validaciones antes del corte

- Abrir archivos desde PCs usando los shares.
- Confirmar que `Z:` sigue funcionando o actualizar mapeos.
- Probar impresión por Brother HL-2040.
- Probar aplicaciones que usan MySQL.
- Probar aplicaciones que usan SQL Express.
- Probar IIS/servicios web actuales.
- Probar backups y restauración.
- Probar acceso remoto por RDP.
- Revisar eventos de Windows.
- Confirmar espacio libre y estado SMART del SSD.

## Plan de rollback

- Mantener HDD viejo intacto al menos hasta terminar validaciones.
- Documentar cómo volver a arrancar desde el HDD viejo.
- Conservar imagen completa del disco original.
- No borrar backup externo anterior hasta tener varios backups correctos del Server 2019.
