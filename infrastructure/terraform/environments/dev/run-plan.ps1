<#
  run-plan.ps1  —  Real terraform plan for AEOS dev environment, from PowerShell.

  WHY THIS EXISTS:
    - PowerShell on this machine can reach AWS (aws sts get-caller-identity works).
    - Terraform's Go SDK does NOT understand the `login` / IAM Identity Center
      credential source, so we bridge by exporting concrete keys into env vars.
    - Two variables have no default and must be supplied: db_password, redis_auth_token.
      This script generates compliant values (RDS 8-41 chars; Redis 16-128 chars)
      for the PLAN ONLY. They are ephemeral and printed nowhere.

  WHAT IT DOES (read-only against AWS; NO apply):
    init -backend=false  ->  validate  ->  plan -out=aeos-dev.plan

  RUN:
    cd "D:\My projects\AEOS\infrastructure\terraform\environments\dev"
    powershell -ExecutionPolicy Bypass -File .\run-plan.ps1
#>

$ErrorActionPreference = "Stop"

# --- Locate tools ---------------------------------------------------------
$env:Path = "C:\Users\saich\bin;C:\Users\saich\bin\windows-amd64;" + $env:Path
$env:TF_CLI_CONFIG_FILE = "C:\Users\saich\.terraformrc"   # local provider mirror
$awsExe = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"

Set-Location "D:\My projects\AEOS\infrastructure\terraform\environments\dev"

# --- Credential bridge: login-session -> concrete env keys ----------------
Write-Host "==> Exporting AWS credentials from default profile..." -ForegroundColor Cyan
Remove-Item Env:\AWS_PROFILE -ErrorAction SilentlyContinue
$creds = & $awsExe configure export-credentials --profile default --format env-no-export
foreach ($line in $creds) {
    if ($line -match '^\s*([A-Z_]+)=(.*)$') {
        Set-Item -Path ("Env:\" + $Matches[1]) -Value $Matches[2]
    }
}
Write-Host "==> Verifying identity..." -ForegroundColor Cyan
& $awsExe sts get-caller-identity
if ($LASTEXITCODE -ne 0) { throw "STS identity check failed - AWS unreachable or creds expired." }

# --- Generate ephemeral, compliant secrets (plan-only) --------------------
function New-Secret([int]$len) {
    $alphabet = (65..90) + (97..122) + (48..57)   # A-Z a-z 0-9 (no shell-special chars)
    -join ($alphabet | Get-Random -Count $len | ForEach-Object { [char]$_ })
}
$env:TF_VAR_db_password      = New-Secret 24   # RDS: 8-41 chars
$env:TF_VAR_redis_auth_token = New-Secret 32   # Redis: 16-128 chars

# --- Terraform sequence ---------------------------------------------------
Write-Host "==> terraform init -backend=false" -ForegroundColor Cyan
terraform init -backend=false -input=false

Write-Host "==> terraform validate" -ForegroundColor Cyan
terraform validate

Write-Host "==> terraform plan (cicd_role_arn -> account 660249531916)" -ForegroundColor Cyan
terraform plan -input=false `
    -var "cicd_role_arn=arn:aws:iam::660249531916:role/aeos-cicd-role" `
    -out=aeos-dev.plan

Write-Host ""
Write-Host "==> PLAN COMPLETE. Saved to aeos-dev.plan" -ForegroundColor Green
Write-Host "    (No apply was performed. Secrets were ephemeral and are now discarded.)" -ForegroundColor Green
