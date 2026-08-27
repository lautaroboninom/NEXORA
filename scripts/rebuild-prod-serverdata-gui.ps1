param(
  [switch]$ValidateOnly,
  [string]$PreviewPath = "",
  [string]$AnchorBounds = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
$global:OutputEncoding = $Utf8Encoding
try {
  [Console]::InputEncoding = $Utf8Encoding
  [Console]::OutputEncoding = $Utf8Encoding
} catch {
  # Mantener compatibilidad con hosts que no exponen una consola estándar.
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$ExpectedComputerName = "SERVERDATA"
$WindowsRepo = "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA"
$BackendScript = Join-Path $WindowsRepo "scripts\rebuild-prod-serverdata.ps1"
$IconPath = Join-Path $WindowsRepo "scripts\assets\nexora-rebuild.ico"
$LogoPath = Join-Path $WindowsRepo "web\public\branding\logotipo-nexora.png"
$LogDirectory = "E:\Datos\NEXORA_Runtime\deploy_logs"
$PowerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

function Show-FatalError {
  param([string]$Message)
  [void][System.Windows.Forms.MessageBox]::Show(
    $Message,
    "NEXORA - Error",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Error
  )
}

try {
  if ([Environment]::MachineName -ne $ExpectedComputerName) {
    throw "Esta interfaz solo puede ejecutarse en $ExpectedComputerName. Equipo actual: $([Environment]::MachineName)."
  }

  foreach ($requiredFile in @($BackendScript, $IconPath, $LogoPath, $PowerShellPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "Falta el archivo requerido: $requiredFile"
    }
  }
} catch {
  Show-FatalError -Message $_.Exception.Message
  exit 1
}

$colorNavy = [System.Drawing.ColorTranslator]::FromHtml("#082D59")
$colorBlue = [System.Drawing.ColorTranslator]::FromHtml("#2767E8")
$colorBackground = [System.Drawing.ColorTranslator]::FromHtml("#F2F5F9")
$colorText = [System.Drawing.ColorTranslator]::FromHtml("#172033")
$colorMuted = [System.Drawing.ColorTranslator]::FromHtml("#667085")
$colorBorder = [System.Drawing.ColorTranslator]::FromHtml("#D8E0EA")
$colorPending = [System.Drawing.ColorTranslator]::FromHtml("#98A2B3")
$colorWorking = [System.Drawing.ColorTranslator]::FromHtml("#F59E0B")
$colorSuccess = [System.Drawing.ColorTranslator]::FromHtml("#16A36A")
$colorError = [System.Drawing.ColorTranslator]::FromHtml("#D92D20")
$colorLog = [System.Drawing.ColorTranslator]::FromHtml("#101828")

function ConvertTo-AnchorRectangle {
  param([string]$Anchor)

  if ([string]::IsNullOrWhiteSpace($Anchor)) {
    return $null
  }
  $parts = $Anchor -split ","
  if ($parts.Count -ne 4) {
    return $null
  }
  $numbers = New-Object System.Collections.Generic.List[int]
  foreach ($part in $parts) {
    $value = 0
    if (-not [int]::TryParse($part.Trim(), [ref]$value)) {
      return $null
    }
    [void]$numbers.Add($value)
  }
  if ($numbers[2] -le 0 -or $numbers[3] -le 0) {
    return $null
  }
  return New-Object System.Drawing.Rectangle($numbers[0], $numbers[1], $numbers[2], $numbers[3])
}

function Get-InitialWindowLocation {
  param(
    [System.Drawing.Size]$WindowSize,
    [string]$Anchor
  )

  # La ventana del rebuild no debe caer encima de quien la abrió (centro de control
  # o consola de PowerShell): se ubica al costado y, si no entra, contra el borde
  # más lejano de la pantalla.
  $anchorBounds = ConvertTo-AnchorRectangle -Anchor $Anchor
  if ($null -ne $anchorBounds) {
    $area = [System.Windows.Forms.Screen]::FromRectangle($anchorBounds).WorkingArea
  } else {
    $area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
  }

  $gap = 16
  if ($null -ne $anchorBounds) {
    $x = $anchorBounds.Right + $gap
    $y = $anchorBounds.Top
    if (($x + $WindowSize.Width) -gt $area.Right) {
      $x = $anchorBounds.Left - $gap - $WindowSize.Width
      if ($x -lt $area.Left) {
        # No entra a ningún costado: se va al borde más lejano del ancla para
        # tapar lo menos posible.
        $anchorCenter = $anchorBounds.Left + [int]($anchorBounds.Width / 2)
        $areaCenter = $area.Left + [int]($area.Width / 2)
        if ($anchorCenter -le $areaCenter) {
          $x = $area.Right - $WindowSize.Width
        } else {
          $x = $area.Left
        }
        $y = $area.Top + 24
      }
    }
  } else {
    $x = $area.Right - $WindowSize.Width - 32
    $y = $area.Top + [int](($area.Height - $WindowSize.Height) / 2)
  }

  $maxX = [Math]::Max($area.Left, ($area.Right - $WindowSize.Width))
  $maxY = [Math]::Max($area.Top, ($area.Bottom - $WindowSize.Height))
  $x = [Math]::Min([Math]::Max([int]$x, $area.Left), $maxX)
  $y = [Math]::Min([Math]::Max([int]$y, $area.Top), $maxY)
  return (New-Object System.Drawing.Point($x, $y))
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "NEXORA - Rebuild de producción"
$form.Size = New-Object System.Drawing.Size(900, 760)
$form.MinimumSize = New-Object System.Drawing.Size(900, 760)
$form.MaximumSize = New-Object System.Drawing.Size(900, 760)
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.Location = Get-InitialWindowLocation -WindowSize $form.Size -Anchor $AnchorBounds
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedSingle
$form.MaximizeBox = $false
$form.BackColor = $colorBackground
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$form.Icon = New-Object System.Drawing.Icon($IconPath)

$header = New-Object System.Windows.Forms.Panel
$header.Dock = [System.Windows.Forms.DockStyle]::Top
$header.Height = 118
$header.BackColor = $colorNavy

$logoContainer = New-Object System.Windows.Forms.Panel
$logoContainer.Location = New-Object System.Drawing.Point(28, 20)
$logoContainer.Size = New-Object System.Drawing.Size(205, 76)
$logoContainer.BackColor = [System.Drawing.Color]::White
$header.Controls.Add($logoContainer)

$logo = New-Object System.Windows.Forms.PictureBox
$logo.Dock = [System.Windows.Forms.DockStyle]::Fill
$logo.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom
$logo.Padding = New-Object System.Windows.Forms.Padding(14)
$logo.Image = [System.Drawing.Image]::FromFile($LogoPath)
$logoContainer.Controls.Add($logo)

$title = New-Object System.Windows.Forms.Label
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(266, 27)
$title.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 20)
$title.ForeColor = [System.Drawing.Color]::White
$title.Text = "Rebuild de producción"
$header.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.AutoSize = $true
$subtitle.Location = New-Object System.Drawing.Point(269, 68)
$subtitle.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$subtitle.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#C8D7EA")
$subtitle.Text = "SERVERDATA  •  Docker Compose  •  Entorno productivo"
$header.Controls.Add($subtitle)

$content = New-Object System.Windows.Forms.Panel
$content.Dock = [System.Windows.Forms.DockStyle]::Fill
$content.Padding = New-Object System.Windows.Forms.Padding(28, 20, 28, 18)
$content.BackColor = $colorBackground
$form.Controls.Add($content)
$form.Controls.Add($header)

$notice = New-Object System.Windows.Forms.Label
$notice.Location = New-Object System.Drawing.Point(28, 18)
$notice.Size = New-Object System.Drawing.Size(824, 38)
$notice.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$notice.ForeColor = $colorMuted
$notice.Text = "Reconstruye los archivos actuales del repositorio. No ejecuta git pull, no borra volúmenes y no corre tests en producción."
$content.Controls.Add($notice)

$cardDefinitions = @(
  @{ Key = "preflight"; Title = "Entorno"; Caption = "WSL y Docker" },
  @{ Key = "build"; Title = "Build"; Caption = "Imágenes y código" },
  @{ Key = "services"; Title = "Servicios"; Caption = "Frontend y workers" },
  @{ Key = "health"; Title = "API"; Caption = "Health check" }
)
$script:Cards = @{}

for ($index = 0; $index -lt $cardDefinitions.Count; $index++) {
  $definition = $cardDefinitions[$index]
  $card = New-Object System.Windows.Forms.Panel
  $card.Location = New-Object System.Drawing.Point((28 + ($index * 207)), 62)
  $card.Size = New-Object System.Drawing.Size(196, 82)
  $card.BackColor = [System.Drawing.Color]::White
  $card.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle

  $dot = New-Object System.Windows.Forms.Label
  $dot.Location = New-Object System.Drawing.Point(14, 17)
  $dot.Size = New-Object System.Drawing.Size(14, 18)
  $dot.Font = New-Object System.Drawing.Font("Segoe UI Symbol", 11, [System.Drawing.FontStyle]::Bold)
  $dot.ForeColor = $colorPending
  $dot.Text = "●"
  $card.Controls.Add($dot)

  $cardTitle = New-Object System.Windows.Forms.Label
  $cardTitle.AutoSize = $true
  $cardTitle.Location = New-Object System.Drawing.Point(35, 14)
  $cardTitle.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
  $cardTitle.ForeColor = $colorText
  $cardTitle.Text = $definition.Title
  $card.Controls.Add($cardTitle)

  $cardCaption = New-Object System.Windows.Forms.Label
  $cardCaption.AutoSize = $true
  $cardCaption.Location = New-Object System.Drawing.Point(35, 35)
  $cardCaption.Font = New-Object System.Drawing.Font("Segoe UI", 8)
  $cardCaption.ForeColor = $colorMuted
  $cardCaption.Text = $definition.Caption
  $card.Controls.Add($cardCaption)

  $cardStatus = New-Object System.Windows.Forms.Label
  $cardStatus.AutoSize = $true
  $cardStatus.Location = New-Object System.Drawing.Point(35, 57)
  $cardStatus.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 8)
  $cardStatus.ForeColor = $colorPending
  $cardStatus.Text = "Pendiente"
  $card.Controls.Add($cardStatus)

  $content.Controls.Add($card)
  $script:Cards[$definition.Key] = @{ Dot = $dot; Status = $cardStatus }
}

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Location = New-Object System.Drawing.Point(28, 162)
$statusLabel.Size = New-Object System.Drawing.Size(650, 26)
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 11)
$statusLabel.ForeColor = $colorText
$statusLabel.Text = "Listo para iniciar"
$content.Controls.Add($statusLabel)

