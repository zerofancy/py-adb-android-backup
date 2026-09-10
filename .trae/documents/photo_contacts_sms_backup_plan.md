# 相册 / 联系人 / 短信 备份与恢复 实现计划

## 一、背景与目标

当前项目状态：
- **已支持**：应用与应用数据 备份/恢复（`android_backup/apps.py`，需 root）
- **WiFi 备份**：已按用户要求**移除**（不再支持，亦不在代码中保留相关引用）

用户提出两个方向的需求：
1. **相册（照片+视频）**备份与恢复 —— 数据量大、位于外置存储公开目录、**不需要 root**
2. **联系人**与**短信（含彩信）**备份与恢复 —— 数据量小但价值高，位于系统 Provider
   私有数据库目录，**需要 root**

本次一次性新增三个备份项（`photos`、`contacts`、`sms`），复用现有
`backup.py` / `restore.py` 的 PyWebIO 勾选框架与 manifest 机制。

---

## 二、三大模块的技术路径总览

| 模块 | id | requires_root | 备份对象 | 核心技术 |
|---|---|---|---|---|
| 相册（照片 + 视频） | `photos` | **False** | `/sdcard/DCIM/`、`/sdcard/Pictures/` 等目录下的媒体文件 | `adb pull -a` / `adb push` |
| 联系人 | `contacts` | **True** | `com.android.providers.contacts` 的 SQLite 数据库及相关文件 | root `adb pull` 整个 databases 目录，恢复时 `chown` + `restorecon` + `am force-stop` |
| 短信 + 彩信 | `sms` | **True** | `com.android.providers.telephony` 的 `mmssms.db` 与 MMS 附件目录 | root `adb pull` 数据库 + `app_parts/`，恢复流程同上 |

---

## 三、相册模块（`android_backup/photos.py`）详细设计

### 3.1 Android 相册路径调研

**主流相册目录**（`/sdcard` 即 `/storage/emulated/0` 的 symlink，adb 默认可读写）：

| 路径 | 用途 |
|---|---|
| `/sdcard/DCIM/Camera` | 系统相机（最核心） |
| `/sdcard/DCIM/<100ANDRO 等数字前缀>` | 厂商自定义相机目录（小米/索尼/三星等） |
| `/sdcard/DCIM/Screenshots` | 部分机型截屏 |
| `/sdcard/Pictures/Screenshots` | AOSP / 多数国产机型截屏 |
| `/sdcard/Pictures/WeiXin`、`Pictures/Telegram` 等 | 社交 App 保存的图片/视频 |
| `/sdcard/DCIM/<其他App名>` | OpenCamera、B612 等第三方相机 |

**扩展名白名单**（大小写不敏感）：
```
图片: .jpg .jpeg .png .gif .webp .bmp .heic .heif .tif .tiff .raw .dng .cr2 .nef .arw
视频: .mp4 .mov .3gp .mkv .avi .webm .m4v .wmv .flv
```

**扫描排除的目录**（即便含图片也不纳入）：
```
.thumbnails  .cache  Android/  data/  obb/  Music/  Ringtones/
Alarms/  Notifications/  Podcasts/  Recordings/  Audiobooks/
以及所有以 "." 开头的隐藏目录
```

### 3.2 导出 API

```python
ITEM = {
    "id": "photos",
    "label": "相册（照片与视频）",
    "files": ["photos/manifest.json", "photos/DCIM/*", "photos/Pictures/*"],
}

def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 3.3 备份目录结构

```
backup-YYYYMMDD-HHMMSS/
├── manifest.json          (顶层：新增 "photos" items 条目)
├── apps/                  (既有)
├── contacts/              (新增，见 §四)
├── sms/                   (新增，见 §五)
└── photos/
    ├── manifest.json
    ├── DCIM/
    │   ├── Camera/
    │   │   ├── IMG_20240101_120000.jpg
    │   │   └── VID_20240101_120000.mp4
    │   └── Screenshots/
    │       └── Screenshot_2024-01-01-12-00-00.png
    └── Pictures/
        └── WeiXin/
            └── mmexport1700000000000.jpg
