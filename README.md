# QMK.Top Manager · SK75 Lighting Lab

**Публичный Windows-компаньон для Womier SK75 TMR: профили, подсветка и магнитные клавиши**

**A public Windows companion for Womier SK75 TMR: profiles, lighting and magnetic keys**

The app now runs offline by default. Network access is only available if you
explicitly enable the sniffer or a future update-check hook.

---

## 🇷🇺 Русский

### Что это?

QMK.Top Manager for SK75 TMR — настольное приложение для Windows и клавиатуры **Womier SK75 TMR**. Оно настраивает магнитные клавиши и подсветку, а при необходимости переключает обычный профиль по активному процессу.

### Создание проекта

По заявлению владельца репозитория, этот форк разрабатывался по его заданиям с
помощью OpenAI Codex: владелец задавал требования, проверял сборки и интерфейс,
но не писал строки исходного кода вручную. Это примечание об использовании ИИ
не устанавливает лицензию и не меняет авторские права.

### Возможности

- **Автоматическое переключение профилей** — привяжите профиль к любой программе (игре, редактору, браузеру), и клавиатура сама переключится когда вы в неё перейдёте
- **Мониторинг батареи** — для беспроводных клавиатур отображается уровень заряда в трее и в окне приложения (обновляется каждую минуту)
- **Управление частотой опроса** — переключайте polling rate (125–8000 Гц) вместе с профилем (только магнитные клавиатуры)
- **Управление подсветкой** — переключайте профиль подсветки вместе с основным профилем (только магнитные клавиатуры)
- **Lighting Lab для Womier SK75 TMR** — выбирайте RGB-цвет, яркость, скорость и вариант для штатных эффектов Womier
- **Magnetic Lab для Womier SK75 TMR** — точка активации, точка деактивации, Rapid Trigger, мёртвые зоны, RTStab и Snap Key
- **Работа в трее** — приложение сворачивается в системный трей и не мешает работе; иконка показывает уровень батареи
- **Автозапуск с Windows** — включается одной галочкой в настройках

### Поддерживаемая клавиатура

Эта сборка работает только с **Womier SK75 TMR** (магнитные свитчи, Vendor ID `0x3151`). Подключение — USB-кабель или 2.4G-донгл.

### Как пользоваться

1. **Запустите приложение** и подключите клавиатуру (USB или донгл).
2. Тип свитчей выбирать не нужно: эта сборка работает только с магнитной
   **Womier SK75 TMR**.
3. Чистая установка не содержит чужих названий профилей, привязок процессов, RGB-цветов или магнитных значений. Приложение безопасно читает текущие параметры клавиатуры; если установлен официальный Womier Driver, один раз импортируются только его магнитные профили.
4. **Настройте профили** — дайте имена, выберите частоту опроса и подсветку.
5. **Добавьте привязки к программам** — укажите имя процесса (например `cs2.exe`) и целевой профиль.
6. **Закройте окно** — приложение уйдёт в трей и продолжит работать.

Все локальные данные приложения, включая рабочий JSON, хранятся отдельно для каждого пользователя в `%LOCALAPPDATA%\QMK.Top Manager for SK75 TMR\profiles_config.json`. Файл рядом с EXE не создаётся и не требуется; старая папка `%LOCALAPPDATA%\QMK.Top Manager` не используется и не переносится автоматически.

### Перенос CFG

В «Настройках» нажмите кнопку **CFG**, отметьте нужные карточки и затем скопируйте, вставьте из буфера или импортируйте конфигурацию. Можно независимо выбрать:

- **Профили** — имена профилей и отмеченный профиль по умолчанию;
- **Lighting Lab** — настройки подсветки;
- **Magnetic Lab** — настройки магнитных клавиш;
- **Привязки к процессам** — правила переключения профилей для программ.

В файл попадают только выбранные пользовательские настройки. Невыбранные карточки не экспортируются и не меняются при импорте; это не полная резервная копия служебного состояния устройства.

### Lighting Lab: Womier SK75 TMR

1. Откройте карточку **Lighting Lab · SK75 TMR**.
2. Выберите штатный эффект Womier, укажите `#RRGGBB` или RGB, яркость, скорость и направление, затем нажмите **«Применить»**. Этот режим записывается в память клавиатуры.
3. Нажмите на цветной кубик, чтобы выбрать оттенок мышью в визуальном окне; до нажатия **«Применить»** цвет меняется только в интерфейсе.

### Magnetic Lab: Womier SK75 TMR

Откройте **Magnetic Lab · SK75 TMR** для точной настройки одной клавиши: точки активации и деактивации, Rapid Trigger и верхней/нижней мёртвых зон. Значение выбранной клавиши записывается после короткой паузы. **RTStab** считывается с клавиатуры при запуске. Для Snap Key выберите две клавиши: **«Убрать эту пару»** меняет только эти две клавиши и не переписывает настройки остальных.