$elapsedLabel = New-Object System.Windows.Forms.Label
$elapsedLabel.Location = New-Object System.Drawing.Point(708, 164)
$elapsedLabel.Size = New-Object System.Drawing.Size(144, 22)
$elapsedLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleRight
$elapsedLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$elapsedLabel.ForeColor = $colorMuted
$elapsedLabel.Text = "00:00"
$content.Controls.Add($elapsedLabel)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(28, 194)
$progress.Size = New-Object System.Drawing.Size(824, 12)
$progress.Minimum = 0
$progress.Maximum = 100
$progress.Value = 0
$progress.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
$content.Controls.Add($progress)

$activityTitle = New-Object System.Windows.Forms.Label
$activityTitle.AutoSize = $true
$activityTitle.Location = New-Object System.Drawing.Point(28, 224)
$activityTitle.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
$activityTitle.ForeColor = $colorText
$activityTitle.Text = "Actividad"
$content.Controls.Add($activityTitle)

$activityLog = New-Object System.Windows.Forms.RichTextBox
$activityLog.Location = New-Object System.Drawing.Point(28, 251)
$activityLog.Size = New-Object System.Drawing.Size(824, 220)
$activityLog.ReadOnly = $true
$activityLog.BorderStyle = [System.Windows.Forms.BorderStyle]::None
$activityLog.BackColor = $colorLog
$activityLog.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#D0D5DD")
$activityLog.Font = New-Object System.Drawing.Font("Consolas", 9)
$activityLog.DetectUrls = $false
$activityLog.Text = "  Esperando confirmación para comenzar...`n"
$content.Controls.Add($activityLog)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Location = New-Object System.Drawing.Point(28, 493)
$startButton.Size = New-Object System.Drawing.Size(210, 44)
$startButton.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$startButton.FlatAppearance.BorderSize = 0
$startButton.BackColor = $colorBlue
$startButton.ForeColor = [System.Drawing.Color]::White
$startButton.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
$startButton.Text = "Iniciar rebuild"
$startButton.Cursor = [System.Windows.Forms.Cursors]::Hand
$content.Controls.Add($startButton)

