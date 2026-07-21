<#
  bootstrap-deploy.ps1 - One-time AWS setup for Milestone 14.2 (run from an ADMIN session).

  This is the LAST action you take as an admin/root-capable identity. It creates the
  minimum footprint needed so that every subsequent operation runs as the scoped
  `aeos-deploy` role instead of root:

    1. S3 state bucket        aeos-tfstate-<account>   (versioned, encrypted, private)
    2. DynamoDB lock table    aeos-tflock              (PAY_PER_REQUEST)
    3. IAM policy             aeos-deploy              (from aeos-deploy-permissions.json)
    4. IAM role               aeos-deploy              (from aeos-deploy-trust.json)
    5. IAM user               aeos-deployer            (the human assumer; MFA enabled separately)
    6. (optional) GitHub OIDC provider                 (-EnableOidc)

  It is IDEMPOTENT: re-running updates in place (new policy version, refreshed trust,
  re-attached policy) and never errors on "already exists". These resources are
  effectively free - unlike the `terraform apply` they enable.

  MFA is NOT fully automatable (enabling a virtual device needs two live TOTP codes).
  The script creates the user and prints the exact enable-mfa commands to finish.

  RUN:
    cd "D:\My projects\AEOS\infrastructure\aws"
    powershell -ExecutionPolicy Bypass -File .\bootstrap-deploy.ps1
    # add -EnableOidc to also create the GitHub Actions OIDC provider
    # add -Yes to skip the confirmation prompt
#>

param(
  [string]$Account     = "660249531916",
  [string]$Region      = "us-east-1",
  [string]$RoleName    = "aeos-deploy",
  [string]$PolicyName  = "aeos-deploy",
  [string]$UserName    = "aeos-deployer",
  [switch]$EnableOidc,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
$env:Path = "C:\Users\saich\bin;C:\Users\saich\bin\windows-amd64;" + $env:Path
$awsExe = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"

$bucket    = "aeos-tfstate-$Account"
$lockTable = "aeos-tflock"
$permFile  = Join-Path $PSScriptRoot "iam\aeos-deploy-permissions.json"
$trustFile = Join-Path $PSScriptRoot "iam\aeos-deploy-trust.json"
$policyArn = "arn:aws:iam::${Account}:policy/$PolicyName"

foreach ($f in @($permFile, $trustFile)) {
  if (-not (Test-Path $f)) { throw "Required policy file not found: $f" }
}

# Small helper: returns $true if a CLI probe succeeds (resource exists).
function Test-AwsExists([scriptblock]$probe) {
  try { & $probe *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# --- 0. Confirm identity + intent -----------------------------------------
Write-Host "==> Caller identity:" -ForegroundColor Cyan
$ident = & $awsExe sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Cannot reach AWS. Ensure this admin session's creds are exported." }
$ident | Format-List Account, Arn | Out-Host
if ($ident.Account -ne $Account) {
  throw "Logged into account $($ident.Account) but expected $Account. Aborting."
}

if (-not $Yes) {
  Write-Host "This will create/update in $Account/${Region}:" -ForegroundColor Yellow
  Write-Host "  S3 $bucket | DynamoDB $lockTable | IAM policy+role $RoleName | IAM user $UserName" -ForegroundColor Yellow
  if ($EnableOidc) { Write-Host "  + GitHub OIDC provider" -ForegroundColor Yellow }
  $ans = Read-Host "Proceed? (yes/no)"
  if ($ans -ne "yes") { Write-Host "Aborted."; exit 1 }
}

# --- 1. S3 state bucket ----------------------------------------------------
Write-Host "==> [1/6] S3 state bucket $bucket" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe s3api head-bucket --bucket $bucket }) {
  Write-Host "    exists - ensuring config" -ForegroundColor DarkGray
} else {
  # us-east-1 must NOT be given a LocationConstraint (it's the API default).
  & $awsExe s3api create-bucket --bucket $bucket --region $Region | Out-Null
  Write-Host "    created" -ForegroundColor Green
}
& $awsExe s3api put-bucket-versioning --bucket $bucket `
  --versioning-configuration Status=Enabled | Out-Null
& $awsExe s3api put-bucket-encryption --bucket $bucket `
  --server-side-encryption-configuration '{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"AES256\"}}]}' | Out-Null
& $awsExe s3api put-public-access-block --bucket $bucket `
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true | Out-Null
Write-Host "    versioned + AES256 + public-access-blocked" -ForegroundColor Green

# --- 2. DynamoDB lock table ------------------------------------------------
Write-Host "==> [2/6] DynamoDB lock table $lockTable" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe dynamodb describe-table --table-name $lockTable --region $Region }) {
  Write-Host "    exists" -ForegroundColor DarkGray
} else {
  & $awsExe dynamodb create-table --table-name $lockTable --region $Region `
    --attribute-definitions AttributeName=LockID,AttributeType=S `
    --key-schema AttributeName=LockID,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST | Out-Null
  Write-Host "    creating - waiting for ACTIVE..." -ForegroundColor Green
  & $awsExe dynamodb wait table-exists --table-name $lockTable --region $Region
  Write-Host "    active" -ForegroundColor Green
}

# --- 3. IAM policy (create or new default version) -------------------------
Write-Host "==> [3/6] IAM policy $PolicyName" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe iam get-policy --policy-arn $policyArn }) {
  # AWS caps a policy at 5 versions; prune non-default ones before adding.
  $versions = & $awsExe iam list-policy-versions --policy-arn $policyArn --output json | ConvertFrom-Json
  $stale = $versions.Versions | Where-Object { -not $_.IsDefaultVersion }
  if ($stale.Count -ge 4) {
    $oldest = $stale | Sort-Object CreateDate | Select-Object -First 1
    & $awsExe iam delete-policy-version --policy-arn $policyArn --version-id $oldest.VersionId | Out-Null
    Write-Host "    pruned stale version $($oldest.VersionId)" -ForegroundColor DarkGray
  }
  & $awsExe iam create-policy-version --policy-arn $policyArn `
    --policy-document "file://$permFile" --set-as-default | Out-Null
  Write-Host "    updated (new default version)" -ForegroundColor Green
} else {
  & $awsExe iam create-policy --policy-name $PolicyName `
    --policy-document "file://$permFile" | Out-Null
  Write-Host "    created" -ForegroundColor Green
}

# --- 4. IAM role (create or refresh trust) ---------------------------------
Write-Host "==> [4/6] IAM role $RoleName" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe iam get-role --role-name $RoleName }) {
  & $awsExe iam update-assume-role-policy --role-name $RoleName `
    --policy-document "file://$trustFile" | Out-Null
  Write-Host "    exists - trust policy refreshed" -ForegroundColor Green
} else {
  & $awsExe iam create-role --role-name $RoleName `
    --assume-role-policy-document "file://$trustFile" `
    --description "AEOS scoped deploy role (Milestone 14.1)" | Out-Null
  Write-Host "    created" -ForegroundColor Green
}
& $awsExe iam attach-role-policy --role-name $RoleName --policy-arn $policyArn | Out-Null
Write-Host "    policy attached" -ForegroundColor Green

