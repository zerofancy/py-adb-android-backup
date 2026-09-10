# 相册 / 联系人 / 短信 备份与恢复 + 实时进度反馈 实现计划

## 一、背景与目标

当前项目状态：
- **已支持**：应用与应用数据 备份/恢复（`android_backup/apps.py`，需 root）
- **WiFi 备份**：已按用户要求**移除**（不再支持，亦不在代码中保留相关引用）

用户需求（合并两次反馈）：
1. 新增三个备份项：**相册 / 联系人 / 短信**（参见上一版计划）
2. **过程反馈优化**：备份/恢复过程中每完成一个条目（一个相册目录、一个应用包、
   一个数据库阶段等），**立刻在网页上追加输出一条记录**，而不是等所有项跑完
   才统一出一张结果表 —— 让用户对长时间任务的进度有可感知的把握。

本计划把三条新模块 + 实时进度反馈机制合并为一份完整 spec，统一执行。

---

## 二、三大模块的技术路径总览

| 模块 | id | requires_root | 备份对象 | 核心技术 |
|---|---|---|---|---|
| 相册（照片 + 视频） | `photos` | **False** | `/sdcard/DCIM/`、`/sdcard/Pictures/` 等目录下的媒体文件 | `adb pull -a` / `adb push` |
| 联系人 | `contacts` | **True** | `com.android.providers.contacts` 的 SQLite DB 及相关文件 | root tar 打包 databases 目录；恢复时 chown+restorecon+force-stop |
| 短信 + 彩信 | `sms` | **True** | `com.android.providers.telephony` 的 mmssms.db + app_parts 附件 | 同上，含两个子目录 |
| 应用与应用数据（既有） | `apps` | True | 第三方 APK + /data/data + 外部存储 | 既有实现，只补进度输出 |

---

## 三、实时进度反馈机制（全局，影响所有模块）

### 3.1 设计原则

- **增量追加**：每完成一个"原子条目"（一个应用包、一个相册子目录、
  短信 DB 备份的一个阶段）就立即写一行，不等整体结束
- **结构化**：开始用灰色 `⏱`、成功用绿色 `✅`、失败用红色 `❌`
  （通过 put_html 小色块或 emoji + 文案色区分）
- **最终仍然汇总**：过程的逐条输出结束后，原 `show_result()` 汇总表依然保留，
  方便用户一眼核对
- **失败不中断**：单条目失败后依然立刻输出 ❌ 行，然后继续后续条目

### 3.2 在 `android_backup/ui.py` 新增统一 Helper

为避免每个模块重复写 HTML，在 `ui.py` 增加三个函数与一个 context manager：

```python
from pywebio.output import use_scope, clear, put_html

_PROGRESS_SCOPE = "progress_stream"

def begin_progress_section(title: str = "执行进度") -> None:
    """在当前页面开启一块"实时进度"区域，后续进度都追加到这里。"""
    put_markdown(f"## {title}")
    # 先用一个空 scope 占位；后续 put_* 会自动追加
    with use_scope(_PROGRESS_SCOPE, clear=True):
        pass

def progress_start(item: str, note: str = "") -> None:
    """标记一个条目开始执行（灰色提示行）。"""
    safe_item = _html_escape(item)
    safe_note = _html_escape(note)
    suffix = f" <span style='color:#999;'>— {safe_note}</span>" if safe_note else ""
    with use_scope(_PROGRESS_SCOPE):
        put_html(
            f"<div style='padding:4px 2px;color:#888;font-size:14px;'>"
            f"⏱ 开始 <b>{safe_item}</b>{suffix}</div>"
        )

def progress_done(item: str, ok: bool, detail: str = "") -> None:
    """标记一个条目结束；ok=True 绿色 ✅，ok=False 红色 ❌。"""
    safe_item = _html_escape(item)
    safe_detail = _html_escape(detail)
    if ok:
        color, emoji = "#16a34a", "✅"
    else:
        color, emoji = "#dc2626", "❌"
    suffix = f" <span style='color:#666;'>— {safe_detail}</span>" if safe_detail else ""
    with use_scope(_PROGRESS_SCOPE):
        put_html(
            f"<div style='padding:4px 2px;color:{color};font-size:14px;'>"
            f"{emoji} <span style='color:#111;'>{safe_item}</span>: 完成{suffix}</div>"
        )

def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
```

> 注：PyWebIO 的 `use_scope(name)` 默认是**追加**模式；首次调用会创建 scope，
> 所以 `progress_start` / `progress_done` 每次都用 `with use_scope(NAME)` 即可在
> 末尾追加新行，不需要额外管理。