```

`photos/manifest.json`：

```json
{
  "created_at": "2024-01-01T12:00:00",
  "source_root": "/sdcard",
  "total_files": 1234,
  "total_size_bytes": 8589934592,
  "albums": [
    {
      "remote_root": "/sdcard/DCIM/Camera",
      "local_relpath": "DCIM/Camera",
      "file_count": 800,
      "size_bytes": 6442450944
    }
  ]
}
```

### 3.4 备份流程

1. **枚举候选相册目录**：
   - `adb shell find /sdcard -maxdepth 4 -type d`，对每个目录做快速"是否包含至少一个媒体文件"检查
   - 排除清单过滤 + 父子目录去重（选了父就不再列出子目录）
   - 若 `find /sdcard` 失败（极少数机型没挂载 `/sdcard`），fallback 到 `/storage/emulated/0`

2. **PyWebIO 二级勾选**：`label = "<相对路径>  ·  N 个文件  ·  约 X GB"`，默认全选

3. **逐个 `adb pull -a` 拉取**：
   ```python
   adb run_adb(["pull", "-a", remote_dir, str(local_dir)], check=False)
   ```
   - 若 stderr 出现 `unknown option`（旧版 adb 不支持 `-a`），退化为无 `-a` 重试
   - 失败不中断，记录 `(album, error)`，最终汇总

4. **写出 `photos/manifest.json`**

### 3.5 恢复流程

1. 读 `photos/manifest.json` → 列 `albums` → 二级勾选（默认全选）
2. 对每个勾选的 album：
   - `adb shell mkdir -p /sdcard/<parent>`
   - `adb push <src>/photos/<rel> /sdcard/<rel>`
   - 推送后尝试触发媒体扫描（失败静默）：
     ```
     adb shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE
             -d file:///sdcard/<rel> -n com.android.providers.media/.MediaScannerReceiver
     ```
     或更宽松的 `MEDIA_MOUNTED`（Android 12+ 会被拒绝，忽略错误）
3. 在结果中提示：如相册未立即刷新，请重启设备或进入系统「文件管理器/相册」手动触发扫描

### 3.6 风险与处理

| 风险 | 处理 |
|---|---|
| 文件极多（数万张）→ `find` 卡死 | `-maxdepth 4` + subprocess `timeout=60`；失败时降级为"预设目录列表 + 存在性检查" |
| 大视频 4GB+ → 用户以为卡死 | `put_text("正在拉取 DCIM/Camera（预计 800 文件 / 6.1GB）…")`，逐个目录给提示 |
| `-a` 不兼容 | stderr 含 "unknown option" → 自动降级到无参 pull |
| 恢复时同名文件覆盖 | UI 里提醒用户 "adb push 会覆盖同名文件，重要内容请先备份目标设备" |

---

## 四、联系人模块（`android_backup/contacts.py`）详细设计

### 4.1 备份对象

`com.android.providers.contacts` 的数据目录：

```
/data/data/com.android.providers.contacts/
└── databases/
    ├── contacts2.db              (主库)
    ├── contacts2.db-wal          (WAL 日志，可能不存在)
    ├── contacts2.db-shm          (共享内存文件，可能不存在)
    ├── profile.db                (Android 7+，个人资料卡)
    ├── profile.db-wal / -shm
    ├── calllog.db                (部分机型通话记录独立库，可选)
    └── phone_lookup.db           (反查数据库，部分机型)
```

**策略**：整个 `databases/` 目录 tar 打包，避免漏文件。

### 4.2 导出 API

```python
ITEM = {
    "id": "contacts",
    "label": "联系人（含群组/头像/通话记录等）",
    "files": ["contacts/manifest.json", "contacts/contacts_databases.tar"],
}

def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 4.3 备份产物结构

```
backup-YYYYMMDD-HHMMSS/
└── contacts/
    ├── manifest.json
    └── contacts_databases.tar     (tar 包内是 databases/ 的所有文件)
```

`contacts/manifest.json`：
```json
{
  "created_at": "2024-01-01T12:00:00",
  "source_package": "com.android.providers.contacts",
  "android_version": "13",
  "android_sdk_int": 33,
  "build_fingerprint": "Xiaomi/lisa/lisa:13/TKQ1.221114.001/V14.0.6.0.TKOCNXM:user/release-keys",
  "tar_file": "contacts_databases.tar",
  "tar_size_bytes": 25165824,
  "db_schemas": {
    "contacts2.db": 2104,
    "profile.db": 1501
  }
}
```

