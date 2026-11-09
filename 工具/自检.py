# -*- coding: utf-8 -*-
"""一键自检：界面默认位置 / 位置栏 / 桌面拖放 / 中文文件名推送与拉取。

用法（两种都行）：
    双击  工具\\自检.bat
    命令行 G:\\xnj\\python311\\python.exe -S 工具\\自检.py

安全说明：
- 只在手机的 /sdcard/Download 建一个临时文件，验完立即删除；
- 不改动手机上任何原有文件；
- 电脑侧只在系统临时目录里读写，不碰你的桌面。
"""
import importlib.util
import os
import sys
import tempfile
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "高效交互平台.py")

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("[PASS] " if cond else "[FAIL] ") + name + (("  | " + str(detail)) if detail != "" else ""))


def main():
    spec = importlib.util.spec_from_file_location("wb", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tk.Tk()
    root.withdraw()
    app = mod.App(root)
    for _ in range(6):
        root.update()

    # ---- 1. 默认位置：左边桌面、右边「下载与接收」
    check("手机侧默认停在「下载与接收」", app.current_path.rstrip("/") == "/sdcard/Download", app.current_path)
    check("电脑侧默认在桌面", "desktop" in app.local_path.lower(), app.local_path)

    # ---- 2. 位置栏与桌面拖放
    check("位置栏入口齐全", len(getattr(app, "place_btns", {})) == len(mod.PHONE_PLACES),
          "%d 个" % len(getattr(app, "place_btns", {})))
    check("桌面拖放已挂载", getattr(app, "_drop_ready", False), getattr(app, "_drop_ready", "未设置"))

    # ---- 3. 设备与目录
    ser = mod.get_serial()
    check("识别到手机", bool(ser), ser or "未连接")
    if not ser:
        print("\n手机未连接，其余检查跳过。插好数据线、手机弹窗点「允许」后重跑。")
        finish(root)
        return

    app.browse("/sdcard/Download/")
    for _ in range(50):
        root.update()
        if app.entries:
            break
        time.sleep(0.1)
    check("手机侧读到条目", len(app.entries) > 0, "%d 项" % len(app.entries))

    # ---- 4. 缩略图：手机上的图片能不能真的解码出预览图
    check("文件页是缩略图网格",
          isinstance(app.grid_local, mod.FileGrid) and isinstance(app.grid_phone, mod.FileGrid))
    check("格子已绘制", len(app.grid_phone._tiles) + len(app.grid_local._tiles) > 0,
          "手机 %d 格 / 电脑 %d 格" % (len(app.grid_phone._tiles), len(app.grid_local._tiles)))

    rc, out = mod.adb(["-s", ser, "shell",
                       "find /sdcard/DCIM /sdcard/Pictures /sdcard/Download "
                       "-maxdepth 2 -type f -name '*.jpg' 2>/dev/null | head -1"])
    probe_img = out.strip().splitlines()[0].strip() if out.strip() else ""
    if probe_img:
        png = mod.thumb_cache_path("|selftest|" + probe_img)
        ok = app._thumb_from_phone(probe_img, png)
        check("手机图片能生成缩略图", ok,
              "%s -> %s" % (os.path.basename(probe_img),
                            (str(os.path.getsize(png)) + " bytes") if ok else "失败"))
        try:
            os.remove(png)
        except OSError:
            pass
    else:
        print("[跳过] 手机上没有找到 jpg，缩略图检查略过")

    # ---- 5. 中文文件名推送（这是之前最容易坏的一环）
    name = "自检_中文文件名_20260922.docx"
    tmp = tempfile.mkdtemp(prefix="wbcheck_")
    src = os.path.join(tmp, name)
    with open(src, "wb") as f:
        f.write(b"payload" * 200)              # 1400 字节
    logs = []
    real_log = app.log
    app.log = lambda m, lv="info": logs.append((lv, m))
    app._push_to_phone([src], "/sdcard/Download")
    app.log = real_log
    check("推送中文名文件成功", any("已推到手机" in m for _, m in logs),
          [m for _, m in logs][-1][:80] if logs else "")

    remote = "/sdcard/Download/" + name
    rc, out = mod.adb(["-s", ser, "shell", "stat -c %%s %s" % mod.shq(remote)])
    got = out.strip().splitlines()[-1].strip() if out.strip() else ""
    check("手机端文件名完整、字节数正确", got == "1400", "远端 %s 字节" % (got or "无此文件"))

    # ---- 6. 拉回电脑
    dest = tempfile.mkdtemp(prefix="wbcheck_out_")
    logs.clear()
    app.log = lambda m, lv="info": logs.append((lv, m))
    app._pull_to_local([remote], [name], dest)
    app.log = real_log
    final = os.path.join(dest, name)
    check("拉回后文件名完好", os.path.exists(final), [m for _, m in logs][-1][:80] if logs else "")
    if os.path.exists(final):
        check("拉回后字节数一致", os.path.getsize(final) == 1400, os.path.getsize(final))
    check("不留临时文件", not any(x.endswith(".part") for x in os.listdir(dest)), os.listdir(dest))

    # ---- 收尾：清掉手机与电脑上的自检文件
    mod.adb(["-s", ser, "shell", "rm -f %s" % mod.shq(remote)])
    leftovers = mod.adb(["-s", ser, "shell", "ls %s 2>/dev/null" % mod.shq("/sdcard/" + name)])[1]
    check("手机侧已清理干净", not leftovers.strip(), leftovers.strip()[:40])

    finish(root)


def finish(root):
    print()
    print("通过 %d/%d" % (sum(results), len(results)))
    try:
        root.destroy()
    except Exception:
        pass
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
