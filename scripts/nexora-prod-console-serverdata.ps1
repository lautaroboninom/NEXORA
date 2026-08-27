param(
  [switch]$ValidateOnly,
  [string]$PreviewPath = "",
  [string]$PreviewDataPath = "",
  [ValidateRange(0, 2)]
  [int]$PreviewTabIndex = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
$global:OutputEncoding = $Utf8Encoding
try {
  [Console]::InputEncoding = $Utf8Encoding
  [Console]::OutputEncoding = $Utf8Encoding
} catch {
  # La consola se abre oculta desde el acceso directo.
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$ExpectedComputerName = "SERVERDATA"
$WindowsRepo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$MonitorScript = Join-Path $WindowsRepo "scripts\get-prod-monitoring-serverdata.ps1"
$RebuildGuiScript = Join-Path $WindowsRepo "scripts\rebuild-prod-serverdata-gui.ps1"
$IconPath = Join-Path $WindowsRepo "scripts\assets\nexora-rebuild.ico"
$LogoPath = Join-Path $WindowsRepo "web\public\branding\logotipo-nexora.png"
$LogDirectory = "E:\Datos\NEXORA_Runtime\deploy_logs"
$ApplicationUrl = "https://nexora.tail7bb880.ts.net/"
$PowerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$RefreshSeconds = 30

function Show-FatalError {
  param([string]$Message)
  [void][System.Windows.Forms.MessageBox]::Show(
    $Message,
    "NEXORA - Error",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Error
  )
}

function Stop-NativeProcessTree {
  param([Parameter(Mandatory = $true)][int]$TargetProcessId)

  $taskKillPath = "$env:SystemRoot\System32\taskkill.exe"
  $taskKillProcess = $null
  try {
    if (Test-Path -LiteralPath $taskKillPath -PathType Leaf) {
      $taskKillInfo = New-Object System.Diagnostics.ProcessStartInfo
      $taskKillInfo.FileName = $taskKillPath
      $taskKillInfo.Arguments = "/PID $TargetProcessId /T /F"
      $taskKillInfo.UseShellExecute = $false
      $taskKillInfo.CreateNoWindow = $true
      $taskKillInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
      $taskKillProcess = New-Object System.Diagnostics.Process
      $taskKillProcess.StartInfo = $taskKillInfo
      if ($taskKillProcess.Start() -and -not $taskKillProcess.WaitForExit(5000)) {
        try { $taskKillProcess.Kill() } catch { }
      }
    }
  } catch {
    # El fallback siguiente garantiza que al menos termine el proceso principal.
  } finally {
    if ($null -ne $taskKillProcess) {
      $taskKillProcess.Dispose()
    }
  }

  try {
    Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue |
      Stop-Process -Force -ErrorAction SilentlyContinue
  } catch {
    # El recolector puede haber terminado mientras se recorría su árbol.
  }
}

function Remove-MonitorOutputFile {
  if (-not [string]::IsNullOrWhiteSpace($script:MonitorOutputFile) -and
      [System.IO.File]::Exists($script:MonitorOutputFile)) {
    Remove-Item -LiteralPath $script:MonitorOutputFile -Force -ErrorAction SilentlyContinue
  }
  $script:MonitorOutputFile = ""
}

function Stop-MonitorCollector {
  param([ValidateRange(0, 3600)][int]$BackoffSeconds = 0)

  if ($null -ne $script:MonitorProcess) {
    try {
      if (-not $script:MonitorProcess.HasExited) {
        Stop-NativeProcessTree -TargetProcessId $script:MonitorProcess.Id
        try { [void]$script:MonitorProcess.WaitForExit(5000) } catch { }
      }
    } catch {
      # El proceso puede no haber terminado de iniciar o haberse cerrado en paralelo.
    } finally {
      $script:MonitorProcess.Dispose()
      $script:MonitorProcess = $null
    }
  }

  Remove-MonitorOutputFile
  if ($BackoffSeconds -gt 0) {
    $script:NextMonitorRefresh = (Get-Date).AddSeconds($BackoffSeconds)
  }
}

try {
  if ([Environment]::MachineName -ne $ExpectedComputerName) {
    throw "Esta consola solo puede ejecutarse en $ExpectedComputerName. Equipo actual: $([Environment]::MachineName)."
  }
  foreach ($requiredFile in @($MonitorScript, $RebuildGuiScript, $IconPath, $LogoPath, $PowerShellPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "Falta el archivo requerido: $requiredFile"
    }
  }
} catch {
  Show-FatalError -Message $_.Exception.Message
  exit 1
}

$colorNavy = [System.Drawing.ColorTranslator]::FromHtml("#082D59")
$colorNavyLight = [System.Drawing.ColorTranslator]::FromHtml("#164B7D")
$colorBlue = [System.Drawing.ColorTranslator]::FromHtml("#2767E8")
$colorBackground = [System.Drawing.ColorTranslator]::FromHtml("#F2F5F9")
$colorText = [System.Drawing.ColorTranslator]::FromHtml("#172033")
$colorMuted = [System.Drawing.ColorTranslator]::FromHtml("#667085")
$colorBorder = [System.Drawing.ColorTranslator]::FromHtml("#D8E0EA")
$colorPending = [System.Drawing.ColorTranslator]::FromHtml("#98A2B3")
$colorWarning = [System.Drawing.ColorTranslator]::FromHtml("#F59E0B")
$colorSuccess = [System.Drawing.ColorTranslator]::FromHtml("#16A36A")
$colorError = [System.Drawing.ColorTranslator]::FromHtml("#D92D20")
$colorSoftSuccess = [System.Drawing.ColorTranslator]::FromHtml("#ECFDF3")
$colorSoftWarning = [System.Drawing.ColorTranslator]::FromHtml("#FFFAEB")
$colorSoftError = [System.Drawing.ColorTranslator]::FromHtml("#FEF3F2")
$colorGridHeader = [System.Drawing.ColorTranslator]::FromHtml("#EAF0F7")

function New-Label {
  param(
    [System.Windows.Forms.Control]$Parent,
    [int]$X,
    [int]$Y,
    [int]$Width,
    [int]$Height,
    [string]$Text,
    [float]$FontSize = 9,
    [System.Drawing.FontStyle]$FontStyle = [System.Drawing.FontStyle]::Regular,
    [System.Drawing.Color]$ForeColor = $colorText
  )

  $label = New-Object System.Windows.Forms.Label
  $label.Location = New-Object System.Drawing.Point($X, $Y)
  $label.Size = New-Object System.Drawing.Size($Width, $Height)
  $label.Font = New-Object System.Drawing.Font("Segoe UI", $FontSize, $FontStyle)
  $label.ForeColor = $ForeColor
  $label.Text = $Text
  $Parent.Controls.Add($label)
  return $label
}

function New-Button {
  param(
    [System.Windows.Forms.Control]$Parent,
    [int]$X,
    [int]$Y,
    [int]$Width,
    [int]$Height,
    [string]$Text,
    [switch]$Primary
  )

  $button = New-Object System.Windows.Forms.Button
  $button.Location = New-Object System.Drawing.Point($X, $Y)
  $button.Size = New-Object System.Drawing.Size($Width, $Height)
  $button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
  $button.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
  $button.Cursor = [System.Windows.Forms.Cursors]::Hand
  if ($Primary) {
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = $colorBlue
    $button.ForeColor = [System.Drawing.Color]::White
  } else {
    $button.FlatAppearance.BorderColor = $colorBorder
    $button.BackColor = [System.Drawing.Color]::White
    $button.ForeColor = $colorText
  }
  $button.Text = $Text
  $Parent.Controls.Add($button)
  return $button
}

function New-MetricCard {
  param(
    [System.Windows.Forms.Control]$Parent,
    [int]$X,
    [int]$Y,
    [int]$Width,
    [string]$Title,
    [string]$InitialValue,
    [string]$Caption
  )

  $panel = New-Object System.Windows.Forms.Panel
  $panel.Location = New-Object System.Drawing.Point($X, $Y)
  $panel.Size = New-Object System.Drawing.Size($Width, 88)
  $panel.BackColor = [System.Drawing.Color]::White
  $panel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
  $Parent.Controls.Add($panel)

  [void](New-Label -Parent $panel -X 14 -Y 10 -Width ($Width - 28) -Height 18 -Text $Title -FontSize 8 -ForeColor $colorMuted)
  $value = New-Label -Parent $panel -X 14 -Y 29 -Width ($Width - 28) -Height 30 -Text $InitialValue -FontSize 16 -FontStyle ([System.Drawing.FontStyle]::Bold)
  $captionLabel = New-Label -Parent $panel -X 14 -Y 63 -Width ($Width - 28) -Height 18 -Text $Caption -FontSize 8 -ForeColor $colorMuted
  return [pscustomobject]@{ Panel = $panel; Value = $value; Caption = $captionLabel }
}

function Set-MetricCard {
  param($Card, [string]$Value, [string]$Caption, [System.Drawing.Color]$Color = $colorText)
  $Card.Value.Text = $Value
  $Card.Value.ForeColor = $Color
  $Card.Caption.Text = $Caption
}

function New-UsageRow {
  param([System.Windows.Forms.Control]$Parent, [int]$Y, [string]$Title)

  [void](New-Label -Parent $Parent -X 18 -Y $Y -Width 150 -Height 20 -Text $Title -FontSize 9 -FontStyle ([System.Drawing.FontStyle]::Bold))
  $detail = New-Label -Parent $Parent -X 170 -Y $Y -Width 270 -Height 20 -Text "Esperando datos" -FontSize 8 -ForeColor $colorMuted
  $detail.TextAlign = [System.Drawing.ContentAlignment]::MiddleRight
  $track = New-Object System.Windows.Forms.Panel
  $track.Location = New-Object System.Drawing.Point(18, ($Y + 24))
  $track.Size = New-Object System.Drawing.Size(422, 8)
  $track.BackColor = $colorBorder
  $Parent.Controls.Add($track)
  $fill = New-Object System.Windows.Forms.Panel
  $fill.Location = New-Object System.Drawing.Point(0, 0)
  $fill.Size = New-Object System.Drawing.Size(0, 8)
  $fill.BackColor = $colorSuccess
  $track.Controls.Add($fill)
  return [pscustomobject]@{ Detail = $detail; Track = $track; Fill = $fill }
}

function Set-UsageRow {
  param($Row, [double]$Percent, [string]$Detail)
  $safePercent = [Math]::Max(0, [Math]::Min(100, $Percent))
  $Row.Fill.Width = [int][Math]::Round($Row.Track.Width * ($safePercent / 100))
  $Row.Fill.BackColor = if ($safePercent -ge 90) { $colorError } elseif ($safePercent -ge 75) { $colorWarning } else { $colorSuccess }
  $Row.Detail.Text = $Detail
}

function New-DataGrid {
  param([System.Windows.Forms.Control]$Parent, [int]$X, [int]$Y, [int]$Width, [int]$Height)

  $grid = New-Object System.Windows.Forms.DataGridView
  $grid.Location = New-Object System.Drawing.Point($X, $Y)
  $grid.Size = New-Object System.Drawing.Size($Width, $Height)
  $grid.AllowUserToAddRows = $false
  $grid.AllowUserToDeleteRows = $false
  $grid.AllowUserToResizeRows = $false
  $grid.ReadOnly = $true
  $grid.MultiSelect = $false
  $grid.SelectionMode = [System.Windows.Forms.DataGridViewSelectionMode]::FullRowSelect
  $grid.RowHeadersVisible = $false
  $grid.BackgroundColor = [System.Drawing.Color]::White
  $grid.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
  $grid.CellBorderStyle = [System.Windows.Forms.DataGridViewCellBorderStyle]::SingleHorizontal
  $grid.GridColor = $colorBorder
  $grid.EnableHeadersVisualStyles = $false
  $grid.ColumnHeadersDefaultCellStyle.BackColor = $colorGridHeader
  $grid.ColumnHeadersDefaultCellStyle.ForeColor = $colorText
  $grid.ColumnHeadersDefaultCellStyle.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
  $grid.ColumnHeadersHeight = 34
  $grid.DefaultCellStyle.BackColor = [System.Drawing.Color]::White
  $grid.DefaultCellStyle.ForeColor = $colorText
  $grid.DefaultCellStyle.SelectionBackColor = [System.Drawing.ColorTranslator]::FromHtml("#DCE8FC")
  $grid.DefaultCellStyle.SelectionForeColor = $colorText
  $grid.DefaultCellStyle.Font = New-Object System.Drawing.Font("Segoe UI", 8.5)
  $grid.RowTemplate.Height = 29
  $Parent.Controls.Add($grid)
  return $grid
}

function Format-Bytes {
  param([double]$Bytes)
  if ($Bytes -ge 1TB) { return "{0:N1} TB" -f ($Bytes / 1TB) }
  if ($Bytes -ge 1GB) { return "{0:N1} GB" -f ($Bytes / 1GB) }
  if ($Bytes -ge 1MB) { return "{0:N1} MB" -f ($Bytes / 1MB) }
  if ($Bytes -ge 1KB) { return "{0:N1} KB" -f ($Bytes / 1KB) }
  return "{0:N0} B" -f $Bytes
}

function Format-Duration {
  param([long]$Seconds)
  if ($Seconds -lt 60) { return "${Seconds}s" }
  if ($Seconds -lt 3600) { return "{0}m {1}s" -f [Math]::Floor($Seconds / 60), ($Seconds % 60) }
  if ($Seconds -lt 86400) { return "{0}h {1}m" -f [Math]::Floor($Seconds / 3600), [Math]::Floor(($Seconds % 3600) / 60) }
  return "{0}d {1}h" -f [Math]::Floor($Seconds / 86400), [Math]::Floor(($Seconds % 86400) / 3600)
}

function Format-ContainerUptime {
  param([string]$StartedAt)
  if ([string]::IsNullOrWhiteSpace($StartedAt) -or $StartedAt.StartsWith("0001-")) { return "—" }
  try {
    $started = [DateTimeOffset]::Parse($StartedAt)
    $elapsed = [DateTimeOffset]::Now - $started
    return Format-Duration -Seconds ([long]$elapsed.TotalSeconds)
  } catch {
    return "—"
  }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "NEXORA - Centro de control de producción"
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.Size = New-Object System.Drawing.Size(1040, 800)
$form.MinimumSize = New-Object System.Drawing.Size(820, 520)
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::Sizable
$form.MaximizeBox = $true
$form.BackColor = $colorBackground
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$form.Icon = New-Object System.Drawing.Icon($IconPath)

$tabControl = New-Object System.Windows.Forms.TabControl
$tabControl.Dock = [System.Windows.Forms.DockStyle]::Fill
$tabControl.Appearance = [System.Windows.Forms.TabAppearance]::Normal
$tabControl.DrawMode = [System.Windows.Forms.TabDrawMode]::Normal
$tabControl.ItemSize = New-Object System.Drawing.Size(155, 34)
$tabControl.SizeMode = [System.Windows.Forms.TabSizeMode]::Normal
$tabControl.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$form.Controls.Add($tabControl)

$statusStrip = New-Object System.Windows.Forms.StatusStrip
$statusStrip.Dock = [System.Windows.Forms.DockStyle]::Bottom
$statusStrip.SizingGrip = $false
$statusStrip.BackColor = [System.Drawing.Color]::White
$statusText = New-Object System.Windows.Forms.ToolStripStatusLabel
$statusText.Text = "Preparando monitoreo de solo lectura..."
$statusText.ForeColor = $colorMuted
$statusText.Spring = $true
$statusText.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$lastUpdateText = New-Object System.Windows.Forms.ToolStripStatusLabel
$lastUpdateText.Text = "Sin actualizar"
$lastUpdateText.ForeColor = $colorMuted
[void]$statusStrip.Items.Add($statusText)
[void]$statusStrip.Items.Add($lastUpdateText)
$form.Controls.Add($statusStrip)

$header = New-Object System.Windows.Forms.Panel
$header.Dock = [System.Windows.Forms.DockStyle]::Top
$header.Height = 88
$header.BackColor = $colorNavy
$form.Controls.Add($header)

$logoContainer = New-Object System.Windows.Forms.Panel
$logoContainer.Location = New-Object System.Drawing.Point(24, 15)
$logoContainer.Size = New-Object System.Drawing.Size(180, 58)
$logoContainer.BackColor = [System.Drawing.Color]::White
$header.Controls.Add($logoContainer)

$logo = New-Object System.Windows.Forms.PictureBox
$logo.Dock = [System.Windows.Forms.DockStyle]::Fill
$logo.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom
$logo.Padding = New-Object System.Windows.Forms.Padding(13)
$logo.Image = [System.Drawing.Image]::FromFile($LogoPath)
$logoContainer.Controls.Add($logo)

$title = New-Label -Parent $header -X 232 -Y 14 -Width 518 -Height 33 -Text "Control de producción" -FontSize 18 -FontStyle ([System.Drawing.FontStyle]::Bold) -ForeColor ([System.Drawing.Color]::White)
$subtitle = New-Label -Parent $header -X 235 -Y 51 -Width 528 -Height 21 -Text "SERVERDATA  •  Estado rápido  •  Actualización automática" -FontSize 9 -ForeColor ([System.Drawing.ColorTranslator]::FromHtml("#C8D7EA"))

$globalStatusPanel = New-Object System.Windows.Forms.Panel
$globalStatusPanel.Location = New-Object System.Drawing.Point(790, 17)
$globalStatusPanel.Size = New-Object System.Drawing.Size(210, 55)
$globalStatusPanel.BackColor = $colorNavyLight
$globalStatusPanel.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor [System.Windows.Forms.AnchorStyles]::Right
$header.Controls.Add($globalStatusPanel)
$globalStatusDot = New-Label -Parent $globalStatusPanel -X 15 -Y 13 -Width 20 -Height 28 -Text "●" -FontSize 13 -FontStyle ([System.Drawing.FontStyle]::Bold) -ForeColor $colorPending
$globalStatusLabel = New-Label -Parent $globalStatusPanel -X 40 -Y 9 -Width 155 -Height 22 -Text "CARGANDO" -FontSize 10 -FontStyle ([System.Drawing.FontStyle]::Bold) -ForeColor ([System.Drawing.Color]::White)
$globalStatusDetail = New-Label -Parent $globalStatusPanel -X 40 -Y 31 -Width 155 -Height 18 -Text "Esperando métricas" -FontSize 8 -ForeColor ([System.Drawing.ColorTranslator]::FromHtml("#C8D7EA"))

function New-TabPage {
  param(
    [string]$Text,
    [System.Windows.Forms.TabControl]$Parent = $tabControl,
    [switch]$Scrollable
  )
  $page = New-Object System.Windows.Forms.TabPage
  $page.Text = $Text
  $page.BackColor = $colorBackground
  $page.Padding = New-Object System.Windows.Forms.Padding(0)
  $page.AutoScroll = $Scrollable
  if ($Scrollable) {
    $page.AutoScrollMinSize = New-Object System.Drawing.Size(1004, 575)
  }
  [void]$Parent.TabPages.Add($page)
  return $page
}

$summaryTab = New-TabPage -Text "Estado"
$detailsTab = New-TabPage -Text "Detalles"
$maintenanceTab = New-TabPage -Text "Mantenimiento" -Scrollable

$detailsToolbar = New-Object System.Windows.Forms.Panel
$detailsToolbar.Location = New-Object System.Drawing.Point(0, 0)
$detailsToolbar.Size = New-Object System.Drawing.Size(1004, 48)
$detailsToolbar.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor [System.Windows.Forms.AnchorStyles]::Left -bor [System.Windows.Forms.AnchorStyles]::Right
$detailsToolbar.BackColor = [System.Drawing.Color]::White
$detailsTab.Controls.Add($detailsToolbar)
$detailsRefreshButton = New-Button -Parent $detailsToolbar -X 18 -Y 7 -Width 180 -Height 34 -Text "Actualizar detalles" -Primary
$detailsStatusLabel = New-Label -Parent $detailsToolbar -X 216 -Y 12 -Width 740 -Height 24 -Text "Los datos pesados se cargan solamente cuando los necesitás." -FontSize 8 -ForeColor $colorMuted

$detailsTabControl = New-Object System.Windows.Forms.TabControl
$detailsTabControl.Location = New-Object System.Drawing.Point(0, 48)
$detailsTabControl.Size = New-Object System.Drawing.Size(1004, 527)
$detailsTabControl.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor [System.Windows.Forms.AnchorStyles]::Bottom -bor [System.Windows.Forms.AnchorStyles]::Left -bor [System.Windows.Forms.AnchorStyles]::Right
$detailsTabControl.ItemSize = New-Object System.Drawing.Size(135, 31)
$detailsTabControl.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$detailsTab.Controls.Add($detailsTabControl)

$servicesTab = New-TabPage -Text "Servicios" -Parent $detailsTabControl -Scrollable
$trafficTab = New-TabPage -Text "Tráfico" -Parent $detailsTabControl -Scrollable
$databaseTab = New-TabPage -Text "Base de datos" -Parent $detailsTabControl -Scrollable
$logsTab = New-TabPage -Text "Eventos" -Parent $detailsTabControl -Scrollable
$serverTab = New-TabPage -Text "Servidor" -Parent $detailsTabControl -Scrollable

$summaryApiCard = New-MetricCard -Parent $summaryTab -X 20 -Y 18 -Width 232 -Title "ESTADO GENERAL" -InitialValue "Comprobando" -Caption "SERVERDATA"
$summaryLatencyCard = New-MetricCard -Parent $summaryTab -X 264 -Y 18 -Width 232 -Title "API" -InitialValue "—" -Caption "Respuesta de /api/health/"
$summaryTrafficCard = New-MetricCard -Parent $summaryTab -X 508 -Y 18 -Width 232 -Title "BASE DE DATOS" -InitialValue "—" -Caption "PostgreSQL productivo"
$summaryContainersCard = New-MetricCard -Parent $summaryTab -X 752 -Y 18 -Width 232 -Title "SERVICIOS" -InitialValue "—" -Caption "Contenedores activos"

$hostPanel = New-Object System.Windows.Forms.Panel
$hostPanel.Location = New-Object System.Drawing.Point(20, 22)
$hostPanel.Size = New-Object System.Drawing.Size(465, 190)
$hostPanel.BackColor = [System.Drawing.Color]::White
$hostPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$serverTab.Controls.Add($hostPanel)
[void](New-Label -Parent $hostPanel -X 18 -Y 12 -Width 250 -Height 25 -Text "Recursos de SERVERDATA" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold))
$hostCpuRow = New-UsageRow -Parent $hostPanel -Y 44 -Title "CPU"
$hostMemoryRow = New-UsageRow -Parent $hostPanel -Y 91 -Title "Memoria"
$hostDiskRow = New-UsageRow -Parent $hostPanel -Y 138 -Title "Disco E:"

$summaryDatabasePanel = New-Object System.Windows.Forms.Panel
$summaryDatabasePanel.Location = New-Object System.Drawing.Point(499, 22)
$summaryDatabasePanel.Size = New-Object System.Drawing.Size(485, 190)
$summaryDatabasePanel.BackColor = [System.Drawing.Color]::White
$summaryDatabasePanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$serverTab.Controls.Add($summaryDatabasePanel)
[void](New-Label -Parent $summaryDatabasePanel -X 18 -Y 12 -Width 260 -Height 25 -Text "PostgreSQL" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold))
$summaryDbSizeLabel = New-Label -Parent $summaryDatabasePanel -X 18 -Y 49 -Width 220 -Height 24 -Text "Tamaño: —" -FontSize 9
$summaryDbConnectionsLabel = New-Label -Parent $summaryDatabasePanel -X 250 -Y 49 -Width 215 -Height 24 -Text "Conexiones: —" -FontSize 9
$summaryDbActiveLabel = New-Label -Parent $summaryDatabasePanel -X 18 -Y 82 -Width 220 -Height 24 -Text "Activas: —" -FontSize 9
$summaryDbLocksLabel = New-Label -Parent $summaryDatabasePanel -X 250 -Y 82 -Width 215 -Height 24 -Text "Bloqueos: —" -FontSize 9
$summaryDbRateLabel = New-Label -Parent $summaryDatabasePanel -X 18 -Y 115 -Width 220 -Height 24 -Text "Transacciones/min: —" -FontSize 9
$summaryDbCacheLabel = New-Label -Parent $summaryDatabasePanel -X 250 -Y 115 -Width 215 -Height 24 -Text "Cache hit: —" -FontSize 9
$summaryDbStateLabel = New-Label -Parent $summaryDatabasePanel -X 18 -Y 151 -Width 447 -Height 22 -Text "Esperando métricas de solo lectura" -FontSize 8 -ForeColor $colorMuted

$summaryAlertsPanel = New-Object System.Windows.Forms.Panel
$summaryAlertsPanel.Location = New-Object System.Drawing.Point(20, 120)
$summaryAlertsPanel.Size = New-Object System.Drawing.Size(964, 255)
$summaryAlertsPanel.BackColor = [System.Drawing.Color]::White
$summaryAlertsPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$summaryTab.Controls.Add($summaryAlertsPanel)
$summaryAlertsTitle = New-Label -Parent $summaryAlertsPanel -X 18 -Y 12 -Width 400 -Height 25 -Text "Situación actual" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold)
$summaryAlertsText = New-Label -Parent $summaryAlertsPanel -X 18 -Y 44 -Width 925 -Height 145 -Text "Recolectando el estado esencial..." -FontSize 10 -ForeColor $colorMuted
$summaryAlertsText.AutoEllipsis = $true
$summaryLastRebuild = New-Label -Parent $summaryAlertsPanel -X 18 -Y 218 -Width 925 -Height 22 -Text "Último despliegue: sin datos" -FontSize 8 -ForeColor $colorMuted