### 3.3 在顶层 backup.py / restore.py 中接入

在进入主循环前调用 `begin_progress_section()`。每个顶层 item（photos、
contacts、sms、apps）的开始/结束调用 `progress_start` / `progress_done`：

```python
# backup.py 的步骤 6) 改造前
rows = []
items_meta = []
for item_id in selected:
    ...
    backend.backup(backup_dir)       # 内部自己会按子条目输出进度
    rows.append(...)

# 改造后
ui.begin_progress_section()
rows = []
items_meta = []
for item_id in selected:
    item = ITEM_BY_ID[item_id]
    backend = BACKEND_BY_ID[item_id]
    ui.progress_start(item["label"])      # ⏱ 开始 应用与应用数据
    try:
        backend.backup(backup_dir)        # 内部会继续逐行输出子进度
        detail = ", ".join(item["files"])
        rows.append([item["label"], "✅ 成功", detail])
        items_meta.append({...})
        ui.progress_done(item["label"], True, detail)
    except Exception as exc:
        rows.append([item["label"], "❌ 失败", str(exc)])
        ui.progress_done(item["label"], False, str(exc))

# 最后仍保留汇总表
ui.show_result("备份结果汇总", rows)
```

restore.py 按同样模式改造。

### 3.4 各 backend 内部按子粒度输出进度

除了顶层的「开始应用与应用数据 / 完成」之外，各模块内部还要在**更小的粒度**
追加进度行。具体到每个模块的粒度见 §四～§六。

---

## 四、相册模块（`android_backup/photos.py`）详细设计

### 4.1 导出 API

```python
ITEM = {
    "id": "photos",
    "label": "相册（照片与视频）",
    "files": ["photos/manifest.json", "photos/DCIM/*", "photos/Pictures/*"],
}
def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 4.2 扫描与扩展名

候选目录扫描策略不变（`find /sdcard -maxdepth 4 -type d` + 扩展名白名单过滤 +
排除清单），与上一版计划完全相同。

扩展名白名单：
```
图片: .jpg .jpeg .png .gif .webp .bmp .heic .heif .tif .tiff .raw .dng .cr2 .nef .arw
视频: .mp4 .mov .3gp .mkv .avi .webm .m4v .wmv .flv
```

排除清单目录：`.thumbnails`、`.cache`、`Android/`、`data/`、`obb/`、`Music/`、
`Ringtones/`、`Alarms/`、`Notifications/`、`Podcasts/`、Recordings/、
所有`.`开头的隐藏目录。

### 4.3 备份目录结构（与上一版相同）

```
photos/
├── manifest.json
├── DCIM/
│   ├── Camera/
│   └── Screenshots/
└── Pictures/
    └── WeiXin/
```

`photos/manifest.json`：字段保持 `created_at / source_root / total_files /
total_size_bytes / albums[{remote_root, local_relpath, file_count, size_bytes}]`。

### 4.4 备份流程（含进度输出）

1. 枚举候选目录 → 二级勾选
2. 逐个 album 拉取：
   ```python
   for rel_path in selected_albums:
       remote = f"/sdcard/{rel_path}"
       local  = dest_dir / "photos" / rel_path
       stats  = _estimate_dir_stats(remote)         # 预估值
       note = f"约 {stats['files']} 个文件 / {_human_size(stats['size'])}"
       ui.progress_start(f"相册 {rel_path}", note)
       try:
           _pull_with_timestamp_fallback(remote, local)
           real_files, real_size = _count_files_and_size(local)
           detail = f"{real_files} 个文件, {_human_size(real_size)}"
           ui.progress_done(f"相册 {rel_path}", True, detail)
       except Exception as exc:
           ui.progress_done(f"相册 {rel_path}", False, str(exc))
   ```
3. 全部拉完后汇总写 manifest；顶层 `backup.py` 随后会再次把 photos 作为一个条目
   输出 "完成"，但由于已经在模块内按相册输出过，顶层只需要显示 "完成 · 共 N 个相册目录"。

### 4.5 恢复流程（含进度输出）

1. 读 manifest → 二级勾选
2. 逐个 album 推送：
   ```python
   for entry in selected_albums_entries:
       rel = entry["local_relpath"]
       size_note = f"{entry['file_count']} 文件 / {_human_size(entry['size_bytes'])}"
       ui.progress_start(f"恢复相册 {rel}", size_note)
       try:
           adb.shell(f"mkdir -p /sdcard/{os.path.dirname(rel)}", check=False)
           adb.run_adb(["push", str(src_dir/"photos"/rel), f"/sdcard/{rel}"], check=True)
           _trigger_media_scan(rel)   # 失败静默
           ui.progress_done(f"恢复相册 {rel}", True)
       except Exception as exc:
           ui.progress_done(f"恢复相册 {rel}", False, str(exc))
   ```

---

## 五、联系人模块（`android_backup/contacts.py`）详细设计

### 5.1 备份对象与 API（不变）

备份 `/data/data/com.android.providers.contacts/databases/` 整个目录，tar 打包；
manifest 记录源设备 Android 版本、sdk_int、fingerprint、各 db 的 schema version。

```python
ITEM = {
    "id": "contacts",
    "label": "联系人（含群组/头像/通话记录等）",
    "files": ["contacts/manifest.json", "contacts/contacts_databases.tar"],
}
def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 5.2 备份流程（含进度输出）