$logsButton = New-Object System.Windows.Forms.Button
$logsButton.Location = New-Object System.Drawing.Point(252, 493)
$logsButton.Size = New-Object System.Drawing.Size(170, 44)
$logsButton.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$logsButton.FlatAppearance.BorderColor = $colorBorder
$logsButton.BackColor = [System.Drawing.Color]::White
$logsButton.ForeColor = $colorText
$logsButton.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$logsButton.Text = "Abrir registros"
$logsButton.Cursor = [System.Windows.Forms.Cursors]::Hand
$content.Controls.Add($logsButton)

$closeButton = New-Object System.Windows.Forms.Button
$closeButton.Location = New-Object System.Drawing.Point(682, 493)
$closeButton.Size = New-Object System.Drawing.Size(170, 44)
$closeButton.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$closeButton.FlatAppearance.BorderColor = $colorBorder
$closeButton.BackColor = [System.Drawing.Color]::White
$closeButton.ForeColor = $colorText
$closeButton.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$closeButton.Text = "Cerrar"
$closeButton.Cursor = [System.Windows.Forms.Cursors]::Hand
$content.Controls.Add($closeButton)

$footer = New-Object System.Windows.Forms.Label
$footer.Location = New-Object System.Drawing.Point(28, 548)
$footer.Size = New-Object System.Drawing.Size(824, 24)
$footer.Font = New-Object System.Drawing.Font("Segoe UI", 8)
$footer.ForeColor = $colorMuted
$footer.Text = "Puede cerrar la ventana en cualquier momento; si hay una operación activa, se solicitará confirmación para cancelarla."
$content.Controls.Add($footer)