$summaryOpenButton = New-Button -Parent $summaryTab -X 20 -Y 395 -Width 170 -Height 42 -Text "Abrir NEXORA" -Primary
$summaryRefreshButton = New-Button -Parent $summaryTab -X 204 -Y 395 -Width 170 -Height 42 -Text "Actualizar estado"
$summaryDetailsButton = New-Button -Parent $summaryTab -X 388 -Y 395 -Width 170 -Height 42 -Text "Ver detalles"
$summaryCopyButton = New-Button -Parent $summaryTab -X 572 -Y 395 -Width 190 -Height 42 -Text "Copiar diagnóstico"
[void](New-Label -Parent $summaryTab -X 20 -Y 451 -Width 964 -Height 36 -Text "El estado se actualiza en segundo plano. Tráfico, consumo y eventos se consultan a pedido para que esta pantalla responda rápido." -FontSize 8 -ForeColor $colorMuted)

[void](New-Label -Parent $servicesTab -X 20 -Y 15 -Width 650 -Height 25 -Text "Estado y consumo de los servicios productivos" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold))
$servicesUpdatedLabel = New-Label -Parent $servicesTab -X 700 -Y 17 -Width 284 -Height 22 -Text "Esperando datos" -FontSize 8 -ForeColor $colorMuted
$servicesUpdatedLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleRight
$servicesGrid = New-DataGrid -Parent $servicesTab -X 20 -Y 50 -Width 964 -Height 475
[void]$servicesGrid.Columns.Add("service", "Servicio")
[void]$servicesGrid.Columns.Add("state", "Estado")
[void]$servicesGrid.Columns.Add("health", "Salud")
[void]$servicesGrid.Columns.Add("cpu", "CPU")
[void]$servicesGrid.Columns.Add("memory", "Memoria")
[void]$servicesGrid.Columns.Add("restarts", "Reinicios")
[void]$servicesGrid.Columns.Add("uptime", "En ejecución")
$servicesGrid.Columns[0].Width = 220
$servicesGrid.Columns[1].Width = 105
$servicesGrid.Columns[2].Width = 105
$servicesGrid.Columns[3].Width = 75
$servicesGrid.Columns[4].Width = 190
$servicesGrid.Columns[5].Width = 85
$servicesGrid.Columns[6].AutoSizeMode = [System.Windows.Forms.DataGridViewAutoSizeColumnMode]::Fill
[void](New-Label -Parent $servicesTab -X 20 -Y 535 -Width 900 -Height 20 -Text "El tráfico de red mostrado por Docker es acumulado desde el inicio del contenedor." -FontSize 8 -ForeColor $colorMuted)

