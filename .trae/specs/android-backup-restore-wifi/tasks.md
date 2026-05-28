# Tasks

- [x] Task 1: 初始化项目骨架与依赖
  - [x] SubTask 1.1: 创建 `requirements.txt`，写入 `pywebio`
  - [x] SubTask 1.2: 创建 `android_backup/` 包及空的 `__init__.py`
  - [x] SubTask 1.3: 在 README 之外不创建多余文档；保持仓库根目录干净

- [x] Task 2: 实现 ADB 命令封装 `android_backup/adb.py`
  - [x] SubTask 2.1: `run_adb(args, check=True) -> CompletedProcess`，统一控制台日志
  - [x] SubTask 2.2: `ensure_root_device()`：执行 `adb root` + `adb wait-for-device` + `adb devices` 校验，失败抛出明确异常
  - [x] SubTask 2.3: `pull(remote, local)` 与 `push(local, remote)` 工具函数
  - [x] SubTask 2.4: `shell(cmd)`：通过 `adb shell` 执行命令并返回输出

- [x] Task 3: 实现 WiFi 备份/恢复逻辑 `android_backup/wifi.py`
  - [x] SubTask 3.1: `backup(dest_dir)`：将 `/data/misc/wifi/WifiConfigStore.xml` 拉取到 `dest_dir/wifi/WifiConfigStore.xml`
  - [x] SubTask 3.2: `restore(src_dir)`：将 `src_dir/wifi/WifiConfigStore.xml` 推送到设备目标路径，并修正 owner/权限
  - [x] SubTask 3.3: 暴露元数据 `ITEM = {"id": "wifi", "label": "WiFi 密码", "files": ["wifi/WifiConfigStore.xml"]}`

- [x] Task 4: 实现 UI 与日志辅助 `android_backup/ui.py`
  - [x] SubTask 4.1: 配置 logging 输出到控制台，提供 `log(msg)`
  - [x] SubTask 4.2: 提供 `select_items(items, title)` 基于 `pywebio.input.checkbox`
  - [x] SubTask 4.3: 提供 `show_result(summary)` 基于 `pywebio.output.put_*`

- [x] Task 5: 实现 `backup.py` 入口
  - [x] SubTask 5.1: 通过 `pywebio.start_server` 或 `pywebio.platform.tornado.start_server` 启动并自动打开浏览器
  - [x] SubTask 5.2: 调用 `ensure_root_device()`，让用户勾选备份项（默认 `wifi`）
  - [x] SubTask 5.3: 创建 `backup-YYYYMMDD-HHMMSS/` 目录，按勾选项调用对应模块的 `backup()`
  - [x] SubTask 5.4: 写入 `manifest.json`（含时间戳、设备 serial、items 列表、文件相对路径）
  - [x] SubTask 5.5: 在 PyWebIO 页面与控制台输出最终结果

- [x] Task 6: 实现 `restore.py` 入口
  - [x] SubTask 6.1: 接收 `sys.argv[1]` 作为备份目录；缺省时在页面让用户输入
  - [x] SubTask 6.2: 校验目录与 `manifest.json`，加载支持的项
  - [x] SubTask 6.3: 让用户勾选要恢复的项，调用对应模块的 `restore()`
  - [x] SubTask 6.4: 在 PyWebIO 页面与控制台输出最终结果与提示（如重启 WiFi）

- [x] Task 7: 手动验证（无设备时的可达性）
  - [x] SubTask 7.1: 创建并激活 venv，`pip install -r requirements.txt` 成功
  - [x] SubTask 7.2: `python backup.py` 与 `python restore.py` 至少能启动 PyWebIO 页面（无设备时给出明确报错）

# Task Dependencies
- Task 3、4 依赖 Task 2
- Task 5、6 依赖 Task 2、3、4
- Task 7 依赖 Task 1、5、6
