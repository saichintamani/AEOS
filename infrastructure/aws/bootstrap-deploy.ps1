<#
  bootstrap-deploy.ps1 - One-time AWS setup for Milestone 14.2 (run from an ADMIN session).

  This is the LAST action you take as an admin/root-capable identity. It creates the
  minimum footprint needed so that every subsequent operation runs as the scoped
  `aeos-deploy` role instead of root:

    1. S3 state bucket        aeos-tfstate-<account>   (versioned, encrypted, private)
    2. DynamoDB lock table    aeos-tflock              (PAY_PER_REQUEST)
    3. IAM policies           aeos-deploy + aeos-deploy-services
                              (split: a single doc exceeds IAM's 6144-char PolicySize
                               limit, and the 14.2 apply loop only grows it)
    4. IAM user               aeos-deployer            (the human assumer; MFA enabled separately)
    5. (optional) GitHub OIDC provider                 (-EnableOidc)
    6. IAM role               aeos-deploy              (from aeos-deploy-trust.json; both policies attached)

  ORDER MATTERS: the trust policy (aeos-deploy-trust.json) names BOTH the aeos-deployer
  user AND the GitHub OIDC provider as principals, and IAM validates that referenced
  principals already exist. So the user and the OIDC provider are created BEFORE the role.
  Because the bundled trust references the OIDC provider unconditionally, -EnableOidc is
  effectively required for a first create; the role step verifies the provider exists and
  fails loudly with guidance if it does not.

  It is IDEMPOTENT: re-running updates in place (new policy version, refreshed trust,
  re-attached policies) and never errors on "already exists". These resources are
  effectively free - unlike the `terraform apply` they enable.

  MFA is NOT fully automatable (enabling a virtual device needs two live TOTP codes).
  The script creates the user and prints the exact enable-mfa commands to finish.

  RUN:
    cd "D:\My projects\AEOS\infrastructure\aws"
    powershell -ExecutionPolicy Bypass -File .\bootstrap-deploy.ps1 -EnableOidc
    # -EnableOidc creates the GitHub Actions OIDC provider (referenced by the trust policy)
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
$trustFile = Join-Path $PSScriptRoot "iam\aeos-deploy-trust.json"

# Two managed policies (see header): control-plane vs services. Attached to the
# same role. Each stays well under the 6144-char PolicySize limit with headroom
# for the 14.2 apply loop to add denied actions.
$policies = @(
  [pscustomobject]@{ Name = $PolicyName
                     File = Join-Path $PSScriptRoot "iam\aeos-deploy-permissions.json"
                     Arn  = "arn:aws:iam::${Account}:policy/$PolicyName" }
  [pscustomobject]@{ Name = "$PolicyName-services"
                     File = Join-Path $PSScriptRoot "iam\aeos-deploy-services-permissions.json"
                     Arn  = "arn:aws:iam::${Account}:policy/$PolicyName-services" }
)
$oidcArn = "arn:aws:iam::${Account}:oidc-provider/token.actions.githubusercontent.com"

foreach ($f in @($trustFile) + ($policies.File)) {
  if (-not (Test-Path $f)) { throw "Required policy file not found: $f" }
}