$trafficRateCard = New-MetricCard -Parent $trafficTab -X 20 -Y 18 -Width 232 -Title "SOLICITUDES/MIN" -InitialValue "—" -Caption "Sin health checks"
$traffic2xxCard = New-MetricCard -Parent $trafficTab -X 264 -Y 18 -Width 232 -Title "RESPUESTAS 2XX" -InitialValue "—" -Caption "Últimos 10 minutos"
$traffic4xxCard = New-MetricCard -Parent $trafficTab -X 508 -Y 18 -Width 232 -Title "RESPUESTAS 4XX" -InitialValue "—" -Caption "Últimos 10 minutos"
$traffic5xxCard = New-MetricCard -Parent $trafficTab -X 752 -Y 18 -Width 232 -Title "RESPUESTAS 5XX" -InitialValue "—" -Caption "Últimos 10 minutos"
[void](New-Label -Parent $trafficTab -X 20 -Y 121 -Width 460 -Height 24 -Text "Rutas más utilizadas" -FontSize 10 -FontStyle ([System.Drawing.FontStyle]::Bold))
[void](New-Label -Parent $trafficTab -X 506 -Y 121 -Width 478 -Height 24 -Text "Respuestas HTTP recientes con error" -FontSize 10 -FontStyle ([System.Drawing.FontStyle]::Bold))
$topPathsGrid = New-DataGrid -Parent $trafficTab -X 20 -Y 151 -Width 465 -Height 355
[void]$topPathsGrid.Columns.Add("path", "Ruta normalizada")
[void]$topPathsGrid.Columns.Add("count", "Solicitudes")
$topPathsGrid.Columns[0].AutoSizeMode = [System.Windows.Forms.DataGridViewAutoSizeColumnMode]::Fill
$topPathsGrid.Columns[1].Width = 95
$httpErrorsGrid = New-DataGrid -Parent $trafficTab -X 506 -Y 151 -Width 478 -Height 355
[void]$httpErrorsGrid.Columns.Add("status", "Código")
[void]$httpErrorsGrid.Columns.Add("method", "Método")
[void]$httpErrorsGrid.Columns.Add("path", "Ruta normalizada")
$httpErrorsGrid.Columns[0].Width = 70
$httpErrorsGrid.Columns[1].Width = 70
$httpErrorsGrid.Columns[2].AutoSizeMode = [System.Windows.Forms.DataGridViewAutoSizeColumnMode]::Fill
$trafficNoteLabel = New-Label -Parent $trafficTab -X 20 -Y 519 -Width 964 -Height 37 -Text "Nginx no registra duración por solicitud. La latencia mostrada corresponde a una sonda HTTPS real contra la API." -FontSize 8 -ForeColor $colorMuted

