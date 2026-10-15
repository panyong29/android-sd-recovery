#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把雕刻出的文件与 FAT 目录项配对, 还原原始文件名并可按目录树导出.

配对依据(按优先级):
  1. 精确文件大小 —— 大文件的字节数是强指纹, 实战中零歧义
  2. 大小相同但多候选时, 用 EXIF 拍摄日期 vs 目录项日期交叉校验
  3. ±1KB 近似大小(尾部有填充/轻微截断的情况)
簇号→偏移映射通常不可靠(文件删除后簇链断裂、簇被复用), 不要依赖它定位数据。

用法:
    python match_restore.py --carve-json carved/carve_records.json \
                            --dirs-json  fat_out/fat_dirs.json \
                            --out 恢复数据_带原名 [--cats photo,video] \
                            [--default-dir DCIM/100HTC]   # 目录名丢失时的兜底目录

产出: <out>/ 按原目录树的文件副本 + 原名对照表.csv + 原名对照.html
      CSV 含"匹配依据/置信度/目录名来源"列, 便于人工复核, 不臆造目录名。
"""
import argparse, base64, collections, csv, io, json, os, re, shutil
from PIL import Image


def norm_date(d):
    return "".join(ch for ch in str(d)[:10] if ch.isdigit())


def build_tree(dirs):
    """返回 cluster -> 目录路径; 名字缺失时用 unknown_dir_<cluster>"""
    info = {}
    for blk in dirs.values():
        own = blk.get("own_cluster")
        if own is None:
            continue
        info.setdefault(own, {"parent": blk.get("parent_cluster"), "name": None})
    for blk in dirs.values():
        own = blk.get("own_cluster")
        for e in blk.get("entries", []):
            if not (e.get("attr", 0) & 0x10 or e.get("kind") == "dir"):
                continue
            c, nm = e.get("cluster", 0), e.get("name") or ""
            if c and nm not in (".", "..") and c != own and c in info and info[c]["name"] is None:
                info[c]["name"] = nm

    def path_of(cl, depth=0, seen=None):
        seen = seen or set()
        if cl in (0, 2) or cl not in info or cl in seen or depth > 24:
            return ""
        seen.add(cl)
        nm = info[cl]["name"] or f"unknown_dir_{cl}"
        p = path_of(info[cl].get("parent") or 0, depth + 1, seen)
        return (p + "/" + nm) if p else nm
    return path_of


def safe(p):
    for ch in ':*?"<>|':
        p = p.replace(ch, "_")
    return "/".join((x.rstrip(" .") or "_") for x in p.split("/"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carve-json", required=True)
    ap.add_argument("--dirs-json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cats", default="photo,video")
    ap.add_argument("--default-dir", default="", help="目录名丢失时的兜底目录(会标注为推断)")
    a = ap.parse_args()

    dirs = json.load(open(a.dirs_json, encoding="utf-8"))
    carve = json.load(open(a.carve_json, encoding="utf-8"))
    cats = set(a.cats.split(","))
    path_of = build_tree(dirs)

    items = []
    for blk in dirs.values():
        dp = path_of(blk.get("own_cluster"))
        for e in blk.get("entries", []):
            if e.get("attr", 0) & 0x10 or e.get("kind") == "dir":
                continue
            nm = e.get("name") or e.get("shortname") or ""
            if nm in (".", "..") or not nm:
                continue
            items.append({"name": nm, "size": e.get("size", 0), "date": e.get("date", ""),
                          "deleted": e.get("deleted", False), "dir": dp,
                          "path": (dp + "/" + nm) if dp else nm})
    by_size = collections.defaultdict(list)
    for i in items:
        if i["size"] > 0:
            by_size[i["size"]].append(i)
    print(f"目录项文件条目: {len(items)}")

    os.makedirs(a.out, exist_ok=True)
    used, rows = set(), []
    for c in carve:
        if c["cat"] not in cats:
            continue
        size, exif = c["size"], c.get("exif", {}) or {}
        edate = norm_date(exif.get("date", ""))
        cand = by_size.get(size, [])
        how = conf = None
        it = None
        if len(cand) == 1:
            it, how, conf = cand[0], "精确大小", "高"
        elif len(cand) > 1:
            same = [x for x in cand if norm_date(x["date"]) == edate] if edate else []
            if len(same) == 1:
                it, how, conf = same[0], "大小+日期", "高"
            elif len(same) > 1:
                it, how, conf = same[0], "大小+日期(多候选)", "中"
            else:
                it, how, conf = cand[0], "大小(多候选,日期不符)", "低"
        else:
            near = [x for x in items if x["size"] and abs(x["size"] - size) <= 1024
                    and os.path.splitext(x["name"])[1].lower() in (".jpg", ".jpeg", ".mp4", ".3gp")]
            if len(near) == 1:
                it, how, conf = near[0], "近似大小±1KB", "中"

        if it:
            dp = it["dir"]
            dirsrc = "实测"
            if "unknown_dir_" in dp:
                dp, dirsrc = (a.default_dir or dp), ("用户指定" if a.default_dir else "缺失")
            rel = safe(dp + "/" + it["name"])
            sub, fname = os.path.dirname(rel) or ".", os.path.basename(rel)
        else:
            d = str(exif.get("date", ""))
            pre = "VID" if c["cat"] == "video" else "IMG"
            stem = (f"{pre}_{d[:10].replace(':','')}_{d[11:19].replace(':','')}"
                    if len(d) >= 19 else (f"{pre}_{edate}" if edate else
                                          os.path.splitext(os.path.basename(c["path"]))[0]))
            tag = "视频" if c["cat"] == "video" else "照片"
            sub = os.path.join("_未匹配", tag)
            fname = stem + os.path.splitext(c["path"])[1].lower()
            how, conf, dirsrc = "无匹配", "-", "-"

        dd = os.path.join(a.out, sub)
        os.makedirs(dd, exist_ok=True)
        dest = os.path.join(dd, fname)
        if dest.lower() in used:
            st, ex = os.path.splitext(fname)
            n = 2
            while os.path.join(dd, f"{st}_{n}{ex}").lower() in used:
                n += 1
            dest = os.path.join(dd, f"{st}_{n}{ex}")
            fname = os.path.basename(dest)
        used.add(dest.lower())
        shutil.copy2(c["path"], dest)
        rows.append({"雕刻文件": os.path.basename(c["path"]), "原文件名": it["name"] if it else "(未匹配)",
                     "原目录": dp if it else "-", "目录名来源": dirsrc,
                     "导出路径": os.path.relpath(dest, a.out).replace("\\", "/"),
                     "大小MB": round(size / 1024**2, 2), "匹配依据": how, "置信度": conf,
                     "EXIF日期": exif.get("date", ""), "目录项日期": it["date"] if it else "",
                     "已删除": "是" if (it and it["deleted"]) else ""})

    with open(os.path.join(a.out, "原名对照表.csv"), "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    cnt = collections.Counter(r["置信度"] for r in rows)
    print(f"处理 {len(rows)} 个: " + ", ".join(f"{k} {v}" for k, v in cnt.items()))

    cards = []
    for r in rows:
        p = os.path.join(a.out, r["导出路径"])
        if p.lower().endswith((".jpg", ".jpeg")):
            try:
                im = Image.open(p)
                im.thumbnail((200, 200))
                b = io.BytesIO()
                im.convert("RGB").save(b, "JPEG", quality=70)
                img = f"<img src='data:image/jpeg;base64,{base64.b64encode(b.getvalue()).decode()}' loading=lazy>"
            except Exception:
                img = "<div class=ph>视频</div>"
        else:
            img = "<div class=ph>视频</div>"
        cls = {"高": "ok", "中": "mid", "低": "low", "-": "no"}[r["置信度"]]
        cards.append(f"<div class='c {cls}'>{img}<div class=t><b>{r['原文件名']}</b>"
                     f"<span>{r['原目录']}</span><span>{r['大小MB']}MB · {r['EXIF日期']} · "
                     f"{r['匹配依据']}</span></div></div>")
    open(os.path.join(a.out, "原名对照.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset=utf-8><title>原名还原对照</title><style>"
        "body{font-family:sans-serif;background:#f4f6f9;margin:0;padding:22px}"
        ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}"
        ".c{background:#fff;border-radius:8px;overflow:hidden;border:1px solid #e3e6ea}"
        ".c img{width:100%;height:150px;object-fit:cover;display:block}"
        ".ph{height:150px;display:flex;align-items:center;justify-content:center;color:#999;background:#eef1f5}"
        ".t{padding:7px 9px;font-size:11px;color:#555}.t b{display:block;color:#222}"
        ".t span{display:block;color:#888}.ok{border-left:4px solid #1D9E75}"
        ".mid{border-left:4px solid #EF9F27}.no{border-left:4px solid #B4B2A9}</style>"
        f"<h1>原名还原对照</h1><p>共 {len(rows)} 个: " +
        ", ".join(f"{k}置信 {v}" for k, v in cnt.items()) + "</p>"
        f"<div class=g>{''.join(cards)}</div>")
    print("输出目录:", a.out)


if __name__ == "__main__":
    main()
