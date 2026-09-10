# 相册备份/恢复功能 实现计划

## 一、背景与目标

当前项目已支持：
- **应用与应用数据** 备份/恢复（`apps.py`，需 root）
- WiFi 配置备份/恢复（历史规格，不在当前代码中，待后续补回或移除）

用户高频需求：刷机/换机前后，需要备份和恢复**手机相册（照片 + 视频）**。
相册文件位于 Android 外置存储公开目录（`/sdcard/DCIM/`、`/sdcard/Pictures/` 等），
通常**不需要 root** 即可通过 `adb pull/push` 访问，是入门级用户也能用的功能。

本次新增：
- 一个新的 `photos` 备份项（与 `apps` 并列）
- 支持**二级勾选**：按相册目录（如 `DCIM/Camera`、`Pictures/Screenshots`）选择性备份/恢复
- 可选（有 root 时）：备份 MediaStore 数据库，保留相册分类、收藏、标签等元信息
- 备份时保留文件时间戳与目录结构；恢复时写回原路径

---

## 二、Android 相册文件系统路径调研

### 2.1 主流相册目录（无需 root，adb 默认可读写）

| 路径 | 用途 | 备注 |
|---|---|---|
| `/sdcard/DCIM/Camera` | 系统相机拍摄的照片/视频 | 最核心目录 |
| `/sdcard/DCIM/100ANDRO` | 部分厂商（小米/索尼等）相机目录 | 数字前缀可变 |
| `/sdcard/DCIM/Screenshots` | 部分机型截屏目录 | 也可能在 Pictures 下 |
| `/sdcard/DCIM/*` | 其它相机应用（OpenCamera、B612 等） |  |
| `/sdcard/Pictures/Screenshots` | AOSP/多数国产机型截屏 |  |
| `/sdcard/Pictures/*` | 微信/QQ/Telegram 等保存的图片 | `Pictures/WeiXin` 等 |
| `/sdcard/Download/` | 下载目录（含图片） | 可选纳入 |
| `/sdcard/Movies/` | 部分应用保存的视频 | 可选纳入 |

> **策略**：枚举 `/sdcard/` 下所有子目录，筛选出 **"包含图片/视频文件"** 且
> **不属于排除清单**（`.thumbnails`、`.cache`、`Android/`、`data/`、`obb/`、`Music/`）
> 的目录，让用户勾选。

### 2.2 图片/视频扩展名白名单

```
图片: .jpg .jpeg .png .gif .webp .bmp .heic .heif .tif .tiff .raw .dng .cr2 .nef .arw
视频: .mp4 .mov .3gp .mkv .avi .webm .m4v .wmv .flv
```

### 2.3 MediaStore 数据库（需 root，可选备份）

路径示例（因厂商/Android 版本而异）：
- `/data/data/com.android.providers.media/databases/external.db`（Android 10 前）
- `/data/data/com.android.providers.media/databases/media_store*`（Android 11+）
- 部分机型为 `/data/data/com.miui.gallery/databases/` 等厂商相册应用私有库

> 备份恢复 MediaStore DB 兼容性差，**本期不作为必选项**，仅在
> `requires_root + 可选勾选` 的前提下尝试备份主 external.db。
> 恢复时不主动覆盖（避免破坏新设备数据库），只导出为独立文件供手动参考。

---

## 三、设计方案

### 3.1 新增模块：`android_backup/photos.py`

遵循 `apps.py` 的后端模块约定，导出：

```python
ITEM = {
    "id": "photos",
    "label": "相册（照片与视频）",
    "files": ["photos/manifest.json", "photos/DCIM/*", "photos/Pictures/*", ...],
}
```

并提供两个入口函数：

```python
def backup(dest_dir: Path) -> None:
    """执行相册备份到 dest_dir/photos/"""

def restore(src_dir: Path) -> None:
    """从 src_dir/photos/ 执行相册恢复"""
```

### 3.2 备份目录结构

```
backup-YYYYMMDD-HHMMSS/
├── manifest.json          (已存在，新增 "photos" items 条目)
├── apps/                  (已存在)
│   └── manifest.json
└── photos/
    ├── manifest.json      ← 新增：本模块的聚合元信息
    ├── DCIM/
    │   ├── Camera/
    │   │   ├── IMG_20240101_120000.jpg
    │   │   └── VID_20240101_120000.mp4
    │   └── Screenshots/
    │       └── Screenshot_2024-01-01-12-00-00.png
    ├── Pictures/
    │   └── WeiXin/
    │       └── mmexport1700000000000.jpg
    └── _mediastore/           (仅 root + 用户勾选时存在)
        └── external.db        (原始数据库文件，或 gz 压缩)
```