$dbSizeCard = New-MetricCard -Parent $databaseTab -X 20 -Y 18 -Width 232 -Title "TAMAÑO" -InitialValue "—" -Caption "Base productiva"
$dbConnectionsCard = New-MetricCard -Parent $databaseTab -X 264 -Y 18 -Width 232 -Title "CONEXIONES" -InitialValue "—" -Caption "Uso del máximo configurado"
$dbActiveCard = New-MetricCard -Parent $databaseTab -X 508 -Y 18 -Width 232 -Title "ACTIVAS" -InitialValue "—" -Caption "Sin contar el monitor"
$dbLocksCard = New-MetricCard -Parent $databaseTab -X 752 -Y 18 -Width 232 -Title "BLOQUEOS" -InitialValue "—" -Caption "Sesiones esperando"

$dbDetailsPanel = New-Object System.Windows.Forms.Panel
$dbDetailsPanel.Location = New-Object System.Drawing.Point(20, 122)
$dbDetailsPanel.Size = New-Object System.Drawing.Size(964, 390)
$dbDetailsPanel.BackColor = [System.Drawing.Color]::White
$dbDetailsPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$databaseTab.Controls.Add($dbDetailsPanel)
[void](New-Label -Parent $dbDetailsPanel -X 18 -Y 14 -Width 350 -Height 25 -Text "Actividad actual de PostgreSQL" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold))
$dbConnectionUsage = New-UsageRow -Parent $dbDetailsPanel -Y 49 -Title "Conexiones"
$dbIdleLabel = New-Label -Parent $dbDetailsPanel -X 18 -Y 102 -Width 440 -Height 25 -Text "Inactivas: —" -FontSize 9
$dbIdleTransactionLabel = New-Label -Parent $dbDetailsPanel -X 495 -Y 102 -Width 440 -Height 25 -Text "Idle en transacción: —" -FontSize 9
$dbLongestQueryLabel = New-Label -Parent $dbDetailsPanel -X 18 -Y 139 -Width 440 -Height 25 -Text "Consulta activa más larga: —" -FontSize 9
$dbOldestTransactionLabel = New-Label -Parent $dbDetailsPanel -X 495 -Y 139 -Width 440 -Height 25 -Text "Transacción más antigua: —" -FontSize 9
$dbCacheDetailLabel = New-Label -Parent $dbDetailsPanel -X 18 -Y 176 -Width 440 -Height 25 -Text "Cache hit: —" -FontSize 9
$dbRateDetailLabel = New-Label -Parent $dbDetailsPanel -X 495 -Y 176 -Width 440 -Height 25 -Text "Transacciones/min: calculando" -FontSize 9
$dbRollbackLabel = New-Label -Parent $dbDetailsPanel -X 18 -Y 213 -Width 440 -Height 25 -Text "Rollbacks acumulados: —" -FontSize 9
$dbDeadlocksLabel = New-Label -Parent $dbDetailsPanel -X 495 -Y 213 -Width 440 -Height 25 -Text "Deadlocks acumulados: —" -FontSize 9
$dbTempLabel = New-Label -Parent $dbDetailsPanel -X 18 -Y 250 -Width 440 -Height 25 -Text "Temporales escritos: —" -FontSize 9
$dbUptimeLabel = New-Label -Parent $dbDetailsPanel -X 495 -Y 250 -Width 440 -Height 25 -Text "Uptime PostgreSQL: —" -FontSize 9
$dbWarningPanel = New-Object System.Windows.Forms.Panel
$dbWarningPanel.Location = New-Object System.Drawing.Point(18, 300)
$dbWarningPanel.Size = New-Object System.Drawing.Size(925, 62)
$dbWarningPanel.BackColor = $colorSoftSuccess
$dbDetailsPanel.Controls.Add($dbWarningPanel)
$dbWarningLabel = New-Label -Parent $dbWarningPanel -X 14 -Y 11 -Width 895 -Height 42 -Text "Esperando la consulta de monitoreo de solo lectura..." -FontSize 9 -ForeColor $colorMuted
[void](New-Label -Parent $databaseTab -X 20 -Y 526 -Width 964 -Height 32 -Text "No se muestra el texto de las consultas ni se ejecutan sentencias de escritura. pg_stat_statements no está instalado." -FontSize 8 -ForeColor $colorMuted)

[void](New-Label -Parent $logsTab -X 20 -Y 15 -Width 650 -Height 25 -Text "Eventos relevantes de los últimos 15 minutos" -FontSize 11 -FontStyle ([System.Drawing.FontStyle]::Bold))
$eventsInfoLabel = New-Label -Parent $logsTab -X 20 -Y 42 -Width 964 -Height 35 -Text "Se filtran errores, excepciones y fallas. Correos, credenciales e identificadores sensibles se ocultan." -FontSize 8 -ForeColor $colorMuted
$eventsGrid = New-DataGrid -Parent $logsTab -X 20 -Y 82 -Width 964 -Height 400
[void]$eventsGrid.Columns.Add("container", "Servicio")
[void]$eventsGrid.Columns.Add("message", "Evento sanitizado")
$eventsGrid.Columns[0].Width = 220
$eventsGrid.Columns[1].AutoSizeMode = [System.Windows.Forms.DataGridViewAutoSizeColumnMode]::Fill
$eventsRefreshButton = New-Button -Parent $logsTab -X 20 -Y 500 -Width 170 -Height 40 -Text "Actualizar ahora" -Primary
$eventsOpenLogsButton = New-Button -Parent $logsTab -X 204 -Y 500 -Width 180 -Height 40 -Text "Abrir deploy_logs"
$eventsCopyButton = New-Button -Parent $logsTab -X 398 -Y 500 -Width 190 -Height 40 -Text "Copiar diagnóstico"

$rebuildPanel = New-Object System.Windows.Forms.Panel
$rebuildPanel.Location = New-Object System.Drawing.Point(20, 260)
$rebuildPanel.Size = New-Object System.Drawing.Size(964, 225)
$rebuildPanel.BackColor = [System.Drawing.Color]::White
$rebuildPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$maintenanceTab.Controls.Add($rebuildPanel)
[void](New-Label -Parent $rebuildPanel -X 24 -Y 18 -Width 700 -Height 34 -Text "Desplegar código actual (avanzado)" -FontSize 15 -FontStyle ([System.Drawing.FontStyle]::Bold))
[void](New-Label -Parent $rebuildPanel -X 24 -Y 58 -Width 900 -Height 67 -Text "Construye las imágenes, inicia PostgreSQL, aplica migraciones y esquemas de forma serializada y luego levanta los servicios. Verifica que NEXORA permanezca activa al terminar. Requiere confirmación y no corre tests en producción." -FontSize 9 -ForeColor $colorWarning)
$openRebuildButton = New-Button -Parent $rebuildPanel -X 24 -Y 148 -Width 270 -Height 48 -Text "Abrir asistente de despliegue"
$rebuildStateLabel = New-Label -Parent $rebuildPanel -X 316 -Y 157 -Width 610 -Height 30 -Text "El estado seguirá visible en esta ventana." -FontSize 9 -ForeColor $colorMuted

$toolsPanel = New-Object System.Windows.Forms.Panel
$toolsPanel.Location = New-Object System.Drawing.Point(20, 22)
$toolsPanel.Size = New-Object System.Drawing.Size(964, 215)
$toolsPanel.BackColor = [System.Drawing.Color]::White
$toolsPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$maintenanceTab.Controls.Add($toolsPanel)
[void](New-Label -Parent $toolsPanel -X 24 -Y 18 -Width 500 -Height 30 -Text "Acciones cotidianas" -FontSize 13 -FontStyle ([System.Drawing.FontStyle]::Bold))
[void](New-Label -Parent $toolsPanel -X 24 -Y 54 -Width 900 -Height 42 -Text "Estas acciones son de solo lectura: abren el sistema, actualizan el estado o permiten compartir el diagnóstico." -FontSize 9 -ForeColor $colorMuted)
$maintenanceOpenButton = New-Button -Parent $toolsPanel -X 24 -Y 116 -Width 190 -Height 44 -Text "Abrir NEXORA"
$maintenanceRefreshButton = New-Button -Parent $toolsPanel -X 230 -Y 116 -Width 190 -Height 44 -Text "Actualizar estado"
$maintenanceCopyButton = New-Button -Parent $toolsPanel -X 436 -Y 116 -Width 200 -Height 44 -Text "Copiar diagnóstico"
$maintenanceLogsButton = New-Button -Parent $toolsPanel -X 652 -Y 116 -Width 190 -Height 44 -Text "Abrir registros"
$maintenanceCloseButton = New-Button -Parent $maintenanceTab -X 814 -Y 505 -Width 170 -Height 42 -Text "Cerrar"

