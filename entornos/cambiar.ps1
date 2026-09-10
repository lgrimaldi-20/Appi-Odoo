<#
.SYNOPSIS
    Cambia de rama y deja el .env de esa rama activo, en un solo paso.

.DESCRIPTION
    Cada rama tiene su instancia de Odoo y su base de control. El .env NO viaja
    con la rama (esta en .gitignore a proposito: lleva credenciales), asi que
    tras un "git switch" el .env seguia apuntando a la instancia anterior. Ese
    desajuste es silencioso y peligroso: se escribe en el Odoo del otro cliente.

    Este script hace las dos cosas juntas para que no puedan separarse.

.EXAMPLE
    .\entornos\cambiar.ps1 Turicopy-V17
    .\entornos\cambiar.ps1              # solo sincroniza el .env con la rama actual
#>
param(
    [Parameter(Position = 0)]
    [string]$Rama
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
$dirEntornos = Join-Path $raiz 'entornos'

# Sin argumento: solo alinear el .env con la rama en la que ya estamos.
if (-not $Rama) {
    $Rama = (git -C $raiz rev-parse --abbrev-ref HEAD).Trim()
    Write-Host "Rama actual: $Rama" -ForegroundColor Cyan
}

$origen = Join-Path $dirEntornos "$Rama.env"
if (-not (Test-Path $origen)) {
    Write-Host "No hay entorno para la rama '$Rama'." -ForegroundColor Red
    Write-Host "Disponibles:" -ForegroundColor Yellow
    Get-ChildItem $dirEntornos -Filter '*.env' | ForEach-Object {
        Write-Host "  $($_.BaseName)"
    }
    exit 1
}

# Cambiar de rama solo si hace falta. Si hay cambios sin guardar que estorben,
# git se niega y nos paramos aqui: es preferible a perderlos.
$ramaActual = (git -C $raiz rev-parse --abbrev-ref HEAD).Trim()
if ($ramaActual -ne $Rama) {
    git -C $raiz switch $Rama
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git switch fallo; el .env NO se ha tocado." -ForegroundColor Red
        exit 1
    }
}

Copy-Item $origen (Join-Path $raiz '.env') -Force

# Mostrar a que instancia ha quedado apuntando: es la comprobacion que evita
# el error caro, y verla siempre cuesta menos que ir a buscarla.
$destino = Join-Path $raiz '.env'
$url = (Select-String -Path $destino -Pattern '^ODOO_URL=(.*)$').Matches.Groups[1].Value
$db  = (Select-String -Path $destino -Pattern '^ODOO_DB=(.*)$').Matches.Groups[1].Value
$ctl = (Select-String -Path $destino -Pattern '^DATABASE_URL=(.*)$').Matches.Groups[1].Value

Write-Host ""
Write-Host "Rama    : $Rama" -ForegroundColor Green
Write-Host "Odoo    : $url"
Write-Host "DB Odoo : $db"
Write-Host "Control : $ctl"
Write-Host ""
Write-Host "Reinicia uvicorn: DATABASE_URL se lee al arrancar." -ForegroundColor Yellow
