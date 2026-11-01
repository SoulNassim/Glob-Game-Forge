; Windows installer for Glob (Inno Setup 6). Built in CI by .github/workflows/release.yml:
;   iscc /DAppVersion=1.2.3 packaging\windows\glob.iss
; Expects the PyInstaller output in dist\Glob (python scripts/build_desktop.py).
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Glob.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F3C2B1A-5D47-4E8B-9A0C-61F0B9E2D7A4}
AppName=Glob
AppVersion={#AppVersion}
AppPublisher=SoulNassim
AppPublisherURL=https://github.com/SoulNassim/Glob-Game-Forge
AppSupportURL=https://github.com/SoulNassim/Glob-Game-Forge/issues
DefaultDirName={localappdata}\Programs\Glob
DefaultGroupName=Glob
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Glob-Setup-Windows
SetupIconFile=..\..\forge\ui\glob.ico
UninstallDisplayIcon={app}\Glob.exe
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\Glob\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Glob"; Filename: "{app}\Glob.exe"
Name: "{autodesktop}\Glob"; Filename: "{app}\Glob.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Glob.exe"; Description: "{cm:LaunchProgram,Glob}"; Flags: nowait postinstall skipifsilent
