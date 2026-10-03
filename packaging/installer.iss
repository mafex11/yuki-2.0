; Yuki per-user installer (Inno Setup 6). No admin: installs to %LOCALAPPDATA%\Programs\Yuki Assistant,
; writes only HKCU. User data (%LOCALAPPDATA%\Yuki: memory, settings, logs, models) is never touched
; unless the user asks the uninstaller to remove it.
;
; Built by packaging\build.ps1:  ISCC /DAppVersion=0.1.0 /DSourceDir=packaging\dist\Yuki packaging\installer.iss
; Signing: CI signs the executables before this step and the setup .exe after it (see .github/workflows/build.yml).

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\Yuki"
#endif

#define AppName "Yuki"
#define AppExe "Yuki.exe"
#define CliExe "yuki-cli.exe"

[Setup]
; Never change AppId: upgrades and the uninstaller find the installation by it.
AppId={{31EF866E-175D-4C07-B3A1-BF2E0F7D374A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Yuki
VersionInfoVersion={#AppVersion}
; Per-user, no elevation. {autopf} is %LOCALAPPDATA%\Programs in this mode. Not "...\Yuki": other
; apps named Yuki already use that folder on some PCs.
PrivilegesRequired=lowest
DefaultDirName={autopf}\Yuki Assistant
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=Output
OutputBaseFilename=YukiSetup-{#AppVersion}
SetupIconFile=yuki.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
; The wizard images: the startup splash's still frame (the planet over YUKI; the planet alone in the corner), drawn
; at each size Inno Setup picks from by DPI (make_icon.py --installer-images), so they are never stretched there.
WizardImageFile=installer\wizard-164x314.bmp,installer\wizard-202x386.bmp,installer\wizard-240x459.bmp,installer\wizard-269x515.bmp,installer\wizard-336x643.bmp
WizardSmallImageFile=installer\wizard-small-58.bmp,installer\wizard-small-77.bmp,installer\wizard-small-97.bmp,installer\wizard-small-124.bmp
; The tray holds files open: close Yuki's processes before replacing them (and we stop them ourselves too).
CloseApplications=force
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Start Yuki when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the whole runtime folder, so no file from an older build lingers.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; The same per-user Run value the tray's "Start with Windows" toggle writes (yuki.ui.startup), pointing here.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Yuki"; \
    ValueData: """{app}\{#AppExe}"" -m yuki.ui.boot"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start Yuki now"; Flags: nowait postinstall skipifsilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

procedure StopYuki();
var
  Code: Integer;
  Cli: String;
begin
  { Ends this installation's tray, memory service, window and transcriber (never other apps). }
  Cli := ExpandConstant('{app}\{#CliExe}');
  if FileExists(Cli) then
    Exec(Cli, '-m yuki.core.procs stop yuki.ui.boot yuki.memory.service yuki.ui.window yuki.memory.transcribe yuki.mcp.server',
         '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopYuki();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Flag: String;
begin
  { Record the installer's start-with-Windows choice so the tray's one-time default respects it. }
  if CurStep = ssPostInstall then
  begin
    Flag := ExpandConstant('{localappdata}\Yuki\startup-default.json');
    if not FileExists(Flag) then
    begin
      ForceDirectories(ExtractFileDir(Flag));
      if WizardIsTaskSelected('startup') then
        SaveStringToFile(Flag, '{"by": "installer", "enabled": true}', False)
      else
        SaveStringToFile(Flag, '{"by": "installer", "enabled": false}', False);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value, Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopYuki();
    { Remove the Run value only when it starts this installation (a dev checkout may own it). }
    if RegQueryStringValue(HKCU, RunKey, 'Yuki', Value) then
      if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Value)) > 0 then
        RegDeleteValue(HKCU, RunKey, 'Yuki');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\Yuki');
    if (not UninstallSilent) and DirExists(Data) then
      if MsgBox('Also remove your Yuki data?' + #13#10#13#10 +
                'This deletes everything Yuki remembers about you (memory, settings, logs, downloaded models) in:' + #13#10 +
                Data + #13#10#13#10 + 'Choose No to keep it (reinstalling Yuki picks it up again).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