$script:MonitorProcess = $null
$script:MonitorOutputFile = ""
$script:MonitorStartedAt = [DateTime]::MinValue
$script:NextMonitorRefresh = [DateTime]::MinValue
$script:LastMonitorData = $null
$script:LastFullMonitorData = $null
$script:LastDetailsAt = [DateTimeOffset]::MinValue
$script:MonitorMode = "Quick"
$script:PendingMonitorMode = ""
$script:PreviousTransactionCount = $null
$script:PreviousTransactionTimestamp = $null
$script:TransactionRate = $null
$script:LatencyHistory = New-Object System.Collections.ArrayList
$script:IsClosing = $false

$serviceNames = @{
  "nexora-web" = "Frontend / Nginx"
  "nexora-api" = "API / Gunicorn"
  "nexora-postgres" = "PostgreSQL"
  "nexora-bejerman-sync" = "Sincronización Bejerman"
  "nexora-bejerman-articulos-sync" = "Catálogo de artículos Bejerman"
  "nexora-bejerman-pdf" = "PDF Bejerman"
  "nexora-email-outbox" = "Cola de correos"
  "nexora-billing-postponements" = "Postergaciones de facturación"
  "nexora-billing-summary" = "Resumen de facturación"
}

function Set-GlobalStatus {
  param([string]$Title, [string]$Detail, [System.Drawing.Color]$Color)
  $globalStatusLabel.Text = $Title
  $globalStatusDetail.Text = $Detail
  $globalStatusDot.ForeColor = $Color
}

function Add-DiagnosticWarning {
  param([System.Collections.ArrayList]$List, [string]$Message)
  if (-not [string]::IsNullOrWhiteSpace($Message) -and $Message -notin $List) {
    [void]$List.Add($Message)
  }
}

function Get-MonitorProperty {
  param(
    [AllowNull()]$Object,
    [Parameter(Mandatory = $true)][string]$Name,
    $DefaultValue = $null
  )
  if ($null -eq $Object) {
    return $DefaultValue
  }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return $DefaultValue
  }
  return $property.Value
}