联系人模块没有「子目录」的概念，所以按**备份阶段**拆分进度颗粒：

```
⏱ 开始 联系人
  ├─ ⏱ 读取设备版本信息
  ├─ ✅ 读取设备版本信息 · Android 13 / SDK 33 / Xiaomi
  ├─ ⏱ 打包 contacts databases (tar)
  ├─ ✅ 打包 contacts databases · 25 MB
  ├─ ⏱ 拉取 contacts_databases.tar
  ├─ ✅ 拉取 contacts_databases.tar · 用时 0.8s
  ├─ ⏱ 解析 DB schema 并写 manifest
  └─ ✅ 解析 DB schema 并写 manifest · contacts2.db schema=2104
✅ 联系人: 完成 · 共 3 个 db 文件
```

对应的代码结构：

```python
def backup(dest_dir: Path) -> None:
    pkg_root = "/data/data/com.android.providers.contacts"

    ui.progress_start("读取设备版本信息")
    info = _get_device_info()    # {release, sdk, fingerprint}
    ui.progress_done("读取设备版本信息", True,
                     f"Android {info['release']} / SDK {info['sdk']}")

    ui.progress_start("打包 contacts databases (tar)")
    remote_tar, size_bytes = _tar_remote_dir(f"{pkg_root}/databases")
    ui.progress_done("打包 contacts databases (tar)", True, _human_size(size_bytes))

    local_tar = dest_dir / "contacts" / "contacts_databases.tar"
    ui.progress_start("拉取 contacts_databases.tar")
    t0 = time.time()
    adb.pull(remote_tar, local_tar)
    adb.shell(f"rm -f {remote_tar}", check=False)
    ui.progress_done("拉取 contacts_databases.tar", True,
                     f"用时 {time.time()-t0:.1f}s")

    ui.progress_start("解析 DB schema 并写 manifest")
    schemas = _read_schema_versions(local_tar)
    _write_manifest(dest_dir/"contacts", info, size_bytes, schemas,
                    tar_name="contacts_databases.tar")
    ui.progress_done("解析 DB schema 并写 manifest", True,
                     f"{len(schemas)} 个 db，主库 schema={list(schemas.values())[0]}")
```

### 5.3 恢复流程（含进度输出）

同样按阶段拆分颗粒，且在跨版本警告阶段**单独输出一条红色警告行**
（用 `ui.show_warning`，不是 progress_*，因为它是弹窗级横幅）。

阶段拆分：

```
⏱ 开始 恢复联系人
  ├─ ⏱ 检查兼容性
  │    [show_warning: 源版本 11 → 目标版本 13，跨大版本可能导致崩溃，已自动保留目标设备预备份]
  ├─ ✅ 检查兼容性 · 源=11 / 目标=13（用户确认继续）
  ├─ ⏱ 预备份目标设备现有联系人数据库
  ├─ ✅ 预备份目标设备现有联系人数据库 · _pre_restore_backup_<ts>.tar
  ├─ ⏱ 停止 contacts provider 进程
  ├─ ✅ 停止 contacts provider 进程
  ├─ ⏱ 推送并解包 databases
  ├─ ✅ 推送并解包 databases
  ├─ ⏱ 修复权限 + restorecon SELinux 标签
  ├─ ✅ 修复权限 · owner=u0_a9  SELinux=u:object_r:app_data_file:s0:c9,c255
  ├─ ⏱ 重启 provider 进程
  └─ ✅ 重启 provider 进程 · force-stop 成功
✅ 恢复联系人: 完成
```

