# Checklist

- [x] 仓库根目录存在 `backup.py`、`restore.py`、`requirements.txt`，且 `requirements.txt` 至少包含 `pywebio`
- [x] `android_backup/adb.py` 提供统一的 adb 命令封装，包含 `adb root` 与设备就绪检查，并将命令与结果输出到控制台
- [x] `android_backup/wifi.py` 实现从 `/data/misc/wifi/WifiConfigStore.xml` 备份/恢复，恢复后正确设置文件 owner 与权限
- [x] `backup.py` 启动后通过 PyWebIO 让用户勾选备份项，默认包含 `wifi`，并在当前目录创建 `backup-YYYYMMDD-HHMMSS/` 目录
- [x] 备份目录中包含对应数据文件以及一份描述本次备份的 `manifest.json`
- [x] `restore.py` 支持以 `python restore.py <dir>` 形式传入目录，或在页面输入；能解析 `manifest.json` 并展示可恢复项供用户勾选
- [x] 缺设备/未授权 root 时，`backup.py` 与 `restore.py` 都会在 PyWebIO 页面与控制台给出明确错误并终止
- [x] 全部用户交互通过 PyWebIO 进行，执行日志同步打印到控制台
- [x] 在 venv 中执行 `pip install -r requirements.txt` 成功，且 `python backup.py` / `python restore.py` 能正常启动页面
