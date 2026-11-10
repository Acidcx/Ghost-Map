# Code signing GhostMap.exe

GhostMap.exe is unsigned today. Signing it with an Authenticode certificate puts a named, verifiable publisher on
the file, so Windows, Defender and IT can tell that this exact file came from you and hasn't been changed.

## What signing does and doesn't fix

It does:

- Show a publisher (Properties > Digital Signatures, UAC prompts) instead of "Unknown publisher".
- Let IT allow Ghost Map **by publisher** instead of by path or hash: a Defender for Endpoint certificate indicator,
  or an AppLocker / WDAC publisher rule. A path exclusion is weaker, and a hash exclusion breaks on every release.
- With a certificate from a public CA (options C to E below), build Microsoft reputation over time, which is what
  calms SmartScreen and Defender's cloud ML on PCs that have never seen the file.

It doesn't:

- Guarantee Defender's ML stops flagging it. Reputation builds per certificate as more PCs run the file; a
  Microsoft submission of the signed file speeds that up.
- Stop Defender scanning the files the onefile exe unpacks to `%TEMP%\_MEI*` on every start, or each child process
  Ghost Map spawns. Removing the child processes, and later a signed installer that puts an unpacked (onedir) build in
  `C:\Program Files\GhostMap`, are the fixes for that CPU cost.

## Certificate options

| | Cost | Trusted on | Needs | Signs in GitHub Actions? |
|---|---|---|---|---|
| **A. Self-signed** | Free, 5 minutes | Only PCs where you import it (Root + Trusted Publishers) | Nothing | Yes, `.pfx` as a secret |
| **B. Company internal CA** (AD CS code-signing template) | Free if the company runs AD CS | Domain PCs automatically; workgroup HMIs after a one-time import of the company root | IT to issue it | Yes if IT lets the key be exported as `.pfx`; otherwise sign locally |
| **C. Public OV certificate** (DigiCert, Sectigo, SSL.com...) | about $200-500 a year | Every Windows PC | Company validation; since June 2023 the key must live on a USB token or the CA's cloud HSM | Only through the CA's cloud HSM service (extra cost); otherwise sign locally with the token |
| **D. Public EV certificate** | about $300-700 a year | Every Windows PC | Stricter company validation, token / cloud HSM | Same as C |
| **E. Azure Artifact Signing** (was "Trusted Signing") | about $10 a month | Every Windows PC | An Azure subscription in the company's name and Microsoft's identity validation of the company | Yes, with Microsoft's GitHub action; no key to look after |

Notes:

- **EV no longer buys instant SmartScreen reputation.** Microsoft stopped treating EV certificates specially in 2024,
  so for Ghost Map an EV certificate costs more than OV for the same result.
- **A and B don't build Microsoft reputation.** They make the signature valid on PCs you control and give IT a
  publisher to allow, which is often all an internal OT tool needs.
- **A's certificate becomes a trust anchor** on every PC you import it into: anyone with the `.pfx` can sign code
  those PCs trust. Keep it off shared drives, give it a strong password, and treat it as a stopgap.
- A public certificate (C to E) names the **company** as publisher, so it needs the company's agreement that Ghost
  Map is a company tool. Option B needs the same from IT.

## Recommended path

1. **Ask IT one question**: do we have a code-signing certificate, or an internal CA (AD CS) that can issue one?
   If yes, use **B**. It costs nothing, IT already trusts its own CA, and IT can allow Ghost Map by publisher.
2. If IT has nothing, and the company is happy to put its name on Ghost Map, use **E (Azure Artifact Signing)**. It is
   the cheapest publicly trusted option and the only one that signs in GitHub Actions without a token to babysit.
3. Use **A (self-signed)** only to try signing on a bench or test HMI while 1 or 2 is sorted out.
4. Don't buy EV.