### 4.4 备份流程（需 root）

1. 校验 `adb.has_root_access()`，若 False 直接抛错告知需要 root
2. 读取设备 Android 版本信息用于兼容性提示：
   ```
   adb shell getprop ro.build.version.release        # android_version
   adb shell getprop ro.build.version.sdk            # sdk_int
   adb shell getprop ro.build.fingerprint            # fingerprint
   ```
3. 用 `apps.py` 中已有的 `_tar_remote_dir`（或等价函数）打包：
   ```
   tar -cpf /data/local/tmp/contacts_bak_<ts>.tar
       -C /data/data/com.android.providers.contacts databases
   ```
4. `adb pull` 到 `dest_dir/contacts/contacts_databases.tar`，删除远端临时 tar
5. 对每个 db 文件读取 schema version（`PRAGMA user_version;`，可通过 sqlite3
   或 python 内置 sqlite3 在 tar 解包的临时文件上读取），写入 manifest
6. 写 manifest

### 4.5 恢复流程（需 root）

1. 读 `contacts/manifest.json`，显示源设备 `android_version` 与目标设备当前
   `ro.build.version.release`；若主版本不匹配，弹 `ui.show_warning`
   告知兼容性风险，让用户勾选确认是否继续（二级确认页面："我已了解跨版本恢复可能导致联系人 App 崩溃，仍要继续"）
2. **保护目标设备数据**：恢复前先把目标设备上现有的 contacts databases 打包一份
   拉回到本地备份目录的 `contacts/_pre_restore_backup_<ts>.tar`，方便用户回滚
3. 推送 tar 到 `/data/local/tmp/`
4. `am force-stop com.android.providers.contacts`（停止 Provider 进程释放锁）
5. 清库 + 解包：
   ```
   cd /data/data/com.android.providers.contacts
   rm -rf databases   # 或先 mv 到 databases.bak_<ts> 做安全冗余
   tar -xpf /data/local/tmp/..._contacts_databases.tar
   ```
6. **权限与 SELinux 修复**（核心，否则 Provider 启动失败）：
   ```
   uid=$(stat -c %u /data/data/com.android.providers.contacts)   # 读目录 owner
   find databases -exec chown $uid:$uid {} +
   chmod 0771 databases                   # 与出厂权限一致
   find databases -type f -exec chmod 0660 {} +
   restorecon -RF databases
   restorecon -RF databases               # 双跑，确保 MLS 类别打全
   ```
7. `ls -ldZ databases/` 打印日志供排查
8. 重启 Provider：
   ```
   am force-stop com.android.providers.contacts    # 再次确保被杀
   # Provider 下次被访问时会由 AMS 自动启动，无需手动启动
   ```
9. 在结果里提示："请打开系统「联系人」应用，若崩溃请手动清空联系人 App 数据后重试，
   或使用 contacts/_pre_restore_backup_<ts>.tar 回滚"

---

## 五、短信 / 彩信 模块（`android_backup/sms.py`）详细设计

### 5.1 备份对象

`com.android.providers.telephony` 的数据库与彩信附件目录：

```
/data/data/com.android.providers.telephony/
├── databases/
│   ├── mmssms.db              (主库：短信 + 彩信元信息 + 会话)
│   ├── mmssms.db-wal / -shm
│   └── telephony.db           (carrier 配置等，可选备份)
└── app_parts/                 (彩信附件二进制，非常关键)
    ├── PART_12345_1
    └── ...
```

> 部分厂商 ROM（如华为信息、小米短信）会用自包名 Provider 代替，
> 本模块仅备份**系统标准 telephony provider**。其它厂商数据需要手动导出
> 或在后续版本扩展包名列表。

### 5.2 导出 API

```python
ITEM = {
    "id": "sms",
    "label": "短信与彩信（含会话、附件）",
    "files": ["sms/manifest.json", "sms/mmssms.tar", "sms/app_parts.tar"],
}

def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 5.3 备份产物

```
backup-YYYYMMDD-HHMMSS/
└── sms/
    ├── manifest.json
    ├── mmssms_databases.tar    (databases/ 目录下所有 db)
    └── app_parts.tar           (app_parts/ 目录，若存在)