$script:BackendProcess = $null
$script:StatusFile = ""
$script:ProcessedStatusLines = 0
$script:IsRunning = $false
$script:RunFinalized = $true
$script:RunState = "idle"
$script:Stopwatch = New-Object System.Diagnostics.Stopwatch
$script:LastStage = "preflight"
$script:FailureObserved = $false
$script:ErrorObservedAt = $null
$script:TerminalReceived = $false
$script:TerminalObservedAt = $null
$script:TerminalResult = ""
$script:TerminalMessage = ""
$script:CancelRequested = $false
$script:AllowClose = $false
$script:LastTerminationAttemptAt = $null
$script:TerminationAttempts = 0
$script:ErrorGraceSeconds = 12
$script:TerminalGraceSeconds = 5

function Set-CardState {
  param(
    [string]$Key,
    [ValidateSet("pending", "working", "success", "error")]
    [string]$State
  )

  $card = $script:Cards[$Key]
  switch ($State) {
    "working" { $card.Dot.ForeColor = $colorWorking; $card.Status.ForeColor = $colorWorking; $card.Status.Text = "En proceso" }
    "success" { $card.Dot.ForeColor = $colorSuccess; $card.Status.ForeColor = $colorSuccess; $card.Status.Text = "Completado" }
    "error" { $card.Dot.ForeColor = $colorError; $card.Status.ForeColor = $colorError; $card.Status.Text = "Error" }
    default { $card.Dot.ForeColor = $colorPending; $card.Status.ForeColor = $colorPending; $card.Status.Text = "Pendiente" }
  }
}

function Append-Activity {
  param([string]$Message, [string]$Level = "info")

  $activityLog.SelectionStart = $activityLog.TextLength
  $activityLog.SelectionLength = 0
  switch ($Level) {
    "success" { $activityLog.SelectionColor = [System.Drawing.ColorTranslator]::FromHtml("#6CE9A6") }
    "error" { $activityLog.SelectionColor = [System.Drawing.ColorTranslator]::FromHtml("#FDA29B") }
    "working" { $activityLog.SelectionColor = [System.Drawing.ColorTranslator]::FromHtml("#FEC84B") }
    default { $activityLog.SelectionColor = [System.Drawing.ColorTranslator]::FromHtml("#D0D5DD") }
  }
  $activityLog.AppendText(("  [{0}] {1}`n" -f (Get-Date -Format "HH:mm:ss"), $Message))
  $activityLog.SelectionColor = $activityLog.ForeColor
  $activityLog.ScrollToCaret()
}

function Reset-Interface {
  foreach ($key in "preflight", "build", "services", "health") {
    Set-CardState -Key $key -State "pending"
  }
  $progress.Value = 0
  $statusLabel.ForeColor = $colorText
  $statusLabel.Text = "Preparando..."
  $elapsedLabel.Text = "00:00"
  $activityLog.Clear()
}

function Test-BackendProcessRunning {
  if ($null -eq $script:BackendProcess) {
    return $false
  }

  try {
    return -not $script:BackendProcess.HasExited
  } catch {
    return $false
  }
}

function Get-BackendProcessTreeIds {
  param([int]$RootProcessId)

  $processIds = @($RootProcessId)
  try {
    $processRows = @(Get-WmiObject -Class Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId)
    do {
      $foundChild = $false
      foreach ($processRow in $processRows) {
        $processId = [int]$processRow.ProcessId
        $parentProcessId = [int]$processRow.ParentProcessId
        if ($parentProcessId -in $processIds -and $processId -notin $processIds) {
          $processIds += $processId
          $foundChild = $true
        }
      }
    } while ($foundChild)
  } catch {
    # taskkill /T es la vía principal; esta lista se usa solamente como respaldo.
  }

  return @($processIds)
}

