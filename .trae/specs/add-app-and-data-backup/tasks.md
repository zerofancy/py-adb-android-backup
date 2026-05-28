# Tasks

- [x] Task 1: 在 `android_backup/apps.py` 中实现备份/恢复
  - [x] SubTask 1.1: 暴露 `ITEM = {"id": "apps", "label": "应用与应用数据", ...}` 元信息
  - [x] SubTask 1.2: 实现 `list_third_party_packages()`：解析 `pm list packages -3`
  - [x] SubTask 1.3: 实现 `select_packages_interactive()`：基于 `pywebio.input.checkbox` 二级勾选
  - [x] SubTask 1.4: 实现 `backup_one_package(pkg, dest_dir)`：拉 APK、tar `/data/data/<pkg>`，可选打包外置存储与 obb，写 `info.json`
  - [x] SubTask 1.5: 实现 `backup(dest_dir)`：调用 1.2/1.3/1.4，并写 `apps/manifest.json`
  - [x] SubTask 1.6: 实现 `restore_one_package(pkg, src_dir)`：必要时 `pm install`(-create/-write/-commit) → `am force-stop` → tar 解包覆盖 → `chown` + `restorecon`，按需恢复 ext/obb
  - [x] SubTask 1.7: 实现 `restore(src_dir)`：读取 `apps/manifest.json`，让用户勾选并逐包调 1.6，失败隔离

- [x] Task 2: 在 `backup.py` 中注册 `apps` 备份项
  - [x] SubTask 2.1: `import apps`
  - [x] SubTask 2.2: `SUPPORTED_ITEMS` 加入 `apps.ITEM`
  - [x] SubTask 2.3: `BACKEND_BY_ID` 加入 `apps.ITEM["id"]: apps`

- [x] Task 3: 在 `restore.py` 中注册 `apps` 备份项
  - [x] SubTask 3.1: `import apps`
  - [x] SubTask 3.2: `BACKEND_BY_ID` 加入 `apps.ITEM["id"]: apps`

- [x] Task 4: 验证
  - [x] SubTask 4.1: `python -c "import backup, restore; from android_backup import apps"` 能正常导入
  - [x] SubTask 4.2: 用日志走查 `list_third_party_packages` 解析逻辑（mock 输出）

# Task Dependencies
- Task 2、3 依赖 Task 1
- Task 4 依赖 Task 1、2、3
