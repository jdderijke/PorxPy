; Inno Setup script for the PorxPy Windows installer (v0.145.0).
;
; Built by packaging/build.ps1, which passes the version read from
; porxpy/__init__.py - the one place a version lives - so this file
; never carries a number of its own:
;
;     ISCC.exe /DAppVersion=0.145.0 packaging\porxpy.iss
;
; It wraps the PyInstaller folder (dist\PorxPy) and nothing else. The
; user's data is NOT the installer's business: the app keeps it under
; %LOCALAPPDATA%\PorxPy (porxpy/config.py, DATA_DIR) and creates it on
; first run, so an upgrade replaces the program and leaves portfolios,
; settings and cache alone, and an uninstall does too - said on the
; last page of the uninstaller rather than deleted silently.

#ifndef AppVersion
  #error AppVersion is not defined - run packaging\build.ps1
#endif

[Setup]
; Fixed for ever: it is how Windows recognises an upgrade of THIS app.
AppId={{0B0A4D78-DA6A-4F5D-96D8-A0280D1FEDF2}
AppName=PorxPy
AppVersion={#AppVersion}
AppVerName=PorxPy {#AppVersion}
AppPublisher=PorxPy
AppPublisherURL=https://github.com/jdderijke/PorxPy
AppSupportURL=https://github.com/jdderijke/PorxPy/issues
; Per user by default, so no administrator is needed; the first page
; offers "for all users" to someone who has the rights.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\PorxPy
DefaultGroupName=PorxPy
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=PorxPy-Setup-{#AppVersion}
SetupIconFile=porxpy.ico
UninstallDisplayIcon={app}\PorxPy.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; A running PorxPy holds its files open; let the Restart Manager close it.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\PorxPy\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PorxPy"; Filename: "{app}\PorxPy.exe"; Comment: "Portfolio X-ray - opens in your browser"
Name: "{autodesktop}\PorxPy"; Filename: "{app}\PorxPy.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PorxPy.exe"; Description: "Start PorxPy now"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('PorxPy has been removed.' + #13#10 + #13#10 +
           'Your portfolios, settings and fund data were left in place, in' + #13#10 +
           ExpandConstant('{localappdata}\PorxPy') + #13#10 + #13#10 +
           'Delete that folder yourself if you no longer want them.',
           mbInformation, MB_OK, IDOK);
end;
