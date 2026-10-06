# Case-study images: the workbench (all staff) and one filtered view, captured at native size, cropped to the
# 1440 x 900 canvas, retaken when Tableau's editor outlines show, then sliced into the dashboard's rows.
$root = Split-Path $PSScriptRoot -Parent
$img = Join-Path $root "docs\img"; $tmp = Join-Path $root ".captures"
function Capture($twbx, $name) {
    foreach ($try in 1..4) {
        & (Join-Path $PSScriptRoot "open_twb.ps1") -Twbx $twbx -Width 0 -Out (Join-Path $tmp "$name-raw.png") | Out-Null
        & (Join-Path $PSScriptRoot "crop_dashboard.ps1") -In (Join-Path $tmp "$name-raw.png") -Out (Join-Path $img "$name.png")
        if ($LASTEXITCODE -ne 2) { "$name ok (try $try)"; return }
    }
    throw "$name still shows editor outlines after 4 tries"
}
Capture (Join-Path $root "tableau\HR Analytics Workbench.twbx") "01-workbench"
$preset = Join-Path $tmp "F1 department.twbx"   # filtered copy, built fresh (test workbooks are not kept)
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $PSScriptRoot "build_twb.py") --out $preset --preset "Department=Data and Analytics" | Out-Null
Capture $preset "02-filtered"
Add-Type -AssemblyName System.Drawing
$b = [System.Drawing.Bitmap]::FromFile((Join-Path $img "01-workbench.png"))
foreach ($s in @(@("03-headline", 0, 180), @("04-row-one", 180, 235), @("05-row-two", 415, 235), @("06-at-risk", 650, 250))) {
    $c = $b.Clone((New-Object System.Drawing.Rectangle 0, $s[1], $b.Width, $s[2]), $b.PixelFormat)
    $c.Save((Join-Path $img "$($s[0]).png"), [System.Drawing.Imaging.ImageFormat]::Png); $c.Dispose()
}
$b.Dispose(); "sliced 4 rows"
