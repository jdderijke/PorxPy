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
; settings and cache alone. An uninstall ASKS (v0.146.1): keeping the
; data is the default answer, and the only one a silent uninstall
; gets, so nothing is lost without a person saying so - but "uninstall,
; reinstall, start clean" no longer means digging in AppData. Before
; that it only said where the folder was, and a reinstall then found
; the old portfolios and funds and loaded them as if it were a first run.

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
; A fixed name, not PorxPy-Setup-<version> (v0.147.2): attached to a
; GitHub release under it, the installer has a permanent link,
; .../releases/latest/download/PorxPy-Setup.exe, that the README's
; download badge can hardcode -- the same reason porxpy_funds.zip has a
; fixed name. The version is not lost: it is in the release tag, the
; installer's title, and the exe's file properties (AppVersion).
OutputBaseFilename=PorxPy-Setup
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
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\PorxPy');
  if not DirExists(DataDir) then
    exit;
  // No is the default button and what /SILENT gets: deleting the user's
  // portfolios has to be somebody's explicit choice.
  if SuppressibleMsgBox('PorxPy has been removed.' + #13#10 + #13#10 +
         'Your portfolios, settings and fund data are still in' + #13#10 +
         DataDir + #13#10 + #13#10 +
         'Remove them as well?' + #13#10 + #13#10 +
         'Yes deletes them permanently, so a new install starts clean.' + #13#10 +
         'No keeps them, and a later install picks them up again.',
         mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    if DelTree(DataDir, True, True, True) then
      SuppressibleMsgBox('Your PorxPy data has been removed.', mbInformation, MB_OK, IDOK)
    else
      SuppressibleMsgBox('Some of your PorxPy data could not be removed (is PorxPy' + #13#10 +
             'still running?). What is left is in' + #13#10 + DataDir,
             mbError, MB_OK, IDOK);
  end;
end;
