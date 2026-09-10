# py-adb-android-backup

基于 ADB 的 Android 设备备份与恢复工具，提供 Web 界面操作。

## 功能特性

- **数据备份**：通过 ADB 备份 Android 设备数据（WiFi 配置、应用数据等）
- **数据恢复**：将备份数据恢复到 Android 设备
- **Root 检测**：自动检测设备 Root 状态，非 Root 模式下自动禁用需要 Root 的功能
- **Web 界面**：使用 PyWebIO 提供友好的浏览器操作界面
- **备份管理**：生成 manifest.json 记录备份元数据，便于追踪和管理

## 系统要求

- Python 3.7+
- ADB（Android Debug Bridge）已安装并添加到 PATH
- Android 设备已启用 USB 调试

## 安装

1. 克隆项目：
```bash
git clone <repository-url>
cd py-adb-android-backup
```

2. 安装依赖：
```bash
pip install -r requirements.txt
```

## 使用方法

### 备份数据

```bash
python backup.py
```

启动后会自动打开浏览器，显示 Web 界面：
1. 工具会自动检测已连接的 Android 设备
2. 检查设备 Root 状态
3. 显示可备份的数据项（非 Root 设备会禁用需要 Root 的项目）
4. 选择要备份的数据范围
5. 执行备份，结果保存在 `backup-YYYYMMDD-HHMMSS` 目录中

### 恢复数据

```bash
# 方式 1：命令行指定备份目录
python restore.py /path/to/backup-directory

# 方式 2：交互式输入
python restore.py
```

启动后会自动打开浏览器，显示 Web 界面：
1. 工具会自动检测已连接的 Android 设备
2. 读取备份目录中的 manifest.json
3. 显示可恢复的数据项
4. 选择要恢复的数据范围
5. 执行恢复操作

## 项目结构

```
py-adb-android-backup/
├── backup.py              # 备份入口脚本
├── restore.py             # 恢复入口脚本
├── requirements.txt       # Python 依赖
├── android_backup/        # 核心模块
│   ├── __init__.py
│   ├── adb.py            # ADB 工具函数
│   ├── apps.py           # 应用数据备份/恢复
│   ├── ui.py             # PyWebIO 界面工具
│   └── wifi.py           # WiFi 配置备份/恢复
└── README.md
```

## 备份目录结构

每次备份会创建一个独立的目录，包含：
- `manifest.json`：备份元数据（创建时间、设备序列号、Root 状态、备份项列表）
- 备份数据文件（根据选择的备份项而定）

## 注意事项

1. **Root 权限**：备份/恢复应用数据和 WiFi 配置需要 Root 权限
2. **USB 调试**：确保 Android 设备已启用 USB 调试并授权当前计算机
3. **ADB 版本**：建议使用较新版本的 ADB 工具
4. **备份安全**：备份文件包含敏感数据（如 WiFi 密码），请妥善保管

## 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件