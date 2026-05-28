# Checklist

- [x] `android_backup/apps.py` 提供 `ITEM`、`backup(dest_dir)`、`restore(src_dir)` 接口
- [x] 备份阶段通过 `pm list packages -3` 列出第三方应用并二级勾选
- [x] 单包备份产出 `apps/<pkg>/apk/`、`data.tar`、`info.json`，必要时含 `ext_data.tar` 与 `obb.tar`
- [x] 备份目录根下生成 `apps/manifest.json` 聚合所有备份的包元信息
- [x] 恢复时若设备未装该包且备份含 APK，会自动 `pm install`（多 APK 用 install-create/-write/-commit）
- [x] 恢复时按 `am force-stop` → tar 解包覆盖 → `chown` + `restorecon` 顺序处理 `/data/data/<pkg>`
- [x] `ext_data.tar` 与 `obb.tar` 在存在时被解包到对应外部存储路径
- [x] 单包失败不影响其他包，结果列表呈现每个包的成功/失败/跳过原因
- [x] `backup.py`/`restore.py` 的勾选列表中已注册 `apps` 备份项，且 wifi 路径不受影响
