; L.U.N.A. Windows installer (ADR 0010, D2). Per-user, no admin rights.
; Compile through scripts/build_installer.py, which passes the defines:
;   ISCC /DAppVersion=0.1.0 /DEdition=public /DAppPublisher=... /DSourceDir=... /DOutputDir=... luna.iss
; "public" and "personal" differ only in the frozen bundle (personal ships the Piper voice runtime).

#ifndef AppVersion
  #error AppVersion is required (/DAppVersion=x.y.z)
#endif
#ifndef Edition
  #define Edition "public"
#endif
#ifndef AppPublisher
  #define AppPublisher "Orifdzhon"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\luna"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#if Edition == "personal"
  #define OutputName "LUNA-Setup-" + AppVersion + "-personal-x64"
#else
  #define OutputName "LUNA-Setup-" + AppVersion + "-x64"
#endif

[Setup]
AppId={{7D3B1E52-4C0A-4F58-9A6B-2E1C5D8A9F31}
AppName=L.U.N.A.
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\L.U.N.A
DefaultGroupName=L.U.N.A.
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=luna.ico
UninstallDisplayIcon={app}\luna.exe
UninstallDisplayName=L.U.N.A.
LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
; The app holds the named mutex Local\LUNA-instance (same session namespace as AppMutex).
AppMutex=LUNA-instance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.AutostartTask=Start L.U.N.A. when Windows starts
english.DeleteDataPrompt=Also delete your L.U.N.A. settings, logs, downloaded models and saved API keys?%n%nChoose No to keep them (recommended if you plan to reinstall).
russian.AutostartTask=Запускать L.U.N.A. при старте Windows
russian.DeleteDataPrompt=Удалить также настройки, журналы, загруженные модели и сохранённые ключи API L.U.N.A.?%n%nВыберите «Нет», чтобы сохранить их (рекомендуется, если вы планируете переустановку).

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"; Flags: unchecked
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[InstallDelete]
; Upgrades never mix DLLs of two versions.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; Never overwrite or remove the owner's config.
Source: "luna.default.toml"; DestDir: "{userappdata}\LUNA"; DestName: "luna.toml"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autoprograms}\L.U.N.A"; Filename: "{app}\luna.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\L.U.N.A"; Filename: "{app}\luna.exe"; WorkingDir: "{app}"; Tasks: desktopicon
; Inno appends ".lnk": the file is L.U.N.A.lnk, the same name as the tray toggle manages (luna.platform.windows.autostart.SHORTCUT_NAME).
Name: "{userstartup}\L.U.N.A"; Filename: "{app}\luna.exe"; Parameters: "--tray --minimized"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\luna.exe"; Description: "{cm:LaunchProgram,L.U.N.A.}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Also removes a shortcut the tray menu created.
Type: files; Name: "{userstartup}\L.U.N.A.lnk"

[Code]
var
  DeleteUserData: Boolean;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  DeleteUserData := False;
  { Silent uninstall always keeps user data. Default answer is No. }
  if not UninstallSilent() then
    DeleteUserData := MsgBox(CustomMessage('DeleteDataPrompt'), mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if not DeleteUserData then
    Exit;
  if CurUninstallStep = usUninstall then
  begin
    { Remove saved credentials while luna-cli.exe still exists; failures are ignored
      (builds without --forget-keys just exit non-zero). }
    if FileExists(ExpandConstant('{app}\luna-cli.exe')) then
      Exec(ExpandConstant('{app}\luna-cli.exe'), '--forget-keys', ExpandConstant('{app}'),
        SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    DelTree(ExpandConstant('{userappdata}\LUNA'), True, True, True);
    DelTree(ExpandConstant('{localappdata}\LUNA'), True, True, True);
  end;
end;