# --- 5. IAM user (the human assumer) --------------------------------------
Write-Host "==> [5/6] IAM user $UserName" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe iam get-user --user-name $UserName }) {
  Write-Host "    exists" -ForegroundColor DarkGray
} else {
  & $awsExe iam create-user --user-name $UserName | Out-Null
  Write-Host "    created" -ForegroundColor Green
}

# --- 6. Optional GitHub OIDC provider --------------------------------------
if ($EnableOidc) {
  Write-Host "==> [6/6] GitHub Actions OIDC provider" -ForegroundColor Cyan
  $oidcArn = "arn:aws:iam::${Account}:oidc-provider/token.actions.githubusercontent.com"
  if (Test-AwsExists { & $awsExe iam get-open-id-connect-provider --open-id-connect-provider-arn $oidcArn }) {
    Write-Host "    exists" -ForegroundColor DarkGray
  } else {
    # Modern IAM validates the cert chain automatically; a thumbprint is still
    # required by the CLI. GitHub's is stable but confirm if creation fails.
    & $awsExe iam create-open-id-connect-provider `
      --url "https://token.actions.githubusercontent.com" `
      --client-id-list "sts.amazonaws.com" `
      --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" | Out-Null
    Write-Host "    created (verify thumbprint if you hit trust errors)" -ForegroundColor Green
  }
} else {
  Write-Host "==> [6/6] OIDC provider skipped (pass -EnableOidc to create)" -ForegroundColor DarkGray
}

# --- Done: what's left for the operator ------------------------------------
Write-Host ""
Write-Host "=== BOOTSTRAP COMPLETE ===" -ForegroundColor Green
Write-Host "Remaining MANUAL step (MFA can't be fully scripted):" -ForegroundColor Yellow
$mfaArn = "arn:aws:iam::${Account}:mfa/${UserName}"
Write-Host "" -ForegroundColor Yellow
Write-Host "  The aeos-deploy trust policy requires MFA. Enable a virtual device on ${UserName}:" -ForegroundColor Yellow
Write-Host "    aws iam create-virtual-mfa-device --virtual-mfa-device-name ${UserName} --outfile qr.png --bootstrap-method QRCodePNG" -ForegroundColor Yellow
Write-Host "    # scan qr.png in an authenticator, then bind it with two consecutive codes:" -ForegroundColor Yellow
Write-Host "    aws iam enable-mfa-device --user-name ${UserName} --serial-number $mfaArn --authentication-code-1 <code1> --authentication-code-2 <code2>" -ForegroundColor Yellow
Write-Host "" -ForegroundColor Yellow
Write-Host "  Also give ${UserName} programmatic access (access keys or SSO) so it can assume" -ForegroundColor Yellow
Write-Host "  the role, then proceed to Milestone 14.2:" -ForegroundColor Yellow
Write-Host "    cd ..\terraform\environments\dev" -ForegroundColor Yellow
Write-Host "    powershell -File .\run-apply.ps1" -ForegroundColor Yellow
