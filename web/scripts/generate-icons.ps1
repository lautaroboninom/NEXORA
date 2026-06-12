param(
  [string]$Source = "public/branding/isotipo-nexora.png"
)

$ErrorActionPreference = 'Stop'

$webRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Resolve-AssetPath {
  param([string]$Path)

  if ([System.IO.Path]::IsPathRooted($Path)) {
    if (Test-Path $Path) {
      return (Resolve-Path $Path).Path
    }
  } else {
    foreach ($candidate in @((Join-Path $webRoot $Path), (Join-Path (Get-Location) $Path))) {
      if (Test-Path $candidate) {
        return (Resolve-Path $candidate).Path
      }
    }
  }

  return $null
}

$sourcePath = Resolve-AssetPath $Source
if (-not $sourcePath) {
  Write-Error "No se encontró la imagen de origen: $Source. Copiá el isotipo como 'web/public/branding/isotipo-nexora.png' o pasá -Source."
}

$outDir = Join-Path $webRoot 'public/icons'
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

Add-Type -AssemblyName System.Drawing

function Export-Size {
  param(
    [System.Drawing.Image]$Img,
    [int]$Size,
    [string]$OutPath,
    [double]$Pad = 0.12,
    [System.Drawing.Rectangle]$Crop = [System.Drawing.Rectangle]::Empty
  )
  $bmp = New-Object System.Drawing.Bitmap $Size, $Size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'HighQuality'
  $g.Clear([System.Drawing.Color]::FromArgb(0,0,0,0))

  $sourceRect = if ($Crop.IsEmpty) {
    New-Object System.Drawing.Rectangle 0, 0, $Img.Width, $Img.Height
  } else {
    $Crop
  }

  $w = $sourceRect.Width; $h = $sourceRect.Height
  $safe = [int]([math]::Round($Size * (1 - $Pad)))
  $scale = [Math]::Min($safe / $w, $safe / $h)
  if ($scale -le 0) { $scale = 1 }
  $nw = [int]([math]::Round($w * $scale))
  $nh = [int]([math]::Round($h * $scale))
  $x = [int](($Size - $nw) / 2)
  $y = [int](($Size - $nh) / 2)

  $rect = New-Object System.Drawing.Rectangle $x, $y, $nw, $nh
  $g.DrawImage($Img, $rect, $sourceRect, [System.Drawing.GraphicsUnit]::Pixel)
  $bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
}

$img = [System.Drawing.Image]::FromFile($sourcePath)

$smallCrop = New-Object System.Drawing.Rectangle `
  ([int]([math]::Round($img.Width * 0.20))), `
  ([int]([math]::Round($img.Height * 0.30))), `
  ([int]([math]::Round($img.Width * 0.60))), `
  ([int]([math]::Round($img.Height * 0.40)))

Export-Size -Img $img -Size 16  -OutPath (Join-Path $outDir 'logo-app-16.png')  -Pad 0.02 -Crop $smallCrop
Export-Size -Img $img -Size 32  -OutPath (Join-Path $outDir 'logo-app-32.png')  -Pad 0.04 -Crop $smallCrop
Export-Size -Img $img -Size 16  -OutPath (Join-Path $outDir 'favicon-16.png')   -Pad 0.02 -Crop $smallCrop
Export-Size -Img $img -Size 32  -OutPath (Join-Path $outDir 'favicon-32.png')   -Pad 0.04 -Crop $smallCrop
Export-Size -Img $img -Size 180 -OutPath (Join-Path $outDir 'logo-app-180.png') -Pad 0.12
Export-Size -Img $img -Size 180 -OutPath (Join-Path $outDir 'apple-touch-icon-180.png') -Pad 0.12
Export-Size -Img $img -Size 192 -OutPath (Join-Path $outDir 'logo-app-192.png') -Pad 0.12
Export-Size -Img $img -Size 512 -OutPath (Join-Path $outDir 'logo-app-512.png') -Pad 0.12
Export-Size -Img $img -Size 512 -OutPath (Join-Path $outDir 'logo-app-512-maskable.png') -Pad 0.24

$img.Dispose()

Write-Host "Listo. Íconos generados en $outDir"