function Stop-BackendProcessTree {
  param([string]$Reason = "Cancelación solicitada por el usuario.")

  if (-not (Test-BackendProcessRunning)) {
    return $true
  }

  $rootProcessId = 0
  try {
    $rootProcessId = [int]$script:BackendProcess.Id
  } catch {
    return $true
  }

  if ($rootProcessId -le 0 -or $rootProcessId -eq $PID) {
    return $false
  }

  $taskkillPath = Join-Path $env:SystemRoot "System32\taskkill.exe"
  try {
    $taskkillInfo = New-Object System.Diagnostics.ProcessStartInfo
    $taskkillInfo.FileName = $taskkillPath
    $taskkillInfo.Arguments = "/PID $rootProcessId /T /F"
    $taskkillInfo.UseShellExecute = $false
    $taskkillInfo.CreateNoWindow = $true
    $taskkillInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

    $taskkillProcess = New-Object System.Diagnostics.Process
    try {
      $taskkillProcess.StartInfo = $taskkillInfo
      if ($taskkillProcess.Start()) {
        if (-not $taskkillProcess.WaitForExit(5000)) {
          try { $taskkillProcess.Kill() } catch { }
        }
      }
    } finally {
      $taskkillProcess.Dispose()
    }
  } catch {
    # Si taskkill no está disponible, se aplica el respaldo por PID capturado.
  }

  if (Test-BackendProcessRunning) {
    $processTreeIds = @(Get-BackendProcessTreeIds -RootProcessId $rootProcessId)
    for ($index = $processTreeIds.Count - 1; $index -ge 0; $index--) {
      $processId = [int]$processTreeIds[$index]
      if ($processId -le 0 -or $processId -eq $PID) {
        continue
      }
      try {
        $process = [System.Diagnostics.Process]::GetProcessById($processId)
        try {
          if (-not $process.HasExited) {
            $process.Kill()
          }
        } finally {
          $process.Dispose()
        }
      } catch {
        # El proceso puede haber finalizado entre la enumeración y el respaldo.
      }
    }
  }

  try {
    [void]$script:BackendProcess.WaitForExit(2000)
  } catch {
    # HasExited confirmará el resultado sin bloquear la interfaz.
  }

  return -not (Test-BackendProcessRunning)
}

function Dispose-BackendProcess {
  param([switch]$Force)

  if ($null -eq $script:BackendProcess) {
    return $true
  }
  if (-not $Force -and (Test-BackendProcessRunning)) {
    return $false
  }

  try {
    $script:BackendProcess.Dispose()
  } catch {
    # La limpieza de la interfaz no debe quedar bloqueada por un handle inválido.
  } finally {
    $script:BackendProcess = $null
  }
  return $true
}

function Remove-StatusFile {
  if (Test-BackendProcessRunning) {
    return
  }
  if (-not [string]::IsNullOrWhiteSpace($script:StatusFile) -and [System.IO.File]::Exists($script:StatusFile)) {
    try {
      [System.IO.File]::Delete($script:StatusFile)
    } catch {
      # Es un archivo temporal; se podrá limpiar en la próxima ejecución.
    }
  }
  $script:StatusFile = ""
  $script:ProcessedStatusLines = 0
}

function Get-StatusEventValue {
  param(
    $Event,
    [string]$Name,
    $DefaultValue = $null
  )

  if ($null -eq $Event) {
    return $DefaultValue
  }
  $property = $Event.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value) {
    return $DefaultValue
  }
  return $property.Value
}

function Test-TerminalStatusEvent {
  param($Event, [string]$Stage)

  if ($Stage -in @("terminal", "finished", "completed", "cancelled")) {
    return $true
  }

  $terminalValue = Get-StatusEventValue -Event $Event -Name "terminal" -DefaultValue $false
  if ($terminalValue -is [bool]) {
    return [bool]$terminalValue
  }
  return ([string]$terminalValue) -match '^(?i:true|1|yes|si)$'
}

function Normalize-TerminalResult {
  param($Event, [string]$Level)

  $result = ([string](Get-StatusEventValue -Event $Event -Name "result" -DefaultValue "")).Trim().ToLowerInvariant()
  switch -Regex ($result) {
    '^(success|succeeded|ok|completed|complete)$' { return "success" }
    '^(cancelled|canceled|cancelado)$' { return "cancelled" }
    '^(error|failed|failure|fallido)$' { return "failed" }
  }

  $exitCodeValue = Get-StatusEventValue -Event $Event -Name "exitCode" -DefaultValue $null
  if ($null -ne $exitCodeValue) {
    try {
      if ([int]$exitCodeValue -eq 0) { return "success" }
      return "failed"
    } catch {
      # Continuar con el nivel si el valor no era numérico.
    }
  }

  switch ($Level) {
    "success" { return "success" }
    "cancelled" { return "cancelled" }
    "error" { return "failed" }
    default { return "" }
  }
}

