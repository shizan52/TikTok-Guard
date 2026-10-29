; Inno Setup script for TikTok Guard - built by `python buildexe/build.py installer` (it passes SourceDir, AppVersion, OutputDir).
;
; What the client sees:  double-click Setup  ->  Next  ->  (desktop shortcut: yes/no)  ->  Install  ->  Finish (opens the app).
; * Per-user install in %LOCALAPPDATA%\Programs\TikTok Guard: NO administrator rights, no UAC prompt.
; * Start menu group (app, diagnostics, read me, uninstall) and an optional desktop shortcut; "Apps & features" entry to uninstall.
; * The client's data (accounts, settings, replies) lives in %LOCALAPPDATA%\TikTok Guard, outside the program folder:
;   an update (run a newer Setup) never touches it, and uninstalling asks whether to delete it (default: keep).
; * A running TikTok Guard is never replaced or removed (AppMutex): the app holds that mutex while it runs.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #error SourceDir must point at the built TikTok Guard folder
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#define AppName "TikTok Guard"
#define AppExe "TikTok Guard.exe"

[Setup]
AppId={{7D4A3B9E-2C61-4F58-9E0A-6B1D84C5F273}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppMutex=TikTokGuardRunning
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
UsePreviousTasks=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile={#SourcePath}\icon.ico
WizardImageFile={#SourcePath}\wizard-large.bmp,{#SourcePath}\wizard-large-150.bmp,{#SourcePath}\wizard-large-200.bmp
WizardSmallImageFile={#SourcePath}\wizard-small.bmp,{#SourcePath}\wizard-small-150.bmp,{#SourcePath}\wizard-small-200.bmp
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename=TikTok-Guard-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany={#AppName}
VersionInfoCopyright=Copyright (c) 2026 {#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} Diagnostics"; Filename: "{app}\TikTok Guard Diagnostics.exe"; Comment: "Check this computer and make a report for support"
Name: "{group}\Read me"; Filename: "{app}\README.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]
// Uninstall: the program goes, the client's data stays unless they say otherwise (a silent uninstall never deletes it).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#AppName}');
    if DirExists(DataDir) and (not UninstallSilent()) then
      if MsgBox('Do you also want to delete your TikTok Guard data (accounts, settings, replies, screenshots)?' + #13#10 + #13#10 +
                'Choose No to keep it, for example if you will install TikTok Guard again.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