`photos/manifest.json` 内容：

```json
{
  "created_at": "2024-01-01T12:00:00",
  "total_files": 1234,
  "total_size_bytes": 8589934592,
  "includes_mediastore": true,
  "albums": [
    {
      "remote_root": "/sdcard/DCIM/Camera",
      "local_relpath": "DCIM/Camera",
      "file_count": 800,
      "size_bytes": 6442450944
    },
    {
      "remote_root": "/sdcard/Pictures/WeiXin",
      "local_relpath": "Pictures/WeiXin",
      "file_count": 434,
      "size_bytes": 2147483648
    }
  ]
}
```

### 3.3 备份流程（`photos.backup()`）

1. **枚举候选相册目录**
   - 用 `adb shell find /sdcard -type d`（加 `-maxdepth 4` 避免过深扫描，超时保护）
     或更稳妥地分段 `ls -R` / `find ... -maxdepth N`
   - 对每个目录，检查是否**至少有 1 个图片/视频文件**（扩展名白名单匹配，不区分大小写）
   - 排除：`.thumbnails`、`.cache`、`Android/`、`data/`、`obb/`、`Music/`、`Ringtones/`、`Alarms/`、`Notifications/`、`Podcasts/`、隐藏目录（`.` 开头）
   - 路径去重：优先保留父目录（已选父目录则不再列出子目录），或允许独立选择两者

2. **PyWebIO 二级勾选**（复用 `ui.select_items`）
   - 每个选项 `id = <相对 /sdcard 的路径>`，如 `DCIM/Camera`
   - `label` 显示路径 + 预估文件数 + 预估大小（`du -sh` 或求和）
   - 默认全选

3. **拉取每个被勾选目录**
   - 对 `<album>`：
     ```
     adb pull -a /sdcard/<album>  <dest_dir>/photos/<album>
     ```
     `-a` 保留文件时间戳（`adb pull` 的 `-a`/`--preserve-timestamp` 参数，
     若旧版 adb 不支持则退化为无参）
   - 计数并汇总文件数和总字节
   - 单个文件/目录拉取失败**不中断整体**，记录错误并继续

4. **可选：MediaStore 数据库备份（需 root）**
   - 仅当 `adb.has_root_access() == True` 时在勾选列表中额外给出一个
     "备份 MediaStore 元数据数据库"勾选项（默认不勾选，因为兼容性风险高）
   - 若勾选，尝试以 root 身份 `adb pull` 以下候选路径，找到的都复制到
     `photos/_mediastore/` 下并重命名带源路径 hash：
     - `/data/data/com.android.providers.media/databases/external.db`
     - `/data/data/com.android.providers.media/databases/external.db-wal`
     - `/data/data/com.android.providers.media/databases/external.db-shm`

5. **写出 `photos/manifest.json`**

### 3.4 恢复流程（`photos.restore()`）

1. **读取 `photos/manifest.json`**，列出所有 `albums` 条目
2. **PyWebIO 二级勾选**要恢复的相册（默认全选）
3. **推送每个被勾选相册**
   - 对 `local_relpath` 对应本地目录 `<src_dir>/photos/<rel>`：
     ```
     adb push <src_dir>/photos/<rel> /sdcard/<rel>
     ```
   - 注意：`/sdcard/<rel>` 父目录可能不存在，先 `adb shell mkdir -p <parent>`
   - 推送后可选执行 `adb shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/<rel>`
     或 `adb shell am broadcast ... MEDIA_MOUNTED` 触发媒体库刷新（不同版本兼容性不同，失败不报错）
   - 单相册失败**不中断整体**，结果表中逐行显示

4. **MediaStore 数据库恢复**（不自动写入）
   - 若备份内含 `_mediastore/`，在结果中提示用户：
     > MediaStore 数据库已备份在 `photos/_mediastore/`，由于不同设备/版本兼容性差异，
     > 本工具不自动覆盖系统数据库。请在需要时参考手动恢复步骤说明。

### 3.5 在 `backup.py` 注册

```python
# 新增 import
from android_backup import photos

SUPPORTED_ITEMS: List[Dict[str, Any]] = [
    {**photos.ITEM, "requires_root": False},   # 相册不需要 root
    {**apps.ITEM, "requires_root": True},
]
# 同步更新 ITEM_BY_ID / BACKEND_BY_ID
```

### 3.6 在 `restore.py` 注册