function Apply-StatusEvent {
  param($Event)

  $stage = [string](Get-StatusEventValue -Event $Event -Name "stage" -DefaultValue "")
  $level = [string](Get-StatusEventValue -Event $Event -Name "level" -DefaultValue "info")
  $message = [string](Get-StatusEventValue -Event $Event -Name "message" -DefaultValue "Actualizando estado...")
  $percentValue = Get-StatusEventValue -Event $Event -Name "percent" -DefaultValue $progress.Value
  try {
    $percent = [Math]::Max(0, [Math]::Min(100, [int]$percentValue))
  } catch {
    $percent = $progress.Value
  }
  if ($level -eq "error") {
    $progress.Value = [Math]::Min(99, $percent)
  } else {
    $progress.Value = $percent
  }
  $statusLabel.Text = $message
  if ($script:Cards.ContainsKey($stage)) {
    $script:LastStage = $stage
  }

  switch ($stage) {
    "preflight" { Set-CardState -Key "preflight" -State $(if ($level -eq "success") { "success" } elseif ($level -eq "error") { "error" } else { "working" }) }
    "build" {
      Set-CardState -Key "preflight" -State "success"
      Set-CardState -Key "build" -State $(if ($level -eq "success") { "success" } elseif ($level -eq "error") { "error" } else { "working" })
    }
    "services" {
      Set-CardState -Key "preflight" -State "success"
      Set-CardState -Key "build" -State "success"
      Set-CardState -Key "services" -State $(if ($level -eq "success") { "success" } elseif ($level -eq "error") { "error" } else { "working" })
    }
    "health" {
      Set-CardState -Key "preflight" -State "success"
      Set-CardState -Key "build" -State "success"
      Set-CardState -Key "services" -State "success"
      Set-CardState -Key "health" -State $(if ($level -eq "success") { "success" } elseif ($level -eq "error") { "error" } else { "working" })
    }
    "success" {
      foreach ($key in "preflight", "build", "services", "health") {
        Set-CardState -Key $key -State "success"
      }
      $statusLabel.ForeColor = $colorSuccess
    }
  }

  if ($level -eq "error") {
    $script:FailureObserved = $true
    if ($null -eq $script:ErrorObservedAt) {
      $script:ErrorObservedAt = Get-Date
    }
    if ($script:Cards.ContainsKey($script:LastStage)) {
      Set-CardState -Key $script:LastStage -State "error"
    }
    $statusLabel.ForeColor = $colorError
    $closeButton.Enabled = $true
    $closeButton.Text = "Cancelar y cerrar"
    $footer.Text = "Se detectó un error. Se esperan brevemente los diagnósticos; puede cancelar y cerrar ahora."
  }

  if (Test-TerminalStatusEvent -Event $Event -Stage $stage) {
    $script:TerminalReceived = $true
    $script:TerminalObservedAt = Get-Date
    $script:TerminalResult = Normalize-TerminalResult -Event $Event -Level $level
    $script:TerminalMessage = $message
    if ($script:TerminalResult -eq "failed" -and $null -eq $script:ErrorObservedAt) {
      $script:FailureObserved = $true
      $script:ErrorObservedAt = Get-Date
    }
  }

  if ($stage -ne "log") {
    Append-Activity -Message $message -Level $level
  }
}

function Read-StatusEvents {
  if ([string]::IsNullOrWhiteSpace($script:StatusFile) -or -not (Test-Path -LiteralPath $script:StatusFile)) {
    return
  }

  try {
    $statusContent = [System.IO.File]::ReadAllText($script:StatusFile, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrEmpty($statusContent)) {
      return
    }

    # La última posición solo es consumible cuando ya existe un salto de línea.
    # Así una escritura concurrente nunca descarta un JSON todavía incompleto.
    $lines = [regex]::Split($statusContent, "\r\n|\n|\r")
    $completeLineCount = [Math]::Max(0, $lines.Count - 1)
    while ($script:ProcessedStatusLines -lt $completeLineCount) {
      $line = $lines[$script:ProcessedStatusLines]
      if ([string]::IsNullOrWhiteSpace($line)) {
        $script:ProcessedStatusLines++
        continue
      }
      try {
        $statusEvent = $line | ConvertFrom-Json
        Apply-StatusEvent -Event $statusEvent
      } catch {
        # Una línea completa pero inválida no debe impedir leer eventos posteriores.
      }
      $script:ProcessedStatusLines++
    }
  } catch {
    # El archivo puede estar ocupado durante unos milisegundos.
  }
}