```

`sms/manifest.json` 字段同 contacts：含源 Android 版本、sdk、fingerprint、
schema version、tar 大小等。

### 5.4 备份流程（需 root）

与 contacts 完全相同的框架，只是路径换成：
- source: `/data/data/com.android.providers.telephony/`
- 分别 tar `databases` 和 `app_parts` 两个子目录（若 `app_parts` 不存在则跳过，
  `manifest.includes_app_parts = false`）

### 5.5 恢复流程（需 root）

步骤与 contacts 恢复对应：
1. 读 manifest → 对比源 / 目标 Android 版本并警告
2. **预备份目标设备现有短信库**到 `sms/_pre_restore_backup_<ts>/`
3. `am force-stop com.android.providers.telephony` + `am force-stop com.android.mms`
   （以及常见厂商短信 App：`com.google.android.apps.messaging`、`com.miui.smsextra`
   等，force-stop 失败静默）
4. 推送 `mmssms_databases.tar`，解包覆盖 `databases/`；`app_parts.tar` 存在则同步
5. `chown/chmod/restorecon -RF`（uid 来自 `stat /data/data/com.android.providers.telephony`）
6. `am force-stop` 杀掉进程让 Provider 重启
7. 提示：短信会话 ID/thread_id 在跨设备恢复后，可能需要接收/发送一条新短信才能
   让系统短信 App 正确合并显示；如果彩信附件不显示请检查 app_parts 权限

---

## 六、顶层注册与入口改动

### 6.1 `backup.py` 改动

```python
from android_backup import adb, apps, ui, photos, contacts, sms

SUPPORTED_ITEMS: List[Dict[str, Any]] = [
    {**photos.ITEM,   "requires_root": False},   # 相册：无需 root
    {**contacts.ITEM, "requires_root": True},    # 联系人：需 root
    {**sms.ITEM,      "requires_root": True},    # 短信：需 root
    {**apps.ITEM,     "requires_root": True},    # 应用：已有
]
ITEM_BY_ID = {it["id"]: it for it in SUPPORTED_ITEMS}
BACKEND_BY_ID = {
    photos.ITEM["id"]:   photos,
    contacts.ITEM["id"]: contacts,
    sms.ITEM["id"]:      sms,
    apps.ITEM["id"]:     apps,
}
```

同时**删除** backup.py 中所有关于 WiFi 的注释/文字（目前仅 §背景注释与 spec 引用提到，
已替换为 "与 apps/contacts/sms/photos 并列" 等新文字）。

### 6.2 `restore.py` 改动

```python
from android_backup import adb, apps, ui, photos, contacts, sms

