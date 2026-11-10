<#
.SYNOPSIS
  Authenticode-sign GhostMap.exe (or any file), timestamp it, and check the signature.

.DESCRIPTION
  Uses Set-AuthenticodeSignature, which is built into Windows PowerShell 5.1 and PowerShell 7 on Windows, so the
  Windows SDK (signtool.exe) is not needed. See docs/CODE-SIGNING.md for which certificate to use.

  Pick the certificate one of three ways:
    -PfxPath / -PfxBase64   a .pfx file (an internal-CA or self-signed cert). CI passes it base64 from a secret.
    -Thumbprint             a cert already in Cert:\CurrentUser\My or Cert:\LocalMachine\My (a work cert, or a
                            USB token / cloud HSM whose driver puts the cert there; you may be asked for the PIN).
    neither                 the only code-signing cert with a private key in CurrentUser\My, if there is exactly one.

  The file is always timestamped, so the signature stays valid after the certificate expires.

.EXAMPLE
  .\packaging\sign_exe.ps1                                   # local: your one code-signing cert
  .\packaging\sign_exe.ps1 -Thumbprint 3F2A...               # local: a specific cert
  .\packaging\sign_exe.ps1 -PfxPath C:\keys\ghostmap-sign.pfx -PfxPassword (Read-Host -AsSecureString)
#>
[CmdletBinding()]
param(
  [string]$Path = "dist\GhostMap.exe",
  [string]$PfxPath,
  [string]$PfxBase64,
  [object]$PfxPassword,  # SecureString or plain string (CI)
  [string]$Thumbprint,
  [string]$TimestampServer = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $Path)) { throw "$Path not found: build it first (python packaging/build_exe.py)" }
$Path = (Resolve-Path $Path).Path

function Test-CodeSigning($c) {
  $c.HasPrivateKey -and ($c.EnhancedKeyUsageList.ObjectId -contains "1.3.6.1.5.5.7.3.3")
}

$cert = $null
$ownCert = $false
$tempPfx = $null
try {
  if ($PfxBase64) {
    $tempPfx = Join-Path ([IO.Path]::GetTempPath()) ("ghostmap-sign-" + [guid]::NewGuid() + ".pfx")
    [IO.File]::WriteAllBytes($tempPfx, [Convert]::FromBase64String($PfxBase64.Trim()))
    $PfxPath = $tempPfx
  }
  if ($PfxPath) {
    $pw = if ($PfxPassword -is [securestring]) { [Net.NetworkCredential]::new("", $PfxPassword).Password } else { [string]$PfxPassword }
    # Without PersistKeySet the private key is only held while this script runs (removed on Dispose below).
    $flags = [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::UserKeySet
    $cert = [Security.Cryptography.X509Certificates.X509Certificate2]::new($PfxPath, $pw, $flags)
    $ownCert = $true
  } elseif ($Thumbprint) {
    $tp = ($Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpper()
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My | Where-Object Thumbprint -eq $tp | Select-Object -First 1
    if (-not $cert) { throw "no certificate with thumbprint $tp in CurrentUser\My or LocalMachine\My" }
  } else {
    $found = @(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.NotAfter -gt (Get-Date) })
    if ($found.Count -ne 1) {
      $found | Format-Table Thumbprint, Subject, NotAfter -AutoSize | Out-String | Write-Host
      throw "found $($found.Count) valid code-signing certificates in CurrentUser\My: pass -Thumbprint or -PfxPath"
    }
    $cert = $found[0]
  }
  if (-not (Test-CodeSigning $cert)) { throw "$($cert.Subject) is not a code-signing certificate with a private key" }
  if ($cert.NotAfter -lt (Get-Date)) { throw "$($cert.Subject) expired on $($cert.NotAfter)" }

  Write-Host "Signing $Path"
  Write-Host "  with  $($cert.Subject)  (thumbprint $($cert.Thumbprint), valid to $($cert.NotAfter.ToString('yyyy-MM-dd')))"
  Write-Host "  timestamp $TimestampServer"
  $sig = $null
  foreach ($try in 1..3) {  # timestamp servers have the odd hiccup
    $sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256 `
      -TimestampServer $TimestampServer -IncludeChain NotRoot -ErrorAction Continue
    $now = Get-AuthenticodeSignature -FilePath $Path
    if ($now.SignerCertificate -and $now.TimeStamperCertificate) { break }
    Write-Host "  attempt $try did not get a timestamp ($($sig.StatusMessage)); retrying"
    Start-Sleep -Seconds (5 * $try)
  }
} catch {
  if ($ownCert) { $cert.Dispose() }
  throw
} finally {
  if ($tempPfx) { Remove-Item $tempPfx -Force -ErrorAction SilentlyContinue }
}

# Check what is now on the file. "Valid" needs the signer's root to be trusted on this PC; a self-signed or
# internal-CA cert shows UnknownError on a PC that doesn't trust it yet, which is fine for the build machine.
$check = Get-AuthenticodeSignature -FilePath $Path
Write-Host "Signature: $($check.Status) - $($check.StatusMessage)"
if (-not $check.SignerCertificate -or $check.SignerCertificate.Thumbprint -ne $cert.Thumbprint) { throw "the file is not signed by $($cert.Thumbprint)" }
if ($check.Status -in "NotSigned", "HashMismatch", "NotSupportedFileFormat", "Incompatible") { throw "bad signature: $($check.Status)" }
if (-not $check.TimeStamperCertificate) { throw "the signature has no timestamp: it would stop validating when the certificate expires" }
Write-Host "Timestamped by $($check.TimeStamperCertificate.Subject)"
if ($ownCert) { $cert.Dispose() }