### Запуск из исходников

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app_flet.py
```

### Сборка публичного EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build_release.ps1
```

Готовый один файл: `dist\QMK.Top Manager for SK75 TMR.exe`; рядом создаётся `*.exe.sha256` для проверки загрузки. В сборку не входят `profiles_config.json`, логи, виртуальное окружение или локальные данные разработчика.

### Требования

- Windows 10/11
- Womier SK75 TMR, подключённая по USB или через 2.4G донгл
- Для мониторинга батареи беспроводной клавиатуры: начальная настройка через встроенный сниффер (один раз)

---

## 🇬🇧 English

### What is this?

QMK.Top Manager for SK75 TMR is a Windows companion for the **Womier SK75 TMR** magnetic keyboard. It manages magnetic settings, lighting and optional process-based normal-profile switching.

### Project development

According to the repository maintainer, this fork was developed from the
maintainer's requirements with OpenAI Codex: the maintainer provided
requirements and tested builds and UI, but did not manually write source-code
lines. This AI-use disclosure does not grant a licence or change copyright.

### Features

- **Automatic profile switching** — bind a profile to any app (game, editor, browser), and the keyboard switches when you focus that window
- **Battery monitoring** — for wireless keyboards, battery level is shown in the system tray and app window (updates every minute)
- **Polling rate control** — switch polling rate (125–8000 Hz) together with profiles (magnetic keyboards only)
- **Lighting control** — switch lighting profiles along with keyboard profiles (magnetic keyboards only)
- **Lighting Lab for Womier SK75 TMR** — set RGB color, brightness, speed, and variant for built-in Womier effects
- **Magnetic Lab for Womier SK75 TMR** — tune actuation, deactivation, Rapid Trigger, dead zones, RTStab and Snap Key
- **System tray** — the app minimizes to tray and stays out of the way; the tray icon shows battery level
- **Windows autostart** — enable with a single checkbox

### Supported keyboard

This release supports **Womier SK75 TMR** magnetic switches only (Vendor ID `0x3151`), via USB or a 2.4G dongle.

### How to use

1. **Launch the app** and connect the keyboard.
2. No switch-type selection is required: this release supports only the
   magnetic **Womier SK75 TMR**.
3. A clean installation contains no publisher profile names, process bindings, RGB colours or magnetic data. The app reads current keyboard settings and can perform one read-only import of official Womier Driver magnetic profiles.
4. **Configure profiles** — set names, polling rate and lighting.
5. **Add process bindings** — specify a process name (e.g. `cs2.exe`) and target profile.
6. **Close the window** — the app goes to the tray and keeps running.

All local application data, including the runtime JSON, lives per user in `%LOCALAPPDATA%\QMK.Top Manager for SK75 TMR\profiles_config.json`, never beside the EXE. The legacy `%LOCALAPPDATA%\QMK.Top Manager` folder is not used or imported automatically.

### CFG transfer

In **Settings**, press **CFG**, select the cards you need, then copy, paste from the clipboard, or import the configuration. Each category can be chosen independently:

- **Profiles** — profile names and the marked default profile;
- **Lighting Lab** — lighting settings;
- **Magnetic Lab** — magnetic-key settings;
- **Process bindings** — profile-switching rules for applications.

The transfer file contains only the selected user-facing settings. Unselected cards are neither exported nor changed on import; this is not a complete backup of device-internal state.

### Lighting Lab: Womier SK75 TMR

Open **Lighting Lab · SK75 TMR**, choose a built-in Womier effect, set HEX or RGB colour, and press **Apply** to write it to keyboard memory. Click the colour swatch to choose a colour visually before applying it.

### Magnetic Lab: Womier SK75 TMR

Use **Magnetic Lab · SK75 TMR** to tune one key at a time: actuation/deactivation, Rapid Trigger and dead zones. The selected key is saved after a short pause; RTStab is read on startup. The Snap Key clear action addresses only the two selected keys, without rewriting the rest of the keyboard.

### Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app_flet.py
```

### Build the public EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build_release.ps1
```

The one-file result is `dist\QMK.Top Manager for SK75 TMR.exe`; a neighboring `*.exe.sha256` file verifies the download. It excludes `profiles_config.json`, logs, virtual environments and developer-specific data.

### Requirements

- Windows 10/11
- Womier SK75 TMR connected via USB or 2.4G dongle
- For wireless battery monitoring: one-time setup via the built-in sniffer

---

## Screenshots

*Coming soon*

## License

Choose and add a licence file before publishing the repository. The project
does not currently ship a licence file.
