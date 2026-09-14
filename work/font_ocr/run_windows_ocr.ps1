param(
    [string]$Manifest = "work/font_ocr/windows_rows/manifest.json",
    [string]$Output = "work/font_ocr/windows_ocr_raw.json"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

$script:AsTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq "AsTask" -and
        $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
    } |
    Select-Object -First 1

function Await-Result {
    param(
        [Parameter(Mandatory = $true)]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )
    $method = $script:AsTaskMethod.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait(-1) | Out-Null
    Write-Output $task.Result -NoEnumerate
}

function Invoke-Ocr {
    param(
        [Parameter(Mandatory = $true)][string]$ImagePath,
        [Parameter(Mandatory = $true)]$Engine
    )
    $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
    $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-Result ($Engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        return [ordered]@{
            image = $ImagePath
            width = $bitmap.PixelWidth
            height = $bitmap.PixelHeight
            text = $result.Text
            words = @(
                $result.Lines | ForEach-Object {
                    $_.Words | ForEach-Object {
                        [ordered]@{
                            text = $_.Text
                            x = $_.BoundingRect.X
                            y = $_.BoundingRect.Y
                            width = $_.BoundingRect.Width
                            height = $_.BoundingRect.Height
                        }
                    }
                }
            )
        }
    }
    finally {
        $stream.Dispose()
    }
}

$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$document = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
    [Windows.Globalization.Language]::new("ja")
)
if ($null -eq $engine) {
    throw "Japanese Windows OCR engine is unavailable"
}

$results = New-Object System.Collections.Generic.List[object]
$total = $document.rows.Count * $document.variants.Count
$done = 0
foreach ($row in $document.rows) {
    foreach ($variant in $document.variants) {
        $path = [string]$row.images.$variant
        $ocr = Invoke-Ocr -ImagePath $path -Engine $engine
        $results.Add([ordered]@{
            row_number = [int]$row.row_number
            variant = [string]$variant
            ocr = $ocr
        })
        $done++
        if (($done % 50) -eq 0 -or $done -eq $total) {
            Write-Host "OCR $done / $total"
        }
    }
}

$outputDocument = [ordered]@{
    schema_version = 1
    title = "Windows Japanese OCR results for SO3 glyph rows"
    manifest = $manifestPath
    manifest_sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
    language = "ja"
    engine = "Windows.Media.Ocr"
    results = $results
}
$outputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$json = $outputDocument | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "wrote $outputPath"
