# Android 备份/恢复应用（WiFi 密码） Spec

## Why
为已 root 的安卓设备提供一个简单的本地备份/恢复工具。第一版聚焦于备份并恢复设备已连接过的 WiFi 网络（含密码）。通过浏览器界面（PyWebIO）与用户交互，避免命令行参数的复杂性，同时把执行日志输出到控制台便于排查。

## What Changes
- 新增 Python 项目骨架：`backup.py`、`restore.py` 双入口脚本。
- 新增 `requirements.txt`，并使用 Python `venv` 管理依赖（`pywebio`）。
- 新增内部模块用于：
  - 封装 `adb` 命令执行（含 `adb root` 提权、设备连通性检查）。
  - 封装 WiFi 备份/恢复逻辑（读取/写入 `/data/misc/wifi/WifiConfigStore.xml`）。
  - 封装基于 PyWebIO 的交互流程与控制台日志输出。
- 备份产物：以操作时间命名的目录（位于运行目录下），目录内分类存放各备份项的数据文件与一份 `manifest.json`。
- 恢复入口：接收一个文件夹路径，读取其 `manifest.json`，让用户勾选要恢复的范围。

## Impact
- Affected specs: 项目首个 spec，无现有能力受影响。
- Affected code（待实现）：
  - `backup.py`、`restore.py`
  - `android_backup/adb.py`（adb 命令封装）
  - `android_backup/wifi.py`（WiFi 备份/恢复实现）
  - `android_backup/ui.py`（PyWebIO 交互 + 控制台日志）
  - `requirements.txt`

## ADDED Requirements

### Requirement: 项目骨架与依赖管理
The system SHALL 在仓库根目录提供 `backup.py` 与 `restore.py` 两个入口脚本，并提供 `requirements.txt` 用于通过 Python `venv` 安装依赖（至少包含 `pywebio`）。

#### Scenario: 首次运行
- **WHEN** 用户创建并激活 venv 后执行 `pip install -r requirements.txt`
- **THEN** 能够成功安装 `pywebio` 等所需依赖，并可直接运行 `python backup.py` / `python restore.py`

### Requirement: ADB 提权与连通性检查
The system SHALL 在备份与恢复流程开始前自动执行 `adb root`（视作用户已具备 root 权限的前置条件），并验证至少有一台设备处于 `device` 状态。

#### Scenario: 设备就绪
- **WHEN** 启动 `backup.py` 或 `restore.py` 且仅有一台已授权 root 的设备连接
- **THEN** 系统在控制台输出 `adb root`、`adb devices` 的执行日志，并继续后续流程

#### Scenario: 设备缺失或未授权
- **WHEN** `adb devices` 返回的可用设备数量为 0，或 `adb root` 失败
- **THEN** 系统在 PyWebIO 页面与控制台同时给出明确错误信息并终止本次操作

### Requirement: 备份范围选择与产物结构
The system SHALL 在 `backup.py` 中通过 PyWebIO 让用户从受支持的备份项列表中勾选范围（第一版仅含 `wifi` 一项，默认勾选），并在运行目录下创建以本机操作时间命名的目录（格式：`backup-YYYYMMDD-HHMMSS`）作为本次备份的根目录。

#### Scenario: 用户勾选 WiFi 并执行备份
- **WHEN** 用户在 PyWebIO 页面勾选 `wifi` 并点击开始备份
- **THEN** 系统创建形如 `./backup-20260528-101530/` 的目录，目录内包含：
  - `wifi/WifiConfigStore.xml`（从设备 `/data/misc/wifi/WifiConfigStore.xml` 拉取）
  - `manifest.json`（描述备份时间、设备序列号、包含的备份项及对应文件相对路径）

#### Scenario: 备份过程日志
- **WHEN** 备份执行
- **THEN** 关键步骤（adb 命令、文件大小、成功/失败状态）必须输出到控制台

### Requirement: 恢复范围选择与执行
The system SHALL 在 `restore.py` 中接收一个备份目录路径作为输入（命令行参数 `python restore.py <dir>`，若缺省则在 PyWebIO 页面提示用户输入），读取该目录的 `manifest.json`，向用户展示其中包含的备份项供其勾选要恢复的范围，并对勾选项执行恢复。

#### Scenario: 恢复 WiFi 密码
- **WHEN** 用户提供一个包含 `wifi/WifiConfigStore.xml` 的备份目录并勾选 `wifi`
- **THEN** 系统使用 `adb push` 将该文件回写到设备 `/data/misc/wifi/WifiConfigStore.xml`，调整其属主/权限（`chown system:system`、`chmod 600`），并提示用户重启 WiFi 或设备使配置生效

#### Scenario: 备份目录非法
- **WHEN** 用户提供的路径不存在、不是目录、或其中没有有效 `manifest.json`
- **THEN** 系统在 PyWebIO 页面与控制台报告错误并终止恢复

### Requirement: PyWebIO 交互与控制台日志
The system SHALL 使用 PyWebIO 作为唯一的用户交互界面（启动后浏览器自动打开），并将执行日志同时输出到控制台。

#### Scenario: 同步反馈
- **WHEN** 任一步骤产生日志
- **THEN** 控制台必须看到该日志；PyWebIO 页面用于展示交互问题与最终结果摘要
