; Flowra installer (Inno Setup 6)  ->  installer\Flowra-Setup-1.0.0.exe
#define AppName "Flowra"
#define AppVersion "1.0.0"

[Setup]
AppId={{6E7B2C1A-5F3D-4C8E-9A61-F10A7D2E4B93}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Flowra
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=installer
OutputBaseFilename=Flowra-Setup-{#AppVersion}
SetupIconFile=assets\flowra.ico
UninstallDisplayIcon={app}\Flowra.exe
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
AppMutex=Global\FlowraDictation
CloseApplications=yes
LicenseFile=THIRD_PARTY_NOTICES.txt

[Tasks]
Name: "startup"; Description: "Start Flowra when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "dist\Flowra\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Flowra.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Flowra.exe"; Tasks: desktopicon

[Registry]
; the installer's startup choice; Flowra reads it on first run and manages the Run entry after that
Root: HKCU; Subkey: "Software\Flowra"; ValueType: dword; ValueName: "LaunchAtStartup"; ValueData: 1; Tasks: startup; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Flowra"; ValueType: dword; ValueName: "LaunchAtStartup"; ValueData: 0; Tasks: not startup; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Flowra"; ValueData: """{app}\Flowra.exe"""; Tasks: startup; Flags: uninsdeletevalue
; always remove the Run entry on uninstall, even if it was added later from Flowra's settings
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Flowra"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Flowra.exe"; Description: "Launch Flowra now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM Flowra.exe /F"; Flags: runhidden; RunOnceId: "KillFlowra"

[Messages]
FinishedLabel=Flowra is installed. Look for its icon in the system tray: hold Right Ctrl to dictate, or double-tap it for hands-free.%n%nThe first launch downloads the speech model (about 1.6 GB on NVIDIA GPUs, 0.5 GB otherwise).