function Apply-MonitorData {
  param($Data)

  $script:LastMonitorData = $Data
  $detailsAvailable = [bool](Get-MonitorProperty -Object $Data -Name "detailsAvailable" -DefaultValue $true)
  $monitorMode = [string](Get-MonitorProperty -Object $Data -Name "mode" -DefaultValue "Full")
  $trafficAvailable = [bool](Get-MonitorProperty -Object $Data.traffic -Name "available" -DefaultValue $detailsAvailable)
  $recentErrorsAvailable = [bool](Get-MonitorProperty -Object $Data -Name "recentErrorsAvailable" -DefaultValue $detailsAvailable)
  $collectedAt = [DateTimeOffset]::Parse([string]$Data.collectedAt)
  $runningContainers = @($Data.containers | Where-Object { $_.state -eq "running" }).Count
  $totalContainers = @($Data.containers).Count
  if ($totalContainers -eq 0) {
    $totalContainers = $serviceNames.Count
  }
  $runtime = Get-MonitorProperty -Object $Data -Name "runtime" -DefaultValue $null
  $runtimeTaskState = [string](Get-MonitorProperty -Object $runtime -Name "keepaliveTaskState" -DefaultValue "Desconocido")
  $heartbeatAvailable = [bool](Get-MonitorProperty -Object $runtime -Name "heartbeatAvailable" -DefaultValue $false)
  $heartbeatAge = [int](Get-MonitorProperty -Object $runtime -Name "heartbeatAgeSeconds" -DefaultValue -1)
  $wslMemoryPercent = [int](Get-MonitorProperty -Object $runtime -Name "wslMemoryPercent" -DefaultValue 0)
  $wslUptimeSeconds = [long](Get-MonitorProperty -Object $runtime -Name "wslUptimeSeconds" -DefaultValue 0)
  $runtimeHealthy = (
    $runtimeTaskState -eq "Running" -and
    $heartbeatAvailable -and
    $heartbeatAge -ge 0 -and
    $heartbeatAge -le 90
  )
  $heartbeatCaption = if ($heartbeatAge -ge 0) { "$heartbeatAge s" } else { "sin datos" }
  $runtimeCaption = if ($runtimeHealthy) {
    "WSL activo $(Format-Duration -Seconds $wslUptimeSeconds)  •  RAM $wslMemoryPercent%"
  } else {
    "Keepalive $runtimeTaskState  •  heartbeat $heartbeatCaption"
  }

  if ($detailsAvailable) {
    $script:LastFullMonitorData = $Data
    $script:LastDetailsAt = $collectedAt
    $detailsStatusLabel.Text = "Detalles actualizados: $($collectedAt.LocalDateTime.ToString('dd/MM/yyyy HH:mm:ss'))"
    $detailsStatusLabel.ForeColor = $colorSuccess
  } elseif ($null -eq $script:LastFullMonitorData) {
    $detailsStatusLabel.Text = 'Estado rápido disponible. Pulsá "Actualizar detalles" para cargar consumo, tráfico y eventos.'
    $detailsStatusLabel.ForeColor = $colorMuted
  }

  if ($Data.health.available) {
    Set-MetricCard -Card $summaryLatencyCard -Value "Disponible" -Caption ("HTTPS {0}  •  {1} ms" -f $Data.health.httpCode, $Data.health.latencyMs) -Color $(if ([int]$Data.health.latencyMs -ge 1000) { $colorWarning } else { $colorSuccess })
    [void]$script:LatencyHistory.Add([int]$Data.health.latencyMs)
    while ($script:LatencyHistory.Count -gt 30) { $script:LatencyHistory.RemoveAt(0) }
  } else {
    Set-MetricCard -Card $summaryLatencyCard -Value "No disponible" -Caption $(if ([int]$Data.health.httpCode -gt 0) { "HTTP $($Data.health.httpCode)" } else { "Sin respuesta válida" }) -Color $colorError
  }
  Set-MetricCard -Card $summaryContainersCard -Value ("{0}/{1}" -f $runningContainers, $totalContainers) -Caption "Contenedores en ejecución" -Color $(if ($runningContainers -eq $totalContainers) { $colorSuccess } else { $colorError })

  Set-UsageRow -Row $hostCpuRow -Percent ([double]$Data.host.cpuPercent) -Detail ("{0}%" -f $Data.host.cpuPercent)
  Set-UsageRow -Row $hostMemoryRow -Percent ([double]$Data.host.memoryPercent) -Detail ("{0}%  •  {1}" -f $Data.host.memoryPercent, $Data.host.memoryUsed)
  Set-UsageRow -Row $hostDiskRow -Percent ([double]$Data.host.diskPercent) -Detail ("{0}% usado  •  {1}" -f $Data.host.diskPercent, $Data.host.diskFree)

  if ($Data.database.available) {
    Set-MetricCard -Card $summaryTrafficCard -Value "Disponible" -Caption "Consulta protegida de solo lectura" -Color $colorSuccess
    $transactionCount = [long]$Data.database.transactions + [long]$Data.database.rollbacks
    if ($null -ne $script:PreviousTransactionCount -and $null -ne $script:PreviousTransactionTimestamp) {
      $elapsedMinutes = ($collectedAt - $script:PreviousTransactionTimestamp).TotalMinutes
      $difference = $transactionCount - [long]$script:PreviousTransactionCount
      if ($elapsedMinutes -gt 0 -and $difference -ge 0) {
        $script:TransactionRate = [Math]::Round($difference / $elapsedMinutes, 1)
      }
    }
    $script:PreviousTransactionCount = $transactionCount
    $script:PreviousTransactionTimestamp = $collectedAt

    $connectionCaption = if ([int]$Data.database.maxConnections -gt 0) { "de $($Data.database.maxConnections)" } else { "actuales" }
    Set-MetricCard -Card $dbSizeCard -Value (Format-Bytes -Bytes ([double]$Data.database.sizeBytes)) -Caption "Base productiva" -Color $colorBlue
    Set-MetricCard -Card $dbConnectionsCard -Value ([string]$Data.database.connections) -Caption $connectionCaption -Color $colorText
    Set-MetricCard -Card $dbActiveCard -Value ([string]$Data.database.active) -Caption "Consultas activas" -Color $(if ([int]$Data.database.longestActiveSeconds -gt 30) { $colorWarning } else { $colorSuccess })
    $lockTotal = [int]$Data.database.lockWaiters + [int]$Data.database.blockedSessions
    Set-MetricCard -Card $dbLocksCard -Value ([string]$lockTotal) -Caption "Esperando o bloqueadas" -Color $(if ($lockTotal -gt 0) { $colorError } else { $colorSuccess })

    $summaryDbSizeLabel.Text = "Tamaño: $(Format-Bytes -Bytes ([double]$Data.database.sizeBytes))"
    $summaryDbConnectionsLabel.Text = "Conexiones: $($Data.database.connections)/$($Data.database.maxConnections)"
    $summaryDbActiveLabel.Text = "Activas: $($Data.database.active)"
    $summaryDbLocksLabel.Text = "Bloqueos: $lockTotal"
    $summaryDbRateLabel.Text = if ($null -eq $script:TransactionRate) { "Transacciones/min: calculando" } else { "Transacciones/min: $($script:TransactionRate)" }
    $summaryDbCacheLabel.Text = "Cache hit: $($Data.database.cacheHitPercent)%"
    $summaryDbStateLabel.Text = "Consulta protegida por modo de solo lectura y timeout de 3 segundos."
    $summaryDbStateLabel.ForeColor = $colorSuccess

    $connectionPercent = if ([int]$Data.database.maxConnections -gt 0) { ([double]$Data.database.connections / [double]$Data.database.maxConnections) * 100 } else { 0 }
    Set-UsageRow -Row $dbConnectionUsage -Percent $connectionPercent -Detail ("{0} de {1} ({2:N0}%)" -f $Data.database.connections, $Data.database.maxConnections, $connectionPercent)
    $dbIdleLabel.Text = "Inactivas: $($Data.database.idle)"
    $dbIdleTransactionLabel.Text = "Idle en transacción: $($Data.database.idleInTransaction)"
    $dbLongestQueryLabel.Text = "Consulta activa más larga: $(Format-Duration -Seconds ([long]$Data.database.longestActiveSeconds))"
    $dbOldestTransactionLabel.Text = "Transacción más antigua: $(Format-Duration -Seconds ([long]$Data.database.oldestTransactionSeconds))"
    $dbCacheDetailLabel.Text = "Cache hit: $($Data.database.cacheHitPercent)%"
    $dbRateDetailLabel.Text = if ($null -eq $script:TransactionRate) { "Transacciones/min: calculando" } else { "Transacciones/min: $($script:TransactionRate)" }
    $dbRollbackLabel.Text = "Rollbacks acumulados: $($Data.database.rollbacks)"
    $dbDeadlocksLabel.Text = "Deadlocks acumulados: $($Data.database.deadlocks)"
    $dbTempLabel.Text = "Temporales escritos: $(Format-Bytes -Bytes ([double]$Data.database.temporaryBytes))"
    $dbUptimeLabel.Text = "Uptime PostgreSQL: $(Format-Duration -Seconds ([long]$Data.database.uptimeSeconds))"

    if ($lockTotal -gt 0) {
      $dbWarningPanel.BackColor = $colorSoftError
      $dbWarningLabel.ForeColor = $colorError
      $dbWarningLabel.Text = "Hay sesiones esperando o bloqueadas. Conviene revisar la actividad antes de realizar mantenimiento."
    } elseif ([int]$Data.database.idleInTransaction -gt 0) {
      $dbWarningPanel.BackColor = $colorSoftWarning
      $dbWarningLabel.ForeColor = $colorWarning
      $dbWarningLabel.Text = "Hay $($Data.database.idleInTransaction) conexión(es) idle en transacción. Se muestra como advertencia para seguimiento."
    } else {
      $dbWarningPanel.BackColor = $colorSoftSuccess
      $dbWarningLabel.ForeColor = $colorSuccess
      $dbWarningLabel.Text = "Sin bloqueos ni conexiones idle en transacción detectadas."
    }
  } else {
    Set-MetricCard -Card $summaryTrafficCard -Value "No disponible" -Caption "PostgreSQL sin métricas" -Color $colorError
    foreach ($card in @($dbSizeCard, $dbConnectionsCard, $dbActiveCard, $dbLocksCard)) {
      Set-MetricCard -Card $card -Value "No disponible" -Caption "Sin métricas" -Color $colorError
    }
    $summaryDbStateLabel.Text = "No fue posible consultar PostgreSQL."
    $summaryDbStateLabel.ForeColor = $colorError
    $dbWarningPanel.BackColor = $colorSoftError
    $dbWarningLabel.ForeColor = $colorError
    $dbWarningLabel.Text = "Las métricas de PostgreSQL no están disponibles en esta actualización."
  }

  $servicesGrid.Rows.Clear()
  foreach ($container in $Data.containers) {
    $friendlyName = if ($serviceNames.ContainsKey([string]$container.name)) { $serviceNames[[string]$container.name] } else { [string]$container.name }
    $stateText = if ($container.state -eq "running") { "Activo" } elseif ($container.state -eq "missing") { "Ausente" } else { [string]$container.state }
    $healthText = if ($container.name -eq "nexora-api") {
      if ($Data.health.available) { "HTTP OK" } else { "Sin respuesta" }
    } elseif ($container.health -eq "healthy") {
      "Healthy"
    } elseif ($container.state -eq "running") {
      "En ejecución"
    } else {
      "—"
    }
    $rowIndex = $servicesGrid.Rows.Add($friendlyName, $stateText, $healthText, $container.cpu, $container.memory, $container.restarts, (Format-ContainerUptime -StartedAt ([string]$container.startedAt)))
    $row = $servicesGrid.Rows[$rowIndex]
    if ($container.state -ne "running" -or ($container.name -eq "nexora-api" -and -not $Data.health.available)) {
      $row.DefaultCellStyle.BackColor = $colorSoftError
      $row.Cells[1].Style.ForeColor = $colorError
      $row.Cells[2].Style.ForeColor = $colorError
    } else {
      $row.Cells[1].Style.ForeColor = $colorSuccess
      $row.Cells[2].Style.ForeColor = $colorSuccess
    }
    if ([int]$container.restarts -gt 0) {
      $row.Cells[5].Style.ForeColor = $colorWarning
      $row.Cells[5].Style.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 8.5)
    }
  }
  $servicesGrid.ClearSelection()
  $servicesUpdatedLabel.Text = "Actualizado: $($collectedAt.LocalDateTime.ToString('dd/MM/yyyy HH:mm:ss'))"

  if ($detailsAvailable -and $trafficAvailable) {
    Set-MetricCard -Card $trafficRateCard -Value ([string]$Data.traffic.requestsPerMinute) -Caption "Solicitudes por minuto" -Color $colorBlue
    Set-MetricCard -Card $traffic2xxCard -Value ([string]$Data.traffic.status2xx) -Caption "Respuestas correctas" -Color $colorSuccess
    Set-MetricCard -Card $traffic4xxCard -Value ([string]$Data.traffic.status4xx) -Caption "Errores de cliente" -Color $(if ([int]$Data.traffic.status4xx -gt 0) { $colorWarning } else { $colorSuccess })
    Set-MetricCard -Card $traffic5xxCard -Value ([string]$Data.traffic.status5xx) -Caption "Errores del servidor" -Color $(if ([int]$Data.traffic.status5xx -gt 0) { $colorError } else { $colorSuccess })

    $topPathsGrid.Rows.Clear()
    foreach ($pathItem in $Data.traffic.topPaths) {
      [void]$topPathsGrid.Rows.Add($pathItem.path, $pathItem.count)
    }
    $topPathsGrid.ClearSelection()

    $httpErrorsGrid.Rows.Clear()
    foreach ($httpError in $Data.traffic.recentHttpErrors) {
      $rowIndex = $httpErrorsGrid.Rows.Add($httpError.status, $httpError.method, $httpError.path)
      $httpErrorsGrid.Rows[$rowIndex].Cells[0].Style.ForeColor = if ([int]$httpError.status -ge 500) { $colorError } else { $colorWarning }
    }
    if ($httpErrorsGrid.Rows.Count -eq 0) {
      [void]$httpErrorsGrid.Rows.Add("—", "—", "Sin respuestas HTTP con error en la ventana actual")
    }
    $httpErrorsGrid.ClearSelection()
    $trafficNoteLabel.Text = "Ventana: $($Data.traffic.windowMinutes) min  •  API: $($Data.traffic.apiRequests)  •  Health checks excluidos: $($Data.traffic.healthChecks)" + $(if ($Data.traffic.sampleLimited) { "  •  Muestra limitada a 2500 líneas" } else { "  •  Latencia por ruta no disponible en el log actual" })
  } elseif ($null -eq $script:LastFullMonitorData) {
    foreach ($card in @($trafficRateCard, $traffic2xxCard, $traffic4xxCard, $traffic5xxCard)) {
      Set-MetricCard -Card $card -Value "A pedido" -Caption "Actualizar detalles" -Color $colorMuted
    }
    $topPathsGrid.Rows.Clear()
    [void]$topPathsGrid.Rows.Add('Pulsá "Actualizar detalles" para consultar el tráfico.', "—")
    $httpErrorsGrid.Rows.Clear()
    [void]$httpErrorsGrid.Rows.Add("—", "—", "Los errores HTTP se cargan a pedido.")
    $trafficNoteLabel.Text = "Esta consulta se mantiene fuera de la actualización automática para que el estado principal responda rápido."
  }

  if ($detailsAvailable -and $recentErrorsAvailable) {
    $eventsGrid.Rows.Clear()
    foreach ($eventItem in $Data.recentErrors) {
      $friendlyName = if ($serviceNames.ContainsKey([string]$eventItem.container)) { $serviceNames[[string]$eventItem.container] } else { [string]$eventItem.container }
      [void]$eventsGrid.Rows.Add($friendlyName, $eventItem.message)
    }
    if ($eventsGrid.Rows.Count -eq 0) {
      [void]$eventsGrid.Rows.Add("Todos los servicios", "Sin errores relevantes en los últimos 15 minutos.")
    }
    $eventsGrid.ClearSelection()
  } elseif ($null -eq $script:LastFullMonitorData) {
    $eventsGrid.Rows.Clear()
    [void]$eventsGrid.Rows.Add("Diagnóstico", 'Pulsá "Actualizar detalles" para consultar los eventos recientes.')
    $eventsGrid.ClearSelection()
  }

  $warnings = New-Object System.Collections.ArrayList
  $restartTotal = 0
  foreach ($container in @($Data.containers)) {
    $restartTotal += [int]$container.restarts
  }
  if (-not $runtimeHealthy) { Add-DiagnosticWarning -List $warnings -Message "El runtime permanente de WSL no está confirmado: tarea $runtimeTaskState, heartbeat $heartbeatCaption." }
  if ($wslMemoryPercent -ge 85) { Add-DiagnosticWarning -List $warnings -Message "WSL utiliza $wslMemoryPercent% de su memoria asignada." }
  if ($restartTotal -gt 0) { Add-DiagnosticWarning -List $warnings -Message "Los contenedores acumulan $restartTotal reinicio(s) desde el último despliegue." }
  if (-not $Data.health.available) { Add-DiagnosticWarning -List $warnings -Message "La API no respondió correctamente a la sonda HTTPS." }
  if ($runningContainers -ne $totalContainers) { Add-DiagnosticWarning -List $warnings -Message "Hay $($totalContainers - $runningContainers) contenedor(es) fuera de ejecución." }
  if (-not $Data.database.available) { Add-DiagnosticWarning -List $warnings -Message "PostgreSQL no respondió a la consulta protegida de estado." }
  if ($trafficAvailable -and [int]$Data.traffic.status5xx -gt 0) { Add-DiagnosticWarning -List $warnings -Message "Se detectaron $($Data.traffic.status5xx) respuestas HTTP 5xx en los últimos $($Data.traffic.windowMinutes) minutos." }
  if ($Data.database.available -and ([int]$Data.database.lockWaiters + [int]$Data.database.blockedSessions) -gt 0) { Add-DiagnosticWarning -List $warnings -Message "PostgreSQL informa sesiones esperando o bloqueadas." }
  if ($Data.database.available -and [int]$Data.database.idleInTransaction -gt 0) { Add-DiagnosticWarning -List $warnings -Message "PostgreSQL tiene $($Data.database.idleInTransaction) conexión(es) idle en transacción." }
  if ($recentErrorsAvailable -and @($Data.recentErrors).Count -gt 0) { Add-DiagnosticWarning -List $warnings -Message "Hay $(@($Data.recentErrors).Count) evento(s) relevante(s) sanitizado(s) para revisar." }
  foreach ($collectorWarning in @($Data.warnings)) { Add-DiagnosticWarning -List $warnings -Message ([string]$collectorWarning) }

  if ($warnings.Count -eq 0) {
    $summaryAlertsPanel.BackColor = $colorSoftSuccess
    $summaryAlertsTitle.ForeColor = $colorSuccess
    $summaryAlertsTitle.Text = "Todo funciona correctamente"
    $summaryAlertsText.ForeColor = $colorText
    $summaryAlertsText.Text = "API disponible, $runningContainers servicios activos, PostgreSQL accesible y runtime permanente vigente.`r`n`r`nWSL lleva $(Format-Duration -Seconds $wslUptimeSeconds) activo y utiliza $wslMemoryPercent% de la memoria asignada."
    Set-GlobalStatus -Title "OPERATIVO" -Detail ("{0}/{1} servicios" -f $runningContainers, $totalContainers) -Color $colorSuccess
    Set-MetricCard -Card $summaryApiCard -Value "Operativo" -Caption $runtimeCaption -Color $colorSuccess
  } else {
    $critical = (-not $runtimeHealthy) -or (-not $Data.health.available) -or ($runningContainers -ne $totalContainers) -or (-not $Data.database.available) -or ($wslMemoryPercent -ge 95) -or ($Data.database.available -and ([int]$Data.database.lockWaiters + [int]$Data.database.blockedSessions) -gt 0)
    $summaryAlertsPanel.BackColor = if ($critical) { $colorSoftError } else { $colorSoftWarning }
    $summaryAlertsTitle.ForeColor = if ($critical) { $colorError } else { $colorWarning }
    $summaryAlertsTitle.Text = if ($critical) { "Requiere atención" } else { "Operativo con advertencias" }
    $summaryAlertsText.ForeColor = $colorText
    $summaryAlertsText.Text = (($warnings | Select-Object -First 4 | ForEach-Object { "• $_" }) -join "`r`n")
    Set-GlobalStatus -Title $(if ($critical) { "ATENCIÓN" } else { "ADVERTENCIAS" }) -Detail ("{0} aviso(s)" -f $warnings.Count) -Color $(if ($critical) { $colorError } else { $colorWarning })
    Set-MetricCard -Card $summaryApiCard -Value $(if ($critical) { "Requiere atención" } else { "Con advertencias" }) -Caption $runtimeCaption -Color $(if ($critical) { $colorError } else { $colorWarning })
  }

  if ($Data.lastRebuild.available) {
    $rebuildTime = [DateTimeOffset]::Parse([string]$Data.lastRebuild.timestamp).LocalDateTime
    $summaryLastRebuild.Text = "Último despliegue: $($rebuildTime.ToString('dd/MM/yyyy HH:mm'))  •  $($Data.lastRebuild.result)  •  $($Data.lastRebuild.fileName)"
  } else {
    $summaryLastRebuild.Text = "Último despliegue: no hay un registro previo disponible."
  }

  $statusText.Text = if ($monitorMode -eq "Full") { "Detalles completos cargados. El estado automático seguirá siendo rápido y de solo lectura." } else { "Estado rápido de solo lectura activo  •  Próxima lectura en $RefreshSeconds segundos" }
  $lastUpdateText.Text = "Actualizado $($collectedAt.LocalDateTime.ToString('HH:mm:ss'))"
  foreach ($button in @($summaryRefreshButton, $detailsRefreshButton, $eventsRefreshButton, $maintenanceRefreshButton)) { $button.Enabled = $true }
}

