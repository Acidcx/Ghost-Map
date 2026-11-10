; GhostMap-Setup.exe: installs the unpacked Ghost Map build in C:\Program Files\GhostMap and sets it up as the
; Windows service (the same "GhostMap.exe service install" as by hand). Built by build-exe.yml with Inno Setup 6:
;
;   python packaging/build_exe.py --onedir
;   iscc /DAppVersion=0.5.4 packaging\installer.iss          -> dist\GhostMap-Setup.exe
;
; Run it on the HMI (as administrator). Silent, e.g. for several PCs:
;   GhostMap-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;   GhostMap-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVICEARGS="--ixon"    (IXON web access, see the README)
; The exit code is 0 when the service is running, 10 when Setup copied the files but the service didn't start
; (see C:\ProgramData\GhostMap\logs\setup-service.log).
;
; Running a newer Setup upgrades in place and keeps the data and how the service was opened up (--ixon, --https...).
; Uninstalling (Settings > Apps) removes the service and the program and keeps the data in C:\ProgramData\GhostMap;
; "GhostMap.exe service uninstall --purge" before uninstalling deletes the data too.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\onedir\GhostMap"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{54222205-BC41-4068-BFFA-146A05321CFF}
AppName=Ghost Map
AppVersion={#AppVersion}
AppVerName=Ghost Map {#AppVersion}
AppPublisher=Ghost Map
AppComments=Read-only OT network mapper and machine health monitor
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Ghost Map
VersionInfoDescription=Ghost Map Setup
; The service runs as LocalSystem, so the program must sit where only administrators can change it. The service
; install code expects this folder, so it isn't offered as a choice.
DefaultDirName={autopf}\GhostMap
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Setup stops the service itself (PrepareToInstall below).
CloseApplications=no
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=GhostMap-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\GhostMap.exe
UninstallDisplayName=Ghost Map
SetupLogging=yes

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\OT-SAFETY.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Libraries of the version being replaced: none may linger next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; Opens the web UI of the running service in the browser (or starts Ghost Map in a window when there is none).
Name: "{autoprograms}\Ghost Map"; Filename: "{app}\GhostMap.exe"; Comment: "Open Ghost Map in the browser"

[Run]
Filename: "{app}\GhostMap.exe"; Description: "Open Ghost Map in the browser"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
var
  ServiceFailed: Boolean;

function ServiceLog: String;
begin
  Result := ExpandConstant('{commonappdata}\GhostMap\logs\setup-service.log');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  { An installed service holds GhostMap.exe and its libraries open: stop it so they can be replaced.
    "net stop" waits for it to stop; it fails harmlessly when there is no service yet. }
  Exec(ExpandConstant('{sys}\net.exe'), 'stop GhostMap', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(1500);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  rc: Integer;
  params: String;
begin
  if CurStep = ssPostInstall then
  begin
    { "service install" registers (or upgrades) and starts the service; with no options an upgrade keeps the
      current ones. Its output goes to a log, since Setup has no console. }
    ForceDirectories(ExtractFileDir(ServiceLog));
    params := '/C ""' + ExpandConstant('{app}\GhostMap.exe') + '" service install '
      + ExpandConstant('{param:SERVICEARGS|}') + ' > "' + ServiceLog + '" 2>&1"';
    WizardForm.StatusLabel.Caption := 'Setting up and starting the Ghost Map service...';
    if not Exec(ExpandConstant('{cmd}'), params, '', SW_HIDE, ewWaitUntilTerminated, rc) or (rc <> 0) then
    begin
      ServiceFailed := True;
      SuppressibleMsgBox('Ghost Map was installed, but its Windows service did not start (code ' + IntToStr(rc)
        + '). See ' + ServiceLog + '.' + #13#10#13#10
        + 'A common cause: Ghost Map is already running in a window. Close it and run Setup again.',
        mbError, MB_OK, IDOK);
    end;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if ServiceFailed then
    Result := 10
  else
    Result := 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Remove the service (keeping the data), then give its process a moment to let go of the files. }
    Exec(ExpandConstant('{app}\GhostMap.exe'), 'service uninstall', '', SW_HIDE, ewWaitUntilTerminated, rc);
    Sleep(1500);
  end;
end;