function Complete-BackendRun {
  if ($script:RunFinalized -or (Test-BackendProcessRunning)) {
    return
  }

  $exitCode = 1
  if ($null -ne $script:BackendProcess) {
    try {
      $exitCode = [int]$script:BackendProcess.ExitCode
    } catch {
      $exitCode = 1
    }
  }

  $result = "failed"
  if ($script:CancelRequested -or $script:TerminalResult -eq "cancelled") {
    $result = "cancelled"
  } elseif ($script:TerminalResult -eq "success") {
    $result = "success"
  } elseif ($script:FailureObserved -or $script:TerminalResult -eq "failed") {
    $result = "failed"
  } elseif ($exitCode -eq 0) {
    $result = "success"
  }

  $script:RunFinalized = $true
  $script:IsRunning = $false
  $script:RunState = $result
  $script:Stopwatch.Stop()
  $startButton.Enabled = $true
  $closeButton.Enabled = $true
  $closeButton.Text = "Cerrar"
  $footer.Text = "La operación terminó. Los detalles técnicos quedaron guardados en deploy_logs."

  switch ($result) {
    "success" {
      foreach ($key in "preflight", "build", "services", "health") {
        Set-CardState -Key $key -State "success"
      }
      $progress.Value = 100
      $statusLabel.Text = if ([string]::IsNullOrWhiteSpace($script:TerminalMessage)) { "NEXORA PRODUCCIÓN está disponible" } else { $script:TerminalMessage }
      $statusLabel.ForeColor = $colorSuccess
      Append-Activity -Message "Proceso finalizado correctamente." -Level "success"
      $startButton.Text = "Ejecutar nuevamente"
    }
    "cancelled" {
      $statusLabel.Text = "Operación cancelada"
      $statusLabel.ForeColor = $colorMuted
      Append-Activity -Message "La operación fue cancelada y el proceso se cerró." -Level "info"
      $startButton.Text = "Iniciar rebuild"
    }
    default {
      if ($script:Cards.ContainsKey($script:LastStage)) {
        Set-CardState -Key $script:LastStage -State "error"
      }
      $statusLabel.Text = "El rebuild terminó con errores"
      $statusLabel.ForeColor = $colorError
      Append-Activity -Message "Revise el registro técnico para ver el detalle del error." -Level "error"
      $startButton.Text = "Reintentar rebuild"
    }
  }

  [void](Dispose-BackendProcess)
  Remove-StatusFile
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500
$timer.Add_Tick({
  Read-StatusEvents
  if ($script:IsRunning) {
    $elapsedLabel.Text = $script:Stopwatch.Elapsed.ToString("mm\:ss")

    $backendRunning = Test-BackendProcessRunning
    if ($backendRunning) {
      $now = Get-Date
      $graceStartedAt = $null
      $graceSeconds = 0
      if ($script:TerminalReceived -and $null -ne $script:TerminalObservedAt) {
        $graceStartedAt = $script:TerminalObservedAt
        $graceSeconds = $script:TerminalGraceSeconds
      } elseif ($null -ne $script:ErrorObservedAt) {
        $graceStartedAt = $script:ErrorObservedAt
        $graceSeconds = $script:ErrorGraceSeconds
      }

      if ($null -ne $graceStartedAt -and ($now - $graceStartedAt).TotalSeconds -ge $graceSeconds) {
        $mayRetryTermination = $null -eq $script:LastTerminationAttemptAt -or ($now - $script:LastTerminationAttemptAt).TotalSeconds -ge 4
        if ($mayRetryTermination) {
          $script:LastTerminationAttemptAt = $now
          $script:TerminationAttempts++
          if ($script:TerminationAttempts -eq 1) {
            Append-Activity -Message "Los diagnósticos no finalizaron a tiempo. Se cerrará el proceso bloqueado." -Level "error"
          }
          $statusLabel.Text = "Cerrando el proceso bloqueado..."
          [void](Stop-BackendProcessTree -Reason "Tiempo de gracia agotado después de un error.")
          $backendRunning = Test-BackendProcessRunning
          if ($backendRunning) {
            $statusLabel.Text = "No se pudo cerrar el proceso. Puede usar Cancelar y cerrar."
          }
        }
      }
    }

    if (-not $backendRunning) {
      Read-StatusEvents
      Complete-BackendRun
    }
  }
})
$timer.Start()

$startButton.Add_Click({
  if (Test-BackendProcessRunning) {
    [void][System.Windows.Forms.MessageBox]::Show(
      "La ejecución anterior todavía está finalizando. Puede esperar o usar Cerrar para cancelarla de forma segura.",
      "NEXORA - Operación anterior activa",
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Information
    )
    return
  }
  if ($script:IsRunning) {
    Complete-BackendRun
  }
  if (Test-BackendProcessRunning) {
    return
  }

  [void](Dispose-BackendProcess)
  Remove-StatusFile

  $confirmation = [System.Windows.Forms.MessageBox]::Show(
    "Se reconstruirá NEXORA PRODUCCIÓN usando los archivos actuales del repositorio.`n`n¿Desea continuar?",
    "Confirmar rebuild de producción",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Warning,
    [System.Windows.Forms.MessageBoxDefaultButton]::Button2
  )
  if ($confirmation -ne [System.Windows.Forms.DialogResult]::Yes) {
    return
  }

  Reset-Interface
  Append-Activity -Message "Inicio solicitado por el usuario." -Level "working"
  $script:StatusFile = Join-Path $env:TEMP ("nexora-rebuild-gui-{0}-{1}.jsonl" -f $PID, (Get-Date -Format "yyyyMMddHHmmssfff"))
  if (Test-Path -LiteralPath $script:StatusFile) {
    [System.IO.File]::Delete($script:StatusFile)
  }
  $script:ProcessedStatusLines = 0
  $script:LastStage = "preflight"
  $script:RunFinalized = $false
  $script:RunState = "starting"
  $script:FailureObserved = $false
  $script:ErrorObservedAt = $null
  $script:TerminalReceived = $false
  $script:TerminalObservedAt = $null
  $script:TerminalResult = ""
  $script:TerminalMessage = ""
  $script:CancelRequested = $false
  $script:LastTerminationAttemptAt = $null
  $script:TerminationAttempts = 0

  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$BackendScript`" -NoPause -SkipConfirmation -StatusFile `"$script:StatusFile`""
  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $PowerShellPath
  $startInfo.Arguments = $arguments
  $startInfo.WorkingDirectory = $WindowsRepo
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

  try {
    $script:BackendProcess = New-Object System.Diagnostics.Process
    $script:BackendProcess.StartInfo = $startInfo
    if (-not $script:BackendProcess.Start()) {
      throw "No se pudo iniciar el proceso de rebuild."
    }
    $script:IsRunning = $true
    $script:RunState = "running"
    $script:Stopwatch.Restart()
    $startButton.Enabled = $false
    $closeButton.Enabled = $true
    $closeButton.Text = "Cancelar y cerrar"
    $footer.Text = "La operación está activa. Puede cancelarla de forma segura mediante Cancelar y cerrar."
    $statusLabel.Text = "Iniciando rebuild..."
  } catch {
    $script:IsRunning = $false
    $script:RunFinalized = $true
    $script:RunState = "failed"
    $startButton.Enabled = $true
    $closeButton.Enabled = $true
    $closeButton.Text = "Cerrar"
    $statusLabel.Text = $_.Exception.Message
    $statusLabel.ForeColor = $colorError
    Set-CardState -Key "preflight" -State "error"
    Append-Activity -Message $_.Exception.Message -Level "error"
    [void](Dispose-BackendProcess -Force)
    Remove-StatusFile
  }
})

$logsButton.Add_Click({
  try {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Start-Process -FilePath "explorer.exe" -ArgumentList "`"$LogDirectory`""
  } catch {
    [void][System.Windows.Forms.MessageBox]::Show(
      $_.Exception.Message,
      "No se pudieron abrir los registros",
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Error
    )
  }
})

$closeButton.Add_Click({ $form.Close() })

$form.Add_FormClosing({
  param($sender, $eventArgs)

  if ($script:AllowClose) {
    return
  }

  if (Test-BackendProcessRunning) {
    $eventArgs.Cancel = $true
    $decision = [System.Windows.Forms.MessageBox]::Show(
      "El rebuild todavía tiene un proceso activo.`n`n¿Desea cancelarlo y cerrar la ventana?",
      "NEXORA - Operación en curso",
      [System.Windows.Forms.MessageBoxButtons]::YesNo,
      [System.Windows.Forms.MessageBoxIcon]::Warning,
      [System.Windows.Forms.MessageBoxDefaultButton]::Button2
    )
    if ($decision -eq [System.Windows.Forms.DialogResult]::Yes) {
      $script:CancelRequested = $true
      $script:RunState = "cancelling"
      $statusLabel.Text = "Cancelando la operación..."
      Append-Activity -Message "Cancelación solicitada por el usuario." -Level "working"
      [void](Stop-BackendProcessTree -Reason "Cancelación solicitada al cerrar la ventana.")

      # Una vez confirmada la cancelación, la interfaz nunca vuelve a impedir el cierre.
      $script:AllowClose = $true
      $eventArgs.Cancel = $false
    }
  }
})

$form.Add_FormClosed({
  $timer.Stop()
  $backendStopped = -not (Test-BackendProcessRunning)
  if (-not $backendStopped) {
    [void](Stop-BackendProcessTree -Reason "Cierre de la interfaz.")
    $backendStopped = -not (Test-BackendProcessRunning)
  }
  if ($backendStopped) {
    Remove-StatusFile
  }
  [void](Dispose-BackendProcess -Force)
  if ($null -ne $logo.Image) {
    $logo.Image.Dispose()
  }
  if ($null -ne $form.Icon) {
    $form.Icon.Dispose()
  }
})

if ($ValidateOnly) {
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
  Write-Host "VALIDACIÓN GUI OK: recursos, controles e interfaz cargados correctamente."
  $form.Dispose()
  exit 0
}

[System.Windows.Forms.Application]::Run($form)
