# Sobe o OpenTripPlanner e o backend FastAPI.
#   .\iniciar.ps1            -> usa o grafo já construído
#   .\iniciar.ps1 -Rebuild   -> regenera o GTFS a partir de dados\ e reconstrói o grafo
param([switch]$Rebuild)
$ErrorActionPreference = "Stop"
$raiz = $PSScriptRoot
$py = Join-Path $raiz ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    python -m venv (Join-Path $raiz ".venv")
    & $py -m pip install -r (Join-Path $raiz "requirements.txt")
}

$downloads = @{
    "otp\otp.jar"      = "https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar"
    "otp\poa.osm.pbf"  = "https://download.bbbike.org/osm/bbbike/PortoAlegre/PortoAlegre.osm.pbf"
}
foreach ($arq in $downloads.Keys) {
    $destino = Join-Path $raiz $arq
    if (-not (Test-Path $destino)) {
        Write-Host "Baixando $arq ..."
        Invoke-WebRequest -Uri $downloads[$arq] -OutFile $destino
    }
}

if ($Rebuild -or -not (Test-Path (Join-Path $raiz "otp\graph.obj"))) {
    & $py (Join-Path $raiz "etl\gerar_gtfs.py")
    Push-Location (Join-Path $raiz "otp")
    java -Xmx6G -jar otp.jar --build --save .
    Pop-Location
}

Write-Host "Iniciando OpenTripPlanner (porta 8080)..."
Start-Process java -ArgumentList "-Xmx4G", "-jar", "otp.jar", "--load", "." -WorkingDirectory (Join-Path $raiz "otp")

Write-Host "Iniciando backend em http://localhost:8000 ..."
Set-Location $raiz
& (Join-Path $raiz ".venv\Scripts\uvicorn.exe") backend.app:app --port 8000
