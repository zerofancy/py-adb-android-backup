# 新增应用与应用数据备份/恢复 Spec

## Why
当前工具只支持 WiFi 配置的备份/恢复。已 root 设备在迁移、刷机前后还有
一个高频需求：备份并恢复**第三方应用本身（APK）以及它们的应用数据**
（含 `/data/data/<pkg>` 与外置存储 `/sdcard/Android/data/<pkg>`、
`/sdcard/Android/obb/<pkg>`）。本次新增此能力，复用现有 `backup.py`/
`restore.py` 入口与 PyWebIO 交互框架。

## What Changes
- 在 `android_backup/` 中新增 `apps.py`，提供应用包的备份/恢复实现。
- 把 `apps` 注册为新的备份项（与 `wifi` 并列），出现在 `backup.py` 与
  `restore.py` 的勾选列表中。
- 备份阶段：
  - 通过 `adb shell pm list packages -3` 列出第三方应用，让用户在
    PyWebIO 页面以多选形式勾选要备份的包。
  - 对每个包：
    - 用 `pm path <pkg>` 找到所有 APK（含 split），`adb pull` 到本地。
    - 以 root tar 打包 `/data/data/<pkg>`。
    - 若存在则打包 `/sdcard/Android/data/<pkg>`。
    - 若存在则打包 `/sdcard/Android/obb/<pkg>`。
  - 在备份目录下按包名分子目录存放，并写入 `apps/manifest.json`。
- 恢复阶段：
  - 读取 `apps/manifest.json`，让用户多选要恢复的包。
  - 对每个被选中的包：
    1. 若设备上未安装该包，且备份中含 APK，则先用 `pm install`（或
       `pm install-create` + `install-write` + `install-commit` 处理 split）
       安装。
    2. `am force-stop <pkg>` 停止应用。
    3. tar 解包覆盖 `/data/data/<pkg>`（执行 `chown -R uid:uid` 与
       `restorecon -R`，uid 来源于 `dumpsys package <pkg> | grep userId`）。
    4. 若有外部存储 tar，则解包到 `/sdcard/Android/data` 或 `/sdcard/Android/obb`。
- `backup.py` / `restore.py` 中的勾选项只需扩展现有列表，无需重写交互框架。

## Impact
- Affected specs: 与 `android-backup-restore-wifi` 并列；不修改其行为。
- Affected code:
  - 新增 `android_backup/apps.py`
  - 修改 `backup.py` 中 `SUPPORTED_ITEMS` 与 `BACKEND_BY_ID` 注册
  - 修改 `restore.py` 中 `BACKEND_BY_ID` 注册
  - 备份产物结构新增 `apps/<pkg>/...` 子目录与 `apps/manifest.json`

## ADDED Requirements

### Requirement: 应用列表枚举与勾选
The system SHALL 在 `backup.py` 选择 `apps` 备份项后，调用
`pm list packages -3` 获取第三方应用列表，并通过 PyWebIO 复选框让用户
勾选要备份的包名。

#### Scenario: 用户勾选若干包
- **WHEN** 用户在 PyWebIO 页面看到第三方应用列表
- **THEN** 用户可勾选 0..N 个包；勾选 0 个时不报错，仅跳过 apps 备份并继续后续流程

### Requirement: 单个应用的备份产物
The system SHALL 对每个被选中的包写出如下结构（位于本次备份目录的
`apps/<pkg>/` 下）：
- `apk/`：包含从 `pm path <pkg>` 拉取的 base.apk 与所有 split apk
- `data.tar`：以 root 打包的 `/data/data/<pkg>` 内容
- `ext_data.tar`（可选，存在时才有）：`/sdcard/Android/data/<pkg>`
- `obb.tar`（可选，存在时才有）：`/sdcard/Android/obb/<pkg>`
- `info.json`：记录 versionName、versionCode、uid、是否含 split、tar 包列表

并在 `apps/manifest.json` 中聚合所有备份的包元信息。

#### Scenario: 备份单个 com.example.app
- **WHEN** 该应用 `pm path` 返回 base.apk + split_config.zh.apk
- **THEN** `apps/com.example.app/apk/` 下出现两个 apk；`data.tar` 存在；
  `info.json.has_apk == true`；如设备上有外部存储数据则 `ext_data.tar` 存在

### Requirement: 应用恢复流程
The system SHALL 在 `restore.py` 中支持 `apps` 项；恢复每个被选中的包时
按下列顺序执行：
1) 若设备未安装该包且备份内含 APK，则先安装（多 APK 用
   `pm install-create` + `install-write` + `install-commit`，单 APK 用
   `pm install -r`）。
2) `am force-stop <pkg>` 停止应用。
3) 在 root shell 中 tar 解包 `data.tar` 覆盖 `/data/data/<pkg>`，并
   `chown -R <uid>:<uid>`、`restorecon -R /data/data/<pkg>`，其中 uid
   通过 `dumpsys package <pkg>` 中的 userId 获取。
4) 若 `ext_data.tar`/`obb.tar` 存在则按对应目录解包。

#### Scenario: 设备未安装目标应用
- **WHEN** 备份中含 APK 且设备未安装该包
- **THEN** 系统先 `pm install` 安装；安装失败时跳过该包并记录错误，不阻塞其它包

#### Scenario: 设备已安装目标应用
- **WHEN** 备份中含 APK 但设备已安装该包
- **THEN** 系统跳过 APK 安装，直接进入数据恢复阶段

#### Scenario: 无 APK 且设备未安装
- **WHEN** 备份内不含 APK 且设备未安装该包
- **THEN** 系统跳过该包并在结果中标记“需先手动安装”

### Requirement: 失败隔离
The system SHALL 把每个包视作独立单元；任意一步失败仅影响该包，整体
流程继续处理其余包，并在最终结果中以行的形式呈现成功/失败。

## MODIFIED Requirements

### Requirement: 备份范围选择与产物结构
（基于 `android-backup-restore-wifi` 中已有同名 Requirement，扩展为）
The system SHALL 在 `backup.py` 中通过 PyWebIO 让用户从受支持的备份项
列表中勾选范围，列表至少包含 `wifi` 与 `apps` 两项；并在运行目录下创建
以本机操作时间命名的目录（格式：`backup-YYYYMMDD-HHMMSS`）作为本次
备份的根目录；勾选 `apps` 时进入二级页面让用户挑选具体包名。

### Requirement: 恢复范围选择与执行
（基于 `android-backup-restore-wifi` 中已有同名 Requirement，扩展为）
`restore.py` 在加载 `manifest.json` 时若发现 `apps` 项，应通过
`apps/manifest.json` 列出可恢复的包名让用户二级勾选；勾选项的恢复按
本规范前述步骤执行。