Check whether you already have a code-signing certificate on your work PC (PowerShell):

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Format-List Subject, Issuer, NotAfter, Thumbprint
```

## Signing

`packaging/sign_exe.ps1` signs with SHA-256, timestamps the signature (so it stays valid after the certificate
expires), and checks the result. It uses PowerShell's built-in `Set-AuthenticodeSignature`, so the Windows SDK is
not needed. Run it after `python packaging/build_exe.py`:

```powershell
.\packaging\sign_exe.ps1                                   # your only code-signing cert in CurrentUser\My
.\packaging\sign_exe.ps1 -Thumbprint 3F2A...               # a specific cert (also USB tokens: you'll get a PIN prompt)
.\packaging\sign_exe.ps1 -PfxPath C:\keys\ghostmap-sign.pfx -PfxPassword (Read-Host -AsSecureString)
```

The default timestamp server is DigiCert's (`http://timestamp.digicert.com`); `-TimestampServer` changes it. The
build machine needs internet access for the timestamp; the HMIs don't.

Sign **before** computing the SHA-256 checksum: signing changes the file's hash.

### In GitHub Actions (options A and B)

`build-exe.yml` signs `dist\GhostMap.exe` when the repository has these secrets (Settings > Secrets and variables >
Actions > New repository secret):

- `GHOSTMAP_SIGN_PFX`: the `.pfx` file as base64. Make it with
  `[Convert]::ToBase64String([IO.File]::ReadAllBytes("ghostmap-sign.pfx")) | Set-Clipboard` and paste.
- `GHOSTMAP_SIGN_PFX_PASSWORD`: its password.

Without them the build still works and says, in the run summary, that the exe is unsigned. Every build also signs a
throwaway copy with a temporary self-signed certificate, so the signing script is tested even before a real
certificate exists. The smoke tests and the SHA-256 run on the signed exe.

Anyone with write access to the repository can use these secrets in a workflow, so keep write access to people
you'd trust with the certificate.

Option E uses Microsoft's own action (`azure/artifact-signing-action`) instead of a `.pfx`; that step is added once
the Azure account exists.

## Option A: a self-signed certificate

On your PC (PowerShell, not as admin):

```powershell
$c = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Ghost Map (internal)" `
  -KeyAlgorithm RSA -KeyLength 3072 -NotAfter (Get-Date).AddYears(3) -CertStoreLocation Cert:\CurrentUser\My
Export-Certificate -Cert $c -FilePath ghostmap-sign.cer                                    # public part, for the HMIs
Export-PfxCertificate -Cert $c -FilePath ghostmap-sign.pfx -Password (Read-Host -AsSecureString)  # private, keep safe
```

Trust it on each HMI (Command Prompt **as administrator**, copy only the `.cer` across):

```bat
certutil -addstore Root ghostmap-sign.cer
certutil -addstore TrustedPublisher ghostmap-sign.cer
```

On domain-joined PCs, IT can push the same `.cer` with a GPO instead: Computer Configuration > Policies > Windows
Settings > Security Settings > Public Key Policies > **Trusted Root Certification Authorities** and **Trusted
Publishers**.

## Checking a signed exe

- Explorer: right-click GhostMap.exe > Properties > **Digital Signatures**: the signer and a timestamp.
- PowerShell: `Get-AuthenticodeSignature .\GhostMap.exe | Format-List Status, StatusMessage, SignerCertificate, TimeStamperCertificate`.
  `Valid` means signed, unchanged and trusted on this PC. `UnknownError` with "terminated in a root certificate
  which is not trusted" means the signature is fine but this PC doesn't trust the certificate (option A or B before
  the import). `HashMismatch` means the file was changed after signing: don't run it.
- With the Windows SDK: `signtool verify /pa /v GhostMap.exe`.

## After the first signed release

- Submit the signed exe to Microsoft as a software developer (https://www.microsoft.com/wdsi/filesubmission) if
  Defender still flags it.
- Give IT the certificate (the `.cer`, or the publisher name for a public certificate) for a publisher-based allow
  rule, together with `docs/OT-SAFETY.md`.