---

## 六、短信 / 彩信 模块（`android_backup/sms.py`）详细设计

### 6.1 备份对象与 API（不变）

备份 `/data/data/com.android.providers.telephony/databases/` + `app_parts/` 两个子目录，
分别生成 `mmssms_databases.tar` + `app_parts.tar`（后者不存在则跳过）。

```python
ITEM = {
    "id": "sms",
    "label": "短信与彩信（含会话、附件）",
    "files": ["sms/manifest.json", "sms/mmssms_databases.tar", "sms/app_parts.tar(可选)"],
}
def backup(dest_dir: Path) -> None: ...
def restore(src_dir: Path) -> None: ...
```

### 6.2 备份进度颗粒

```
⏱ 开始 短信
  ├─ ⏱ 读取设备版本信息
  ├─ ✅ 读取设备版本信息 · Android 13 / SDK 33
  ├─ ⏱ 打包 mmssms databases
  ├─ ✅ 打包 mmssms databases · 12 MB
  ├─ ⏱ 拉取 mmssms_databases.tar
  ├─ ✅ 拉取 mmssms_databases.tar · 用时 0.4s
  ├─ ⏱ 打包彩信 app_parts (附件目录)   [不存在时输出灰色"跳过"而非失败]
  ├─ ✅ 打包彩信 app_parts · 208 MB
  ├─ ⏱ 拉取 app_parts.tar
  ├─ ✅ 拉取 app_parts.tar · 用时 22.3s
  └─ ✅ 写 manifest
✅ 短信: 完成
```

### 6.3 恢复进度颗粒

与联系人恢复相同，但要**额外 push/untar `app_parts.tar`** 并在该步骤前后多输出一对进度行。

---

## 七、既有 apps 模块同步补上进度输出

`apps.py` 的 `backup()` 和 `restore()` 目前是收集 entries 列表后静默结束，没有过程输出。本次同步改造：

### 7.1 apps.backup() 逐包进度

```python
for pkg in selected:
    display = _display_name(pkg)   # label + 包名
    ui.progress_start(f"备份应用 {display}")
    try:
        entries.append(backup_one_package(pkg, dest_dir))
        detail = "APK ✓  数据 ✓  ext ✓  obb ✓"  # 根据 has_* 动态拼
        ui.progress_done(f"备份应用 {display}", True, detail)
    except Exception as exc:
        ui.progress_done(f"备份应用 {display}", False, str(exc))
        entries.append({"pkg": pkg, "error": str(exc)})
```

### 7.2 apps.restore() 逐包进度

同样在 for 循环里调用 `progress_start/done`，并根据阶段信息（APK 安装/数据恢复/
ext/obb）写 detail。

---

## 八、顶层注册改动（不变）

### 8.1 `backup.py`

```python
from android_backup import adb, apps, ui, photos, contacts, sms

SUPPORTED_ITEMS = [
    {**photos.ITEM,   "requires_root": False},
    {**contacts.ITEM, "requires_root": True},
    {**sms.ITEM,      "requires_root": True},
    {**apps.ITEM,     "requires_root": True},
]
ITEM_BY_ID = {it["id"]: it for it in SUPPORTED_ITEMS}
BACKEND_BY_ID = {
    photos.ITEM["id"]:   photos,
    contacts.ITEM["id"]: contacts,
    sms.ITEM["id"]:      sms,
    apps.ITEM["id"]:     apps,
}
```

并将 SUPPORTED_ITEMS 循环改为 §3.3 中「begin_progress + progress_start/done per item」的形式。

### 8.2 `restore.py`

