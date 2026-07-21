<#
  run-apply.ps1  —  Milestone 14.2 first real `terraform apply` for AEOS dev.

  READ THE RUNBOOK FIRST: docs/architecture/042-MILESTONE_14_2_APPLY_RUNBOOK.md
  This applies REAL, BILLABLE AWS infrastructure (EKS, RDS, NAT gateways,
  ElastiCache). It is NOT reversible for free — `terraform destroy` afterwards.

  Prerequisites (done once, interactively, per the runbook):
    - aeos-deploy IAM role + aeos-deployer user created (infrastructure/aws/iam)
    - S3 state bucket `aeos-tfstate-660249531916` + DynamoDB `aeos-tflock` exist
    - You can assume aeos-deploy (MFA satisfied)

  WHAT IT DOES:
    assume aeos-deploy -> init S3 backend -> apply the saved plan, TEE'd to a log.
    The log is then fed to scripts/capture_apply_failures.py.

  RUN:
    cd "D:\My projects\AEOS\infrastructure\terraform\environments\dev"
    powershell -ExecutionPolicy Bypass -File .\run-apply.ps1
#>

param(
  [string]$RoleArn   = "arn:aws:iam::660249531916:role/aeos-deploy",
  [string]$StateBucket = "aeos-tfstate-660249531916",
  [switch]$ReusePlan            # apply the saved aeos-dev.plan instead of re-planning
)

$ErrorActionPreference = "Stop"

$env:Path = "C:\Users\saich\bin;C:\Users\saich\bin\windows-amd64;" + $env:Path
$env:TF_CLI_CONFIG_FILE = "C:\Users\saich\.terraformrc"
$awsExe = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
Set-Location "D:\My projects\AEOS\infrastructure\terraform\environments\dev"

# Timestamped, unique-ish run id WITHOUT relying on a fixed clock format.
$runId = ([guid]::NewGuid().ToString("N")).Substring(0, 8)
$logPath = "apply-$runId.log"
Write-Host "==> Run id: $runId  (log: $logPath)" -ForegroundColor Cyan

# --- 1. Assume the scoped deploy role (NEVER root) ------------------------
Write-Host "==> Assuming $RoleArn ..." -ForegroundColor Cyan
Remove-Item Env:\AWS_PROFILE -ErrorAction SilentlyContinue
$assume = & $awsExe sts assume-role `
    --role-arn $RoleArn `
    --role-session-name "aeos-apply-$runId" `
    --output json | ConvertFrom-Json
if (-not $assume.Credentials) { throw "assume-role returned no credentials (MFA? trust policy? role exists?)" }
$env:AWS_ACCESS_KEY_ID     = $assume.Credentials.AccessKeyId
$env:AWS_SECRET_ACCESS_KEY = $assume.Credentials.SecretAccessKey
$env:AWS_SESSION_TOKEN     = $assume.Credentials.SessionToken

Write-Host "==> Identity now:" -ForegroundColor Cyan
& $awsExe sts get-caller-identity

# --- 2. Secrets (real apply needs stable values; do NOT regenerate on re-apply) ---
if (-not $env:TF_VAR_db_password)      { throw "Set `\$env:TF_VAR_db_password before running (RDS master password)." }
if (-not $env:TF_VAR_redis_auth_token) { throw "Set `\$env:TF_VAR_redis_auth_token before running (Redis auth token)." }

# --- 3. Init the S3 backend (this is the first time state is remote) -------
Write-Host "==> terraform init -backend-config=bucket=$StateBucket" -ForegroundColor Cyan
terraform init -input=false -backend-config="bucket=$StateBucket"

# --- 4. Apply, tee'ing ALL output to the log for the capture harness ------
Write-Host "==> terraform apply (output -> $logPath)" -ForegroundColor Yellow
if ($ReusePlan -and (Test-Path "aeos-dev.plan")) {
    terraform apply -input=false -auto-approve aeos-dev.plan 2>&1 | Tee-Object -FilePath $logPath
} else {
    terraform apply -input=false -auto-approve `
        -var "cicd_role_arn=arn:aws:iam::660249531916:role/aeos-cicd-role" `
        2>&1 | Tee-Object -FilePath $logPath
}
$applyExit = $LASTEXITCODE

Write-Host ""
Write-Host "==> apply exit code: $applyExit" -ForegroundColor Cyan
Write-Host "==> Extracting actionable failures:" -ForegroundColor Cyan
python "..\..\..\..\scripts\capture_apply_failures.py" $logPath --json "apply-$runId.report.json"

if ($applyExit -ne 0) {
    Write-Host ""
    Write-Host "APPLY DID NOT FULLY SUCCEED. This is expected on a first run." -ForegroundColor Yellow
    Write-Host "Fold the missing IAM actions above into aeos-deploy-permissions.json," -ForegroundColor Yellow
    Write-Host "re-attach the policy, then re-run. Repeat until zero findings." -ForegroundColor Yellow
}
