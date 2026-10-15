---
name: android-sd-forensics
description: 安卓手机 TF/SD 卡镜像的数据恢复与取证。适用于卡被格式化、被用作 Adoptable Storage（内部存储扩展，dm-crypt 加密）、分区表损坏或文件被大量删除的场景。提供镜像探查（GPT 与熵分布判定加密区）、FAT 目录项锚点法重建、多格式深度文件雕刻（JPEG/PNG/GIF/WEBP/BMP/MP4/SQLite/ZIP，带结构解析与解码校验）、以及用文件大小与日期把雕刻数据配回原始文件名与目录树。当用户提到 SD/TF 卡数据恢复、手机照片找回、android_expand 分区、adoptable storage 解密、镜像文件雕刻、FAT 目录项重建时应使用本技能。
agent_created: true
---

# Android SD/TF 卡镜像恢复

## 用途

把一张"读不出文件系统"的手机卡镜像，尽可能还原成有名字、有目录、可打开的文件，
并如实界定哪些部分技术上不可能恢复。

## 何时使用

- 卡被安卓手机用作"内部存储/可合并存储"，换机或手机损坏后无法读取
- 镜像开头无有效引导扇区，或挂载后目录为空/乱码
- 照片、视频被误删，或卡被格式化过
- 用户手上有 `.img` / `.dd` 原始镜像，要求"尽量把照片恢复出来"

## 工作流

### 第 1 步：摸清镜像结构

```bash
python scripts/probe_image.py <镜像> --out probe.json
```

看三件事：GPT 里有没有 `android_expand` 分区、哪些区域是高熵（密文）、
还有多少文件签名残留在明文区。这一步决定整条路线，不要跳过。

### 第 2 步：判断走哪条路

| 情况 | 路线 |
|---|---|
| 有 `android_expand` 分区，且能拿到手机端密钥 | **A：解密路线**（最优，文件名/目录/时间戳全保留） |
| 有 `android_expand` 分区，手机与备份都没了 | **B：明文残留路线**（本 skill 主力） |
| 卡未被采纳（普通 FAT/exFAT 被删被格式化） | **B**，且目录项通常更完整 |

走 A 时：

```bash
python scripts/decrypt_expand.py <key文件> <镜像> <输出> --limit 64   # 先小范围验证
python scripts/decrypt_expand.py <key文件> <镜像> <输出>              # 通过后整盘
```

密钥在手机 `/data/misc/vold/expand_<UUID>.key`（16 字节）。拿到就赢，拿不到就别穷举——
128 位真随机密钥不可解。详见 `references/adoptable-storage.md`。

### 第 3 步：重建目录项（拿到"名字"）

```bash
python scripts/fat_dirs.py <镜像> --out fat_out
```

产出 `fat_dirs.json`：每个目录簇的条目（长文件名、大小、日期、首簇、删除标记）。
路径靠 cluster 父子关系拼出；拼不出的目录会标成 `unknown_dir_<簇号>`。

### 第 4 步：深度雕刻（拿到"数据"）

```bash
python scripts/carve.py <镜像> --out carved [--start MiB] [--end MiB] [--min-photo KB]
```

产出分类文件 + `carve_records.json`（偏移、MD5、EXIF、完整性）+ 清单 CSV + 画廊 HTML。
全部文件都经过结构解析与解码/CRC 校验，不是按签名硬截断。

### 第 5 步：把名字和数据配回去

```bash
python scripts/match_restore.py --carve-json carved/carve_records.json \
                                --dirs-json fat_out/fat_dirs.json \
                                --out 恢复数据_带原名 --cats photo,video
```

按**精确文件大小**配对（大文件的字节数是强指纹），多候选时用 EXIF 日期与目录项日期交叉校验。
产出按原目录树的副本 + `原名对照表.csv`（含匹配依据与置信度）+ 对照 HTML。

### 第 6 步：交付与如实说明

优先交付相机原图与视频（用户真正想要的），App 缓存的 PNG/图标单独归类、降低优先级。
明确告知：加密区不可恢复；未匹配文件按 `IMG_日期_时间.jpg` 规范命名；目录名若标注为
"推断"，就不是实测结果。

## 关键规则

1. **不要臆造文件名**。目录名拼不出来就是拼不出来，用 `unknown_dir_<簇号>` 并在结果里标注，
   不要用"看起来的惯例"冒充实测。用户可用 `--default-dir` 显式指定兜底目录，此时标注为"用户指定"。
2. **放弃簇链定位**。文件删除后 FAT 链清零、簇被复用，簇号→偏移映射实测支持率极低（本案例 1/48）。
   配对只走"大小 + 日期"。
3. **高熵不等于加密**。JPEG/MP4 压缩数据也接近 8 bit/byte，要结合零字节占比（密文约 1/256）
   与位置（整片连续高熵）判断。
4. **先验证后落盘**。雕刻必须做解码/CRC 校验，否则会产出大量打不开的假文件。
5. **只读原始镜像**。全程只读打开源镜像，绝不原地写回。

## 参考文档

- `references/adoptable-storage.md` — 加密机制、密钥获取途径与判定方法
- `references/fat32-forensics.md` — 目录项结构、锚点法、LFN 逆序与 0xE5 陷阱
- `references/carving-playbook.md` — 各格式边界解析要点与去重策略
- `references/case-htc-tf-card.md` — 7.39 GiB HTC 卡完整复盘（含各阶段真实产出数字）

## 依赖

Python 3.9+，`pillow`（图像解码与 EXIF），`pycryptodome`（仅解密路线需要）。

```bash
pip install pillow pycryptodome
```
