"""Canal directo PC local -> SQL Server de Bejerman (servidor SAMTRONIC) por SSH.

Pensado para diagnosticos y correcciones puntuales de datos en las bases de
Bejerman sin pasar por los contenedores de produccion. Es deliberadamente el
camino "corto": PC local -> SSH SAMTRONIC -> PowerShell -> SqlClient.

NO usar el camino largo (ssh nexora -> wsl -> docker exec -> ssh SAMTRONIC) para
lotes de escritura: colgo el WSL de produccion el 27/07/2026. Ver
docs/modificaciones-directas-bejerman-sql.md.

Uso como libreria (lo habitual, permite reusar UNA sola conexion SSH):

    from samtronic_sql import Samtronic, DB_SEPID

    with Samtronic() as s:
        filas = s.query("SELECT TOP 10 * FROM dbo.Articulos", database=DB_SEPID)
        s.execute("SET XACT_ABORT ON; BEGIN TRAN; UPDATE ...; COMMIT TRAN;")

Uso como CLI (consultas rapidas de lectura):

    python scripts/samtronic_sql.py --db SBDASEP --sql "SELECT TOP 5 art_CodGen FROM dbo.Articulos"
    python scripts/samtronic_sql.py --db SBDAMGBI --file consulta.sql --csv salida.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import uuid
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.exit("Falta paramiko. Instalar con: pip install paramiko")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("NEXORA_ENV_FILE", REPO_ROOT / ".env.prod"))

SQL_SERVER_DEFAULT = "10.0.200.155,1433"
DB_SEPID = "SBDASEP"
DB_MGBIO = "SBDAMGBI"


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Lee un .env sin dependencias externas. Las credenciales nunca se hardcodean."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def sql_literal(value) -> str:
    """Literal de texto escapado. Prefijo N obligatorio para no perder tildes."""
    return "N'" + str(value if value is not None else "").replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    import re

    if not re.match(r"^[A-Za-z0-9_]+$", value or ""):
        raise ValueError(f"Identificador SQL no valido: {value}")
    return f"[{value}]"


class Samtronic:
    """Conexion SSH reutilizable contra SAMTRONIC.

    Abrir UNA vez y correr N consultas dentro del mismo `with`. Abrir una
    conexion por consulta en un loop es justamente lo que tumbo el servidor
    en el incidente del 27/07/2026.
    """

    def __init__(self, *, timeout: int = 60, sql_server: str = "") -> None:
        env = load_env()
        self.host = env.get("BEJERMAN_SSH_HOST") or os.getenv("BEJERMAN_SSH_HOST", "45.173.2.155")
        self.user = env.get("BEJERMAN_SSH_USER") or os.getenv("BEJERMAN_SSH_USER", "administrator")
        self.password = env.get("BEJERMAN_SSH_PASSWORD") or os.getenv("BEJERMAN_SSH_PASSWORD", "")
        self.sql_server = sql_server or env.get("BEJERMAN_SQL_SERVER") or SQL_SERVER_DEFAULT
        if not self.password:
            raise RuntimeError(f"Falta BEJERMAN_SSH_PASSWORD (buscado en {ENV_FILE} y el entorno)")
        self.timeout = timeout
        self.ssh: paramiko.SSHClient | None = None

    def __enter__(self) -> "Samtronic":
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(
            hostname=self.host,
            username=self.user,
            password=self.password,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        return self

    def __exit__(self, *exc) -> None:
        if self.ssh:
            self.ssh.close()
            self.ssh = None

    def _run_ps(self, ps_script: str, timeout: int) -> tuple[int, str, str]:
        if self.ssh is None:
            raise RuntimeError("Usar dentro de un bloque `with Samtronic() as s:`")
        remote_path = f"C:/Windows/Temp/nexora_sql_{uuid.uuid4().hex}.ps1"
        sftp = self.ssh.open_sftp()
        try:
            # BOM UTF-8 obligatorio: Windows PowerShell 5.1 sin BOM lee mal el
            # .ps1 y corrompe las tildes (a -> 2 caracteres), lo que puede
            # desbordar columnas varchar al escribir texto en espanol.
            with sftp.file(remote_path, "wb") as handle:
                handle.write(ps_script.encode("utf-8-sig"))
        finally:
            sftp.close()
        try:
            _, stdout, stderr = self.ssh.exec_command(
                f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_path}"',
                timeout=timeout,
            )
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
        finally:
            try:
                self.ssh.exec_command(
                    f"powershell -NoProfile -Command \"Remove-Item -LiteralPath '{remote_path}' -Force -EA SilentlyContinue\""
                )
            except Exception:
                pass
        return code, out, err

    def _check(self, code: int, err: str) -> None:
        # Este canal puede devolver rc=0 con el error unicamente en stderr: si
        # no se mira stderr, una consulta rota con un nombre de columna
        # inexistente parece "0 filas" y se saca una conclusion falsa.
        if code != 0 or err.strip():
            raise RuntimeError(f"SQL fallo (rc={code}):\n{err[:3000]}")

    def query(self, sql: str, *, database: str = DB_SEPID, timeout: int = 180) -> list[dict]:
        """Ejecuta SQL de lectura y devuelve las filas como lista de dicts."""
        ps = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sql = @'
{sql}
'@
$cs = 'Server={self.sql_server};Database={database};Integrated Security=SSPI;TrustServerCertificate=True;Connection Timeout=15'
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
try {{
  $cmd = $conn.CreateCommand()
  $cmd.CommandTimeout = {max(timeout - 20, 30)}
  $cmd.CommandText = $sql
  $adapter = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
  $dt = New-Object System.Data.DataTable
  [void]$adapter.Fill($dt)
  $rows = @()
  foreach ($row in $dt.Rows) {{
    $obj = [ordered]@{{}}
    foreach ($col in $dt.Columns) {{ $obj[$col.ColumnName] = $row[$col.ColumnName] }}
    $rows += [pscustomobject]$obj
  }}
  $csv = $rows | ConvertTo-Csv -NoTypeInformation
  foreach ($line in $csv) {{ [Console]::Out.WriteLine($line) }}
}} finally {{ $conn.Close() }}
""".strip()
        code, out, err = self._run_ps(ps, timeout)
        self._check(code, err)
        if not out.strip():
            return []
        return [dict(row) for row in csv.DictReader(io.StringIO(out))]

    def execute(self, sql: str, *, database: str = DB_SEPID, timeout: int = 300) -> str:
        """Ejecuta SQL de escritura (ExecuteNonQuery).

        El SQL debe traer su propia transaccion explicita:
            SET XACT_ABORT ON; BEGIN TRANSACTION; ... COMMIT TRANSACTION;
        No hay parametrizacion en este canal: escapar con `sql_literal`.
        """
        ps = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sql = @'
{sql}
'@
$cs = 'Server={self.sql_server};Database={database};Integrated Security=SSPI;TrustServerCertificate=True;Connection Timeout=15'
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
try {{
  $cmd = $conn.CreateCommand()
  $cmd.CommandTimeout = {max(timeout - 20, 60)}
  $cmd.CommandText = $sql
  $rows = $cmd.ExecuteNonQuery()
  Write-Output ('SQL_OK rows=' + $rows)
}} finally {{ $conn.Close() }}
""".strip()
        code, out, err = self._run_ps(ps, timeout)
        self._check(code, err)
        return out.strip()


def dump(rows: list[dict], limit: int = 60) -> None:
    """Imprime filas de forma legible en consola."""
    if not rows:
        print("   (sin filas)")
        return
    for row in rows[:limit]:
        print("   " + " | ".join(f"{k}={str(v).strip()}" for k, v in row.items()))
    if len(rows) > limit:
        print(f"   ... (+{len(rows) - limit} filas mas)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Consulta de lectura contra las bases Bejerman en SAMTRONIC.")
    parser.add_argument("--db", default=DB_SEPID, help=f"Base ({DB_SEPID} o {DB_MGBIO}). Default: {DB_SEPID}")
    parser.add_argument("--sql", default="", help="SQL a ejecutar.")
    parser.add_argument("--file", default="", help="Archivo con el SQL a ejecutar.")
    parser.add_argument("--csv", default="", help="Guarda el resultado como CSV en esta ruta.")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout en segundos. Default: 180")
    args = parser.parse_args()

    sql = args.sql or (Path(args.file).read_text(encoding="utf-8") if args.file else "")
    if not sql.strip():
        parser.error("Indicar --sql o --file")

    with Samtronic() as samtronic:
        rows = samtronic.query(sql, database=args.db, timeout=args.timeout)

    print(f"{len(rows)} filas")
    dump(rows)
    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"-> {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
