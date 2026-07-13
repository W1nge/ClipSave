#define MyAppName "ClipSave"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyBuildSuffix
  #define MyBuildSuffix ""
#endif

[Setup]
AppId={{CDD59DF8-B837-4B4D-A6A3-80475EBF8D0D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=W1nge
AppPublisherURL=https://github.com/W1nge/ClipSave
AppSupportURL=https://github.com/W1nge/ClipSave/issues
AppUpdatesURL=https://github.com/W1nge/ClipSave/releases
DefaultDirName={localappdata}\Programs\ClipSave
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=build\release
OutputBaseFilename=ClipSave-{#MyAppVersion}{#MyBuildSuffix}-windows-x64-installer
SetupIconFile=assets\clipsave.ico
UninstallDisplayIcon={app}\ClipSave.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoCompany=W1nge
VersionInfoDescription=ClipSave Windows installer
VersionInfoProductName=ClipSave
VersionInfoProductVersion={#MyAppVersion}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "build\release\ClipSave\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build\release\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\release\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\release\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\release\THIRD_PARTY_LICENSES\*"; DestDir: "{app}\THIRD_PARTY_LICENSES"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\ClipSave.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\ClipSave.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipSave.exe"; Description: "Launch ClipSave"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      'ClipSave'
    );
end;
