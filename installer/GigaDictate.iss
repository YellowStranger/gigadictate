; GigaDictate installer (Inno Setup 6).
; Build: ISCC /DAppVersion=1.0.0 installer\GigaDictate.iss  (expects the PyInstaller output in dist\GigaDictate)
;
; Installs for the current user only (no administrator rights, no UAC prompt):
; GigaDictate should never run elevated, and a per-user install keeps it that way.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "GigaDictate"
#define AppExe "GigaDictate.exe"
#define SrcDir "..\dist\GigaDictate"

[Setup]
AppId={{6E0C7A52-3F0B-4C1E-9B6D-2A8F5D1C7E44}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} — голосовой ввод для Windows
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=GigaDictate-Setup-{#AppVersion}
SetupIconFile=..\GigaDictate.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
; the running app holds its files open: close it before updating or uninstalling
AppMutex=Local\GigaDictate-SingleInstance
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel2=Будет установлена программа [name/ver].%n%nGigaDictate печатает сказанное вами в любое поле ввода: в браузере, мессенджере, документе. Распознавание идёт прямо на компьютере моделью GigaAM, интернет не нужен, звук никуда не отправляется.%n%nУстановка не требует прав администратора и займёт около 320 МБ.
ru.FinishedLabel=Установка завершена.%n%nПосле запуска значок микрофона появится в области уведомлений возле часов.%n%nКак диктовать: удерживайте правый Ctrl и говорите, или нажмите его один раз, чтобы начать, и ещё раз, чтобы закончить. Esc — отмена.

[Tasks]
Name: "autostart"; Description: "Запускать GigaDictate при входе в Windows"; GroupDescription: "Дополнительно:"
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files left from an older version must not be mixed with the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Голосовой ввод в любое поле"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; the same value the app writes from its tray menu, so the menu checkbox stays in sync
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить GigaDictate"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\models"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // the tray menu may have turned autostart on without the installer task
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
    Data := ExpandConstant('{userappdata}\{#AppName}');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Удалить также настройки и журнал GigaDictate?' + #13#10 + Data,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