```python
from android_backup import adb, apps, ui, photos, contacts, sms

ITEM_REQUIRES_ROOT = {
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

恢复循环同步套用 `begin_progress + progress_start/done per item`。

---

## 九、完整改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `android_backup/ui.py` | **修改** | 新增 `begin_progress_section()`、`progress_start()`、`progress_done()`、`_html_escape()` 四个进度输出 helper |
| `android_backup/photos.py` | **新增** | 相册扫描 + 勾选 + pull/push + 每个目录一对 progress_start/done |
| `android_backup/contacts.py` | **新增** | root tar 备份 contacts DB；恢复按 6 个阶段输出进度；跨版本警告 + 目标库预备份 |
| `android_backup/sms.py` | **新增** | 同 contacts，额外含 app_parts 子目录与对应进度行 |
| `android_backup/apps.py` | **修改** | `backup()` 和 `restore()` 的 for 循环中加逐包 progress_start/done |
| `backup.py` | **修改** | 注册 photos/contacts/sms；item 级循环前后调 begin_progress / progress_start / progress_done；删除 WiFi 注释 |
| `restore.py` | **修改** | 同上注册 + 进度行接入；删除 WiFi 注释 |
| `android_backup/adb.py` | 修改（轻度可选） | 新增 `getprop(name)`、`pull_dir(remote, local, preserve=True)`；或直接在模块内实现 |
| `requirements.txt` | **不变** | 无新依赖 |

---

## 十、风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 相册文件数万 → find 超时卡死 | 整体进度停滞 | `-maxdepth 4` + timeout=60s；失败降级为预设目录列表 |
| 大视频 4GB+ → 用户误以为卡死 | 体验 | progress_start 中用 `note` 字段提前告知预计文件数/大小；`adb pull` 期间即使无新行，用户也知道当前在跑哪个目录 |
| `-a` 拉取参数旧版 adb 不识别 | 失败 | stderr 检测 "unknown option" → 无 `-a` 降级重试；progress_done(False) 会把降级提示作为 detail 显示 |
| 跨大版本恢复联系人/sms → provider 崩溃 | 数据安全 | manifest 对比版本并 show_warning；恢复前 tar 目标现有 DB 到本地 `_pre_restore_backup_<ts>.tar` 并输出进度行 |
| 厂商 ROM 不使用标准 telephony/contacts provider | 备份为空 | `pm path` 检测包名是否存在；不存在时 progress_done(True) 但 detail 注明 "未找到标准包名，备份内容可能为空" |
| progress_scope 行数过多 → 页面过长 | 视觉 | 控制粒度在"一个子目录/一个包/一个阶段"级别即可，不做逐文件输出；一般最多几百行完全可接受 |
| PyWebIO use_scope 顺序与后台线程冲突 | 渲染错位 | 所有 progress_* 都在主线程调用（当前 backup/restore 本身是同步串行执行），不存在并发问题 |

---

## 十一、执行顺序（Todo List）

### Phase 1 — 公共基础设施
1. 修改 `ui.py`：新增 `begin_progress_section / progress_start / progress_done / _html_escape`
2. 修改 `adb.py`（可选）：新增 `getprop` 与带 `-a` 降级的 `pull_dir`
3. 修改 `backup.py`：三模块注册 + begin_progress + item 级进度；删除 WiFi 文字
4. 修改 `restore.py`：三模块注册 + item 级进度；删除 WiFi 文字
5. 修改 `apps.py`：逐包进度输出

### Phase 2 — 相册模块
6. 新建 `android_backup/photos.py`：
   - `ITEM` + `_is_media_file` + `_scan_album_dirs` + `_estimate_dir_stats` + `_human_size`
   - `select_albums_interactive`
   - `_pull_with_timestamp_fallback`
   - `backup()`（逐目录 progress_start/done）
   - `restore()`（逐目录 progress_start/done + 媒体扫描）

### Phase 3 — 联系人模块
7. 新建 `android_backup/contacts.py`：
   - `ITEM`
   - `_get_device_info`（调用 adb getprop）
   - `_tar_remote_to_local`（通用 tar 封装，输出阶段进度）
   - `backup()`（五阶段进度行）
   - `_pre_restore_backup_target`（推送前先拉一份目标的 DB 到本地，输出进度行）
   - `_chown_restorecon_reload`（解包后权限修复，输出进度行）
   - `restore()`（版本警告 + 预备份 → 解包 → chown → restart 六阶段输出）

### Phase 4 — 短信模块
8. 新建 `android_backup/sms.py`：整体结构同 contacts，多一个 app_parts 子目录的
   条件判断 + 独立进度行（不存在时写"跳过 app_parts（目录不存在）"）

### Phase 5 — 自测
9. 功能验证（同前一版计划的 Phase E）
10. **进度体验专项验证**：
    - 备份 photos 一个大目录：看 "⏱ 开始" → 等待 → "✅ 完成" 是否连续出现，detail 是否准确
    - 备份 apps 选中多包：每个包都独立输出一对开始/结束行
    - 故意让一个相册 pull 失败（拔掉 USB 或在中途断开 adbd）：看 ❌ 行出现后后续条目是否继续
    - 恢复 contacts 跨版本：看 show_warning 横幅 + 预备份进度行
    - 验证最终「汇总表」和「过程流」一致，没有缺失条目