function Show-MonitorError {
  param([string]$Message)
  if ($null -eq $script:LastMonitorData) {
    Set-GlobalStatus -Title "SIN DATOS" -Detail "Falló la primera lectura" -Color $colorError
  }
  $statusText.Text = "No se pudo actualizar: $Message"
  $lastUpdateText.Text = if ($null -eq $script:LastMonitorData) { "Reintento automático pendiente" } else { "Se conserva la última lectura válida" }
  $script:NextMonitorRefresh = (Get-Date).AddSeconds([Math]::Max(60, $RefreshSeconds))
  foreach ($button in @($summaryRefreshButton, $detailsRefreshButton, $eventsRefreshButton, $maintenanceRefreshButton)) { $button.Enabled = $true }
}

function Start-MonitorRefresh {
  param([ValidateSet("Quick", "Full")][string]$Mode = "Quick")

  if ($script:IsClosing -or ($null -ne $script:MonitorProcess -and -not $script:MonitorProcess.HasExited)) {
    if ($Mode -eq "Full") {
      $script:PendingMonitorMode = "Full"
      $detailsStatusLabel.Text = "La carga de detalles comenzará al terminar la lectura actual."
      $detailsStatusLabel.ForeColor = $colorMuted
    }
    return
  }

  $script:MonitorOutputFile = Join-Path $env:TEMP ("nexora-monitor-gui-{0}-{1}.json" -f $PID, (Get-Date -Format "yyyyMMddHHmmssfff"))
  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$MonitorScript`" -OutputFile `"$script:MonitorOutputFile`" -Mode $Mode"
  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $PowerShellPath
  $startInfo.Arguments = $arguments
  $startInfo.WorkingDirectory = $WindowsRepo
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

  try {
    $script:MonitorProcess = New-Object System.Diagnostics.Process
    $script:MonitorProcess.StartInfo = $startInfo
    if (-not $script:MonitorProcess.Start()) {
      throw "No se pudo iniciar el recolector."
    }
    $script:MonitorStartedAt = Get-Date
    $script:MonitorMode = $Mode
    if ($null -eq $script:LastMonitorData) {
      Set-GlobalStatus -Title "ACTUALIZANDO" -Detail "Leyendo SERVERDATA" -Color $colorBlue
    }
    if ($Mode -eq "Full") {
      $statusText.Text = "Cargando consumo, tráfico y eventos en segundo plano..."
      $detailsStatusLabel.Text = "Cargando detalles; el último estado válido sigue visible."
      $detailsStatusLabel.ForeColor = $colorBlue
    } else {
      $statusText.Text = "Actualizando el estado esencial en segundo plano..."
    }
    foreach ($button in @($summaryRefreshButton, $detailsRefreshButton, $eventsRefreshButton, $maintenanceRefreshButton)) { $button.Enabled = $false }
  } catch {
    $monitorStartError = $_.Exception.Message
    Stop-MonitorCollector
    Show-MonitorError -Message $monitorStartError
  }
}

function Complete-MonitorRefresh {
  $completedSuccessfully = $false
  $pendingMode = ""
  try {
    if (-not (Test-Path -LiteralPath $script:MonitorOutputFile -PathType Leaf)) {
      throw "El recolector no generó una respuesta."
    }
    $json = [System.IO.File]::ReadAllText($script:MonitorOutputFile, [System.Text.Encoding]::UTF8)
    $data = $json | ConvertFrom-Json
    if (-not $data.success) {
      throw [string]$data.error
    }
    Apply-MonitorData -Data $data
    $completedSuccessfully = $true
  } catch {
    Show-MonitorError -Message $_.Exception.Message
  } finally {
    if ($null -ne $script:MonitorProcess) {
      $script:MonitorProcess.Dispose()
      $script:MonitorProcess = $null
    }
    Remove-MonitorOutputFile
    $script:NextMonitorRefresh = (Get-Date).AddSeconds($(if ($completedSuccessfully) { $RefreshSeconds } else { [Math]::Max(60, $RefreshSeconds) }))
    $pendingMode = $script:PendingMonitorMode
    $script:PendingMonitorMode = ""
  }
  if (-not [string]::IsNullOrWhiteSpace($pendingMode)) {
    Start-MonitorRefresh -Mode $pendingMode
  }
}

function Request-ImmediateRefresh {
  param([ValidateSet("Quick", "Full")][string]$Mode = "Quick")
  $script:NextMonitorRefresh = [DateTime]::MinValue
  Start-MonitorRefresh -Mode $Mode
}

function Open-Nexora {
  try {
    Start-Process -FilePath $ApplicationUrl
  } catch {
    Show-FatalError -Message $_.Exception.Message
  }
}