# Small helper: returns $true if a CLI probe succeeds (resource exists).
function Test-AwsExists([scriptblock]$probe) {
  try { & $probe *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}
# Throw if the most recent native command failed ($ErrorActionPreference does NOT
# apply to external exes, so mutating calls must be checked explicitly - the first
# version of this script silently printed "created" over real failures).
function Assert-LastExit([string]$what) {
  if ($LASTEXITCODE -ne 0) { throw "AWS call failed: $what (exit $LASTEXITCODE). See error above." }
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
  Write-Host "  S3 $bucket | DynamoDB $lockTable | IAM policies $PolicyName(+services) | IAM user $UserName | IAM role $RoleName" -ForegroundColor Yellow
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
  Assert-LastExit "create-bucket $bucket"
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
  Assert-LastExit "create-table $lockTable"
  Write-Host "    creating - waiting for ACTIVE..." -ForegroundColor Green
  & $awsExe dynamodb wait table-exists --table-name $lockTable --region $Region
  Write-Host "    active" -ForegroundColor Green
}

# --- 3. IAM policies (create or new default version) -----------------------
Write-Host "==> [3/6] IAM policies (aeos-deploy + aeos-deploy-services)" -ForegroundColor Cyan
foreach ($p in $policies) {
  if (Test-AwsExists { & $awsExe iam get-policy --policy-arn $p.Arn }) {
    # AWS caps a policy at 5 versions; prune non-default ones before adding.
    $versions = & $awsExe iam list-policy-versions --policy-arn $p.Arn --output json | ConvertFrom-Json
    $stale = $versions.Versions | Where-Object { -not $_.IsDefaultVersion }
    if ($stale.Count -ge 4) {
      $oldest = $stale | Sort-Object CreateDate | Select-Object -First 1
      & $awsExe iam delete-policy-version --policy-arn $p.Arn --version-id $oldest.VersionId | Out-Null
      Write-Host "    [$($p.Name)] pruned stale version $($oldest.VersionId)" -ForegroundColor DarkGray
    }
    & $awsExe iam create-policy-version --policy-arn $p.Arn `
      --policy-document "file://$($p.File)" --set-as-default | Out-Null
    Assert-LastExit "create-policy-version $($p.Name)"
    Write-Host "    [$($p.Name)] updated (new default version)" -ForegroundColor Green
  } else {
    & $awsExe iam create-policy --policy-name $p.Name `
      --policy-document "file://$($p.File)" | Out-Null
    Assert-LastExit "create-policy $($p.Name)"
    Write-Host "    [$($p.Name)] created" -ForegroundColor Green
  }
}

# --- 4. IAM user (the human assumer) --------------------------------------
# Created BEFORE the role: the trust policy names this user as a principal.
Write-Host "==> [4/6] IAM user $UserName" -ForegroundColor Cyan
if (Test-AwsExists { & $awsExe iam get-user --user-name $UserName }) {
  Write-Host "    exists" -ForegroundColor DarkGray
} else {
  & $awsExe iam create-user --user-name $UserName | Out-Null
  Assert-LastExit "create-user $UserName"
  Write-Host "    created" -ForegroundColor Green
}

# --- 5. GitHub OIDC provider ----------------------------------------------
# Also created BEFORE the role: the trust policy names it as a Federated principal.
if ($EnableOidc) {
  Write-Host "==> [5/6] GitHub Actions OIDC provider" -ForegroundColor Cyan
  if (Test-AwsExists { & $awsExe iam get-open-id-connect-provider --open-id-connect-provider-arn $oidcArn }) {
    Write-Host "    exists" -ForegroundColor DarkGray
  } else {
    # Modern IAM validates the cert chain automatically; a thumbprint is still
    # required by the CLI. GitHub's is stable but confirm if creation fails.
    & $awsExe iam create-open-id-connect-provider `
      --url "https://token.actions.githubusercontent.com" `
      --client-id-list "sts.amazonaws.com" `
      --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" | Out-Null
    Assert-LastExit "create-open-id-connect-provider"
    Write-Host "    created (verify thumbprint if you hit trust errors)" -ForegroundColor Green
  }
} else {
  Write-Host "==> [5/6] OIDC provider skipped (pass -EnableOidc to create)" -ForegroundColor DarkGray
}

# --- 6. IAM role (create or refresh trust) + attach both policies ----------
Write-Host "==> [6/6] IAM role $RoleName" -ForegroundColor Cyan
# The bundled trust references the OIDC provider unconditionally; fail early and
# clearly if it is missing rather than emitting a cryptic MalformedPolicyDocument.
if (-not (Test-AwsExists { & $awsExe iam get-open-id-connect-provider --open-id-connect-provider-arn $oidcArn })) {
  throw "Trust policy references the GitHub OIDC provider but it does not exist. Re-run with -EnableOidc (or create it) before the role."
}
if (Test-AwsExists { & $awsExe iam get-role --role-name $RoleName }) {
  & $awsExe iam update-assume-role-policy --role-name $RoleName `
    --policy-document "file://$trustFile" | Out-Null
  Assert-LastExit "update-assume-role-policy $RoleName"
  Write-Host "    exists - trust policy refreshed" -ForegroundColor Green
} else {
  & $awsExe iam create-role --role-name $RoleName `
    --assume-role-policy-document "file://$trustFile" `
    --description "AEOS scoped deploy role (Milestone 14.1)" | Out-Null
  Assert-LastExit "create-role $RoleName"
  Write-Host "    created" -ForegroundColor Green
}
foreach ($p in $policies) {
  & $awsExe iam attach-role-policy --role-name $RoleName --policy-arn $p.Arn | Out-Null
  Assert-LastExit "attach-role-policy $($p.Name)"
  Write-Host "    attached $($p.Name)" -ForegroundColor Green
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