ITEM_REQUIRES_ROOT: Dict[str, bool] = {
    photos.ITEM["id"]:   False,
    contacts.ITEM["id"]: True,
    sms.ITEM["id"]:      True,
    apps.ITEM["id"]:     True,
}
BACKEND_BY_ID = {
    photos.ITEM["id"]:   photos,
    contacts.ITEM["id"]: contacts,
    sms.ITEM["id"]:      sms,
    apps.ITEM["id"]:     apps,
}
```

同样**删除所有 WiFi 相关注释与描述**。

### 6.3 `android_backup/adb.py`（可选轻度增强）

在 `adb.py` 增加两个辅助函数（或直接在 photos/contacts/sms 内用现有
`run_adb`/`shell` 实现，不强求）：

```python
def getprop(name: str) -> str:
    """读取 Android 属性，返回去空白的 stdout；失败返回空串。"""
    proc = run_adb(["shell", f"getprop {name}"], check=False, capture=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""

def pull_dir(remote_dir: str, local_dir: Path,
             preserve_timestamp: bool = True) -> None:
    """带 -a 兼容性降级的目录拉取。"""
```

---

## 七、完整改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `android_backup/photos.py` | **新增** | 相册目录扫描 + 勾选 + `adb pull -a`/`push` + manifest |
| `android_backup/contacts.py` | **新增** | root tar 备份 `com.android.providers.contacts/databases`，恢复时 chown+restorecon+force-stop，版本差异警告，目标库预备份 |
| `android_backup/sms.py` | **新增** | 同 contacts，目标为 `com.android.providers.telephony` 的 databases + app_parts |
| `backup.py` | 修改 | 注册 `photos`(无root)、`contacts`(root)、`sms`(root) 三项；移除 WiFi 相关文字 |
| `restore.py` | 修改 | 同上三项注册 + manifest 兼容；移除 WiFi 相关文字 |
| `android_backup/adb.py` | 修改（轻度可选） | 可补 `getprop` / `pull_dir` 辅助 |
| `apps.py`、`ui.py`、`requirements.txt` | **不改动** | 无新依赖 |
| `.trae/specs/android-backup-restore-wifi/` | 暂不处理 | 规格文档不影响运行，若用户希望删除可在后续单独清理，本次不纳入改动 |

---

## 八、风险与边界

| 风险 | 影响 | 处理 |
|---|---|---|
| 跨 Android 大版本恢复联系人/短信 DB → Provider 崩溃 | 严重 | manifest 记录源版本 + 目标版本对比；UI 强警告并二次确认；恢复前预备份目标库 |
| 厂商 ROM 短信 App 不用 com.android.providers.telephony → 备份为空 | 数据漏备份 | 在联系人/短信模块备份前先用 `pm path` 检查包名；若不存在，提示用户 "当前 ROM 的短信 provider 不是标准包名，本模块备份的是系统 telephony 库，厂商私有数据请手动导出" |
| 彩信 app_parts 目录体积大（数 GB） | 备份慢 | 备份前 `du -sh app_parts` 显示并提示用户；失败不中断整体 |
| `adb pull -a` 在某些 adb 版本会报错 "can't create" 等 | 备份失败 | 检测 stderr 关键句并降级为无 `-a` 重试；记录日志 |
| 恢复后 contacts/sms provider 不自动重启 | 数据不生效 | `force-stop` 后 5s 再尝试 `cmd activity start-foreground-service` 或直接让用户打开联系人/短信 App 触发 |
| `restorecon` 命令不存在 / toybox restorecon 参数不同 | 权限错误 | 失败不抛异常，打印 warning + `ls -ldZ` 让用户自行排查 |

---

## 九、执行顺序（Todo List）

### Phase A — 公共改动（入口注册 + 辅助函数）
1. 修改 `backup.py`：三个新模块 import + SUPPORTED_ITEMS/ITEM_BY_ID/BACKEND_BY_ID 注册
2. 修改 `restore.py`：三个新模块 import + ITEM_REQUIRES_ROOT/BACKEND_BY_ID 注册
3. （可选）`adb.py` 新增 `getprop`、`pull_dir` 辅助

### Phase B — 相册模块
4. 新建 `android_backup/photos.py`：
   - `ITEM` 定义
   - `_is_media_file()`、`_scan_album_dirs()`、`_estimate_dir_stats()`、`_human_size()`
   - `select_albums_interactive()`
   - `backup(dest_dir)`
   - `restore(src_dir)`

### Phase C — 联系人模块
5. 新建 `android_backup/contacts.py`：
   - `ITEM` 定义
   - `_get_device_info()`（读 getprop 三件套）
   - `_backup_provider_dir()`（通用 tar 打包函数，可与 sms.py 共享）
   - `backup(dest_dir)`
   - `_pre_restore_target_backup()`
   - `_restore_provider_dir()`（通用：force-stop → 解包 → chown → restorecon → force-stop）
   - `restore(src_dir)` 含版本警告与二次确认

### Phase D — 短信模块
6. 新建 `android_backup/sms.py`：结构同 contacts，区别是额外处理 app_parts
   - 共享 `_backup_provider_dir`/`_restore_provider_dir`（可抽成 `_provider_utils`
     内嵌函数，或直接在模块间重复一份简单实现以避免过度抽象）

### Phase E — 自测
7. 真机场景逐一验证：
   - 相册：备份 DCIM/Screenshots 几个文件 → 格式化/清空设备 → 恢复 → 打开相册确认显示
   - 联系人：新建几个联系人 → 备份 → 删除本地联系人 → 恢复 → 打开联系人 App 核对
   - 短信：发送几条短信+彩信 → 备份 → 删除会话 → 恢复 → 打开短信 App 核对
   - 错误注入：拉取中途拔 USB → 看失败隔离是否正确；目标版本不一致 → 看警告是否生效
   - 预备份回滚验证：恢复失败时 `_pre_restore_backup_*.tar` 是否可用于手动还原
   - 无 root 场景：勾选列表中 contacts/sms 被禁用，photos 正常可用