function Open-DeployLogs {
  try {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Start-Process -FilePath "explorer.exe" -ArgumentList "`"$LogDirectory`""
  } catch {
    Show-FatalError -Message $_.Exception.Message
  }
}

function Copy-DiagnosticSummary {
  if ($null -eq $script:LastMonitorData) {
    [void][System.Windows.Forms.MessageBox]::Show(
      "Todavía no hay una actualización completa para copiar.",
      "NEXORA - Diagnóstico",
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Information
    )
    return
  }

  $data = $script:LastMonitorData
  $detailData = if ($null -ne $script:LastFullMonitorData) { $script:LastFullMonitorData } else { $data }
  $detailTrafficAvailable = [bool](Get-MonitorProperty -Object $detailData.traffic -Name "available" -DefaultValue $true)
  $detailEventsAvailable = [bool](Get-MonitorProperty -Object $detailData -Name "recentErrorsAvailable" -DefaultValue $true)
  $lines = New-Object System.Collections.ArrayList
  [void]$lines.Add("NEXORA PRODUCCIÓN - Diagnóstico resumido")
  [void]$lines.Add("SERVERDATA | $($data.collectedAt)")
  [void]$lines.Add("API: disponible=$($data.health.available), HTTP=$($data.health.httpCode), latencia=$($data.health.latencyMs) ms")
  $runtime = Get-MonitorProperty -Object $data -Name "runtime" -DefaultValue $null
  [void]$lines.Add(
    "Runtime WSL: keepalive=$(Get-MonitorProperty -Object $runtime -Name 'keepaliveTaskState' -DefaultValue 'Desconocido'), " +
    "heartbeat=$(Get-MonitorProperty -Object $runtime -Name 'heartbeatAgeSeconds' -DefaultValue -1) s, " +
    "RAM=$(Get-MonitorProperty -Object $runtime -Name 'wslMemoryPercent' -DefaultValue 0)%, " +
    "uptime=$(Format-Duration -Seconds ([long](Get-MonitorProperty -Object $runtime -Name 'wslUptimeSeconds' -DefaultValue 0)))"
  )
  if ($detailTrafficAvailable) {
    [void]$lines.Add("Tráfico $($detailData.traffic.windowMinutes) min: req=$($detailData.traffic.requests), 2xx=$($detailData.traffic.status2xx), 4xx=$($detailData.traffic.status4xx), 5xx=$($detailData.traffic.status5xx)")
  } else {
    [void]$lines.Add("Tráfico: no cargado; requiere una actualización de detalles.")
  }
  [void]$lines.Add("Servidor: CPU=$($data.host.cpuPercent)%, RAM=$($data.host.memoryPercent)%, disco E=$($data.host.diskPercent)%")
  if ($data.database.available) {
    [void]$lines.Add("PostgreSQL: tamaño=$(Format-Bytes -Bytes ([double]$data.database.sizeBytes)), conexiones=$($data.database.connections)/$($data.database.maxConnections), activas=$($data.database.active), locks=$([int]$data.database.lockWaiters + [int]$data.database.blockedSessions)")
  } else {
    [void]$lines.Add("PostgreSQL: métricas no disponibles")
  }
  [void]$lines.Add("")
  [void]$lines.Add("Servicios:")
  foreach ($container in $data.containers) {
    [void]$lines.Add("- $($container.name): $($container.state), CPU=$($container.cpu), memoria=$($container.memory), reinicios=$($container.restarts)")
  }
  [void]$lines.Add("")
  if ($detailEventsAvailable) {
    [void]$lines.Add("Eventos sanitizados detectados: $(@($detailData.recentErrors).Count)")
  } else {
    [void]$lines.Add("Eventos: no cargados; requieren una actualización de detalles.")
  }
  [System.Windows.Forms.Clipboard]::SetText(($lines -join "`r`n"))
  $statusText.Text = "Diagnóstico resumido copiado al portapapeles."
}

function Open-RebuildAssistant {
  try {
    $anchor = "{0},{1},{2},{3}" -f $form.Left, $form.Top, $form.Width, $form.Height
    $arguments = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RebuildGuiScript`" -AnchorBounds `"$anchor`""
    Start-Process -FilePath $PowerShellPath -ArgumentList $arguments -WorkingDirectory $WindowsRepo -WindowStyle Hidden
    $rebuildStateLabel.Text = "Asistente abierto. Esta consola seguirá mostrando el estado de producción."
    $rebuildStateLabel.ForeColor = $colorSuccess
  } catch {
    $rebuildStateLabel.Text = $_.Exception.Message
    $rebuildStateLabel.ForeColor = $colorError
  }
}

$summaryRefreshButton.Add_Click({ Request-ImmediateRefresh -Mode "Quick" })
$maintenanceRefreshButton.Add_Click({ Request-ImmediateRefresh -Mode "Quick" })
$detailsRefreshButton.Add_Click({ Request-ImmediateRefresh -Mode "Full" })
$eventsRefreshButton.Add_Click({ Request-ImmediateRefresh -Mode "Full" })
$summaryDetailsButton.Add_Click({ $tabControl.SelectedTab = $detailsTab })
foreach ($button in @($summaryOpenButton, $maintenanceOpenButton)) {
  $button.Add_Click({ Open-Nexora })
}
foreach ($button in @($summaryCopyButton, $eventsCopyButton, $maintenanceCopyButton)) {
  $button.Add_Click({ Copy-DiagnosticSummary })
}
foreach ($button in @($eventsOpenLogsButton, $maintenanceLogsButton)) {
  $button.Add_Click({ Open-DeployLogs })
}
$openRebuildButton.Add_Click({ Open-RebuildAssistant })
$maintenanceCloseButton.Add_Click({ $form.Close() })
$tabControl.Add_SelectedIndexChanged({
  if (-not $ValidateOnly -and
      $tabControl.SelectedTab -eq $detailsTab -and
      ($script:LastDetailsAt -eq [DateTimeOffset]::MinValue -or ([DateTimeOffset]::Now - $script:LastDetailsAt).TotalMinutes -ge 2)) {
    Request-ImmediateRefresh -Mode "Full"
  }
})

$monitorTimer = New-Object System.Windows.Forms.Timer
$monitorTimer.Interval = 500
$monitorTimer.Add_Tick({
  if ($null -ne $script:MonitorProcess) {
    if ($script:MonitorProcess.HasExited) {
      Complete-MonitorRefresh
    } elseif (((Get-Date) - $script:MonitorStartedAt).TotalSeconds -gt $(if ($script:MonitorMode -eq "Full") { 45 } else { 20 })) {
      $timeoutSeconds = if ($script:MonitorMode -eq "Full") { 45 } else { 20 }
      Stop-MonitorCollector -BackoffSeconds ([Math]::Max(60, $RefreshSeconds))
      $script:PendingMonitorMode = ""
      Show-MonitorError -Message "La actualización $($script:MonitorMode.ToLowerInvariant()) excedió $timeoutSeconds segundos."
    }
  } elseif ((Get-Date) -ge $script:NextMonitorRefresh) {
    Start-MonitorRefresh -Mode "Quick"
  }
})

function Update-ResponsiveHeader {
  $availableWidth = $header.ClientSize.Width
  if ($availableWidth -lt 980) {
    $globalStatusPanel.Visible = $false
    $title.Width = [Math]::Max(300, $availableWidth - 285)
    $subtitle.Width = [Math]::Max(300, $availableWidth - 288)
  } else {
    $globalStatusPanel.Visible = $true
    $globalStatusPanel.Location = New-Object System.Drawing.Point(($availableWidth - 234), 17)
    $title.Width = [Math]::Max(420, $availableWidth - 550)
    $subtitle.Width = [Math]::Max(420, $availableWidth - 550)
  }
}

function Fit-FormToWorkingArea {
  $workingArea = [System.Windows.Forms.Screen]::FromControl($form).WorkingArea
  $availableWidth = [Math]::Max(640, $workingArea.Width - 16)
  $availableHeight = [Math]::Max(460, $workingArea.Height - 16)
  $minimumWidth = [Math]::Min(820, $availableWidth)
  $minimumHeight = [Math]::Min(520, $availableHeight)
  $targetWidth = [Math]::Min(1040, $availableWidth)
  $targetHeight = [Math]::Min(800, $availableHeight)

  $form.MinimumSize = New-Object System.Drawing.Size($minimumWidth, $minimumHeight)
  $form.Size = New-Object System.Drawing.Size($targetWidth, $targetHeight)
  $form.Location = New-Object System.Drawing.Point(
    ($workingArea.Left + [Math]::Max(0, [Math]::Floor(($workingArea.Width - $form.Width) / 2))),
    ($workingArea.Top + [Math]::Max(0, [Math]::Floor(($workingArea.Height - $form.Height) / 2)))
  )
  Update-ResponsiveHeader
}

$form.Add_Load({ Fit-FormToWorkingArea })
$form.Add_Resize({ Update-ResponsiveHeader })

$form.Add_Shown({
  if (-not $ValidateOnly) {
    $script:NextMonitorRefresh = [DateTime]::MinValue
    $monitorTimer.Start()
  }
})

$form.Add_FormClosed({
  $script:IsClosing = $true
  $monitorTimer.Stop()
  Stop-MonitorCollector
  if ($null -ne $logo.Image) { $logo.Image.Dispose() }
  if ($null -ne $form.Icon) { $form.Icon.Dispose() }
})

if (-not [string]::IsNullOrWhiteSpace($PreviewDataPath) -and (Test-Path -LiteralPath $PreviewDataPath -PathType Leaf)) {
  $previewJson = [System.IO.File]::ReadAllText($PreviewDataPath, [System.Text.Encoding]::UTF8)
  Apply-MonitorData -Data ($previewJson | ConvertFrom-Json)
}

if ($ValidateOnly) {
  $tabControl.SelectedIndex = $PreviewTabIndex
  if (-not [string]::IsNullOrWhiteSpace($PreviewPath)) {
    $previewDirectory = Split-Path -Parent $PreviewPath
    if (-not [string]::IsNullOrWhiteSpace($previewDirectory)) {
      New-Item -ItemType Directory -Path $previewDirectory -Force | Out-Null
    }
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $form.PerformLayout()
    $preview = New-Object System.Drawing.Bitmap($form.ClientSize.Width, $form.ClientSize.Height)
    try {
      $form.DrawToBitmap($preview, (New-Object System.Drawing.Rectangle(0, 0, $preview.Width, $preview.Height)))
      $preview.Save($PreviewPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
      $preview.Dispose()
      $form.Hide()
    }
  }
  Write-Host "VALIDACIÓN CONSOLA OK: recursos, controles y navegación cargados correctamente."
  $form.Dispose()
  exit 0
}

[System.Windows.Forms.Application]::Run($form)
