# trigger_daily_report.ps1
# Déclenche le workflow GitHub Actions "Rapport Journalier" via l'API GitHub.
# A configurer dans Windows Task Scheduler : lun-ven à 17h30 UTC (18h30 Paris heure d'été).
#
# Prérequis : créer un GitHub PAT avec permission "Actions: Write" sur le repo
# puis stocker la valeur dans la variable d'environnement GITHUB_PAT :
#   [System.Environment]::SetEnvironmentVariable("GITHUB_PAT", "ghp_xxx...", "User")

param(
    [string]$Date = ""   # Optionnel : forcer une date YYYY-MM-DD
)

$PAT  = $env:GITHUB_PAT
$REPO = "lorenzo18012004/cgf-brvm30-etf"
$URL  = "https://api.github.com/repos/$REPO/actions/workflows/daily_report.yml/dispatches"

if (-not $PAT) {
    Write-Error "Variable d'environnement GITHUB_PAT non definie."
    exit 1
}

$headers = @{
    "Authorization" = "Bearer $PAT"
    "Accept"        = "application/vnd.github+json"
    "Content-Type"  = "application/json"
    "X-GitHub-Api-Version" = "2022-11-28"
}

$body = if ($Date) {
    @{ ref = "main"; inputs = @{ date = $Date } } | ConvertTo-Json
} else {
    '{"ref":"main"}'
}

try {
    $response = Invoke-RestMethod -Uri $URL -Method POST -Headers $headers -Body $body
    Write-Host "[OK] $(Get-Date -Format 'yyyy-MM-dd HH:mm') - Workflow declenche."
} catch {
    Write-Error "[ERREUR] $($_.Exception.Message)"
    exit 1
}
