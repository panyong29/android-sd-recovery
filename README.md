# android-sd-forensics

安卓手机 TF/SD 卡镜像的数据恢复与取证工具集，同时也是一个可直接安装使用的
[WorkBuddy](https://www.workbuddy.cn) Skill。

面向这类场景：卡被安卓手机用作**内部存储（Adoptable Storage，dm-crypt 加密）**、
换机或手机损坏后读不出来，或者卡被格式化、照片被误删。

## 能做什么 / 不能做什么

| | 说明 |
|---|---|
| 能 | 判定镜像结构、定位加密区与明文残留区 |
| 能 | 在引导扇区和 FAT 表损坏的情况下，重建目录项，拿回**真实文件名、大小、日期** |
| 能 | 按文件结构解析边界并解码校验，雕刻出 JPEG / PNG / GIF / WEBP / BMP / MP4 / SQLite / ZIP·APK |
| 能 | 用"文件大小 + 日期"把雕刻数据和目录项配对，按**原文件名与原目录树**导出 |
| 能 | 有手机端密钥时，解密整盘（文件名、目录、时间戳全保留） |
| 不能 | 在无密钥的情况下解密 Adoptable Storage —— 128 位真随机密钥数学上不可解 |
| 不能 | 恢复被覆写过的目录块（表现为路径里出现 `unknown_dir_<簇号>`，会如实标注，不臆造） |

## 快速开始

```bash
pip install pillow pycryptodome

# 1) 摸清镜像结构：有没有 android_expand 分区、哪里是密文、还剩多少签名
python scripts/probe_image.py sdcard.img --out probe.json

# 2) 重建目录项，拿回文件名
python scripts/fat_dirs.py sdcard.img --out fat_out

# 3) 深度雕刻（全部经过结构解析与解码/校验）
python scripts/carve.py sdcard.img --out carved

# 4) 把名字和数据配回去，按原目录导出
python scripts/match_restore.py --carve-json carved/carve_records.json \
                                --dirs-json fat_out/fat_dirs.json \
                                --out restored --cats photo,video
```

有密钥时走解密路线（远优于雕刻）：

```bash
python scripts/decrypt_expand.py expand_<UUID>.key sdcard.img decrypted.img --limit 64
```

## 工作流

```
镜像 ──► probe_image.py ──► 有 android_expand 分区？
                              │
              ┌───────────────┴────────────────┐
           有密钥                            无密钥
              │                                │
    decrypt_expand.py                  fat_dirs.py (名字)
    解密 + 挂载 ext4                          +
    文件名/目录/时间全保留              carve.py (数据)
                                              +
                                       match_restore.py (配对)
                                       按原目录导出 + 对照表
```

## 脚本

| 脚本 | 作用 | 关键产出 |
|---|---|---|
| `probe_image.py` | GPT 解析、熵与零字节分布、签名统计 | `probe_summary.json` |
| `fat_dirs.py` | FAT32 目录项锚点法重建（无需引导扇区） | `fat_dirs.json` |
| `carve.py` | 多格式深度雕刻，结构解析 + 解码/CRC 校验 | 分类文件 + `carve_records.json` + 报告 |
| `match_restore.py` | 大小 + 日期配对，还原原文件名与目录树 | `原名对照表.csv` + 对照 HTML |
| `decrypt_expand.py` | dm-crypt 解密（aes-cbc-essiv / xts） | 可挂载的 ext4/f2fs 镜像 |

## 实战验证

在一张 7.39 GiB 的 HTC 手机 TF 卡（Android 6.0 adoptable storage，密钥随手机丢失）上：

- 探查：开头 104 MiB 为 dm-crypt 密文，其余为明文旧 FAT32 残留
- 目录项：重建出 9188 条（8395 个文件 / 793 个子目录），840 个目录路径
- 雕刻：1902 个文件 / 2.27 GiB，含相机原图 80 张、视频 5 个（最大 1950 MB，box 链完整可播放）
- 配对：89 个大文件中 **48 个零歧义命中真实文件名**（`IMAG0947.jpg` 等 HTC 相机命名），
  EXIF 拍摄时间与目录项日期逐张吻合

完整复盘见 `references/case-htc-tf-card.md`。

## 几条硬经验

1. **放弃簇链定位**。文件删除后 FAT 链清零、簇被复用，簇号→偏移映射实测支持率仅 1/48。
   配对只走"大小 + 日期"。
2. **高熵不等于加密**。JPEG/MP4 压缩数据也接近 8 bit/byte，要看零字节占比（密文约 1/256）
   和是否整片连续。
3. **LFN 片段逆序存放**，拼接前必须反转。
4. **`0xE5` 是 UTF-8 常见首字节**，只凭它判断删除项会解析进中文文本产生海量伪条目，
   必须配合字符集与日期校验。
5. **采样步长别用 512**，会与扇区对齐导致熵估计偏差（质数 997 可解）。

## 目录结构

```
android-sd-forensics/
├── SKILL.md                      Skill 定义与工作流
├── README.md
├── scripts/                      五个可执行脚本
└── references/
    ├── adoptable-storage.md      加密机制与密钥获取途径
    ├── fat32-forensics.md        目录项结构与锚点法
    ├── carving-playbook.md       各格式边界解析要点
    └── case-htc-tf-card.md       完整案例复盘
```

## 作为 Skill 安装

把整个目录放到 `~/.workbuddy/skills/android-sd-forensics/`（用户级）
或项目的 `.workbuddy/skills/` 下即可，WorkBuddy 会在相关任务中自动识别并加载。

## 发布到 GitHub

```bash
cd android-sd-forensics
git init && git add -A
git config user.name  "你的名字"
git config user.email "你的邮箱"
git commit -m "feat: 安卓 SD/TF 卡镜像恢复工具集"
git branch -M main
git remote add origin https://github.com/<用户名>/android-sd-forensics.git
git push -u origin main
```

使用个人访问令牌（repo 权限）认证时：

```bash
git remote set-url origin https://<用户名>:<令牌>@github.com/<用户名>/android-sd-forensics.git
```

推送后建议吊销该令牌。

## 免责声明

仅用于恢复本人拥有合法权利的数据，或经授权的取证工作。请遵守当地法律法规。