```python
from android_backup import photos

ITEM_REQUIRES_ROOT: Dict[str, bool] = {
    photos.ITEM["id"]: False,
    apps.ITEM["id"]: True,
}
BACKEND_BY_ID = {photos.ITEM["id"]: photos, apps.ITEM["id"]: apps}
```

### 3.7 `adb.py` 辅助增强（可选）

目前已有 `pull` / `push`，为相册功能可能需要补充：

```python
def pull_dir(remote_dir: str, local_dir: Path, preserve_timestamp: bool = True) -> None:
    """拉取整个目录，默认保留时间戳。内部 adb pull -a / pull。"""

def list_directory_contents(remote_dir: str, maxdepth: int = 3) -> List[str]:
    """列出目录，内部封装 find / ls，避免结果过大时卡死。"""
```

如不新增，也可直接在 `photos.py` 内用 `run_adb(["pull", "-a", ...])` 实现。

---

## 四、改动清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `android_backup/photos.py` | **新增** | 相册备份/恢复核心模块：目录枚举、勾选、pull/push、manifest 读写 |
| `backup.py` | 修改 | 注册 `photos` 项到 `SUPPORTED_ITEMS`、`ITEM_BY_ID`、`BACKEND_BY_ID` |
| `restore.py` | 修改 | 注册 `photos` 项到 `ITEM_REQUIRES_ROOT`、`BACKEND_BY_ID` |
| `android_backup/adb.py` | 修改（轻度） | 可选：新增 `pull_dir` 或 `list_directory_contents` 辅助（也可直接在 photos.py 内实现） |

**不改动**：`apps.py`、`ui.py`、`requirements.txt`（无新依赖）

---

## 五、关键风险与处理

| 风险 | 影响 | 处理方案 |
|---|---|---|
| 相册目录极深 + 文件极多（数万张）→ `find` 超时或 adb 断开 | 扫描卡死 | `find` 加 `-maxdepth 4`；加 subprocess timeout（如 60s）；失败降级为手动输入路径 |
| 视频单个文件 4GB+ → adb pull 极慢、用户以为卡死 | 体验差 | 在 UI 中用 `put_text` 打印当前正在传输的目录名；不做细粒度进度（adb 不输出百分比），但打印 "开始拉取 /sdcard/DCIM/Camera … 预计 N 个文件 / X GB" |
| 同一文件在两端都存在时 `adb push` 覆盖策略 | 误覆盖 | `adb push` 默认覆盖，符合恢复预期；在恢复 UI 说明文字里提醒 "会覆盖同名文件，请确认后继续" |
| 不同设备 adb 版本差异：`-a` 参数不存在 | 拉取失败但不报错，仅时间戳不保留 | `run_adb(["pull", "-a", ...])` 后若 stderr 包含 "unknown option"，退化为 `pull` 无参重试 |
| `/sdcard` 实际是 `/storage/emulated/0` 的 symlink | 路径不一致 | 统一用 `/sdcard`，adb 内部会解析；若 `find /sdcard` 失败则 fallback 到 `/storage/emulated/0` |
| MediaStore 广播在 Android 12+ 不生效（不允许三方发 MEDIA_MOUNTED） | 恢复后相册看不到图 | 改用针对单文件的 `MEDIA_SCANNER_SCAN_FILE` 或直接 `am broadcast ...` 发完即忘；同时在结果中提示用户 "如相册未刷新，请重启设备或在系统相册中手动触发扫描" |

---

## 六、执行顺序（Todo List）

1. **新建 `android_backup/photos.py`**
   - 定义 `ITEM`
   - 实现辅助函数：`_is_media_file(filename)`、`_scan_album_dirs()`、`_human_size()`、
     `_estimate_dir_stats(remote_dir)`
   - 实现 `select_albums_interactive(albums)` 二级勾选
   - 实现 `backup(dest_dir)`：枚举→勾选→逐个 pull→写 manifest
   - 实现 `restore(src_dir)`：读 manifest→勾选→逐个 push→触发媒体扫描

2. **修改 `backup.py`** 注册 photos 项（requires_root=False）

3. **修改 `restore.py`** 注册 photos 项（requires_root=False）

4. **（可选）轻度修改 `android_backup/adb.py`** 增加目录拉取/列举辅助（若 photos.py
   内直接用 `run_adb` 实现足够简洁，可跳过）

5. **自测**：对有真实设备的场景，备份少量截图目录并恢复，验证文件完整性、
   时间戳保留、结果表正确；错误注入（拔掉 USB、中断 adbd）验证失败隔离
