#!/usr/bin/env python3
"""把 Afterglow i486 base tar.xz 裁剪/转换为 iSH 可直接导入的 tar.gz。

iSH 不引导真正的 Linux 内核——它在用户态模拟 x86 CPU 并把 Linux 系统调用
翻译到 iOS/macOS,因此 Afterglow 里的内核、模块、引导器、硬件管理组件全是
死重;本脚本在 tar 流级别把它们删除(保留全部文件元数据),同时:
  * 把 /sbin/init 替换为一个静态最小 init(由同目录 init.c 编译而来),
    因为 systemd 无法在 iSH 下运行(iSH 默认 boot command 是 /sbin/init)
  * 重写 /etc/resolv.conf(iSH App 启动时也会覆盖它;CLI 下需要可用的 DNS)
  * 注入 /ish/version,使 iSH App 跳过 "Install built-in APK?" 提示
  * tar.xz -> tar.gz(iSH 的 fakefs_import 只支持 tar/tar.gz,见 ish/tools/fakefs.c)

两个结构性坑的处理:
  * 目录归属:某路径只有在"所有登记它的包都被删除"时才删,且不删被保留
    文件的父目录(否则 /usr/share 这类共享目录会被误删)
  * 大小写冲突:iSH 把文件存到宿主文件系统,iOS/模拟器容器在创建和查找上
    的大小写语义不一致,像 perl5 的 sys/ 与 Sys/、内核头文件 ip6t_HL.h 与
    ip6t_hl.h 这类仅大小写不同的路径会让导入直接失败。目录冲突做合并
    (后来者重写到先来者的拼写),文件冲突丢弃后来者。

不修改 dpkg 数据库:所有包条目保持原样,依赖关系完好;日后若想彻底清理,
在 iSH 里运行 oma remove <包名> 即可。

用法: python3 trim-rootfs.py <input.tar.xz> <output.tar.gz> [--keep-docs] [--keep-locales]
需要同目录下有 init(静态 i386 二进制,由 init.c 编译而来)。
"""
import io
import os
import sys
import tarfile

# ---- 整包删除(按 dpkg .list 精确匹配)----
# 选择标准:对"iOS app 内的模拟环境"毫无用途——内核、引导器、物理硬件
# 探测/管理工具(iSH 下没有 netlink、没有真实硬件)、文档工具链。
DROP_PACKAGES = {
    "linux-kernel-7.1.7", "linux+kernel",  # 内核镜像与模块:iSH 自己就是"内核"
    "grub", "os-prober",                   # 引导器:iSH 不存在开机引导过程
    "networkmanager", "systemd-base",      # 需要 netlink;iSH 的网络由 iOS 提供
    "alsa-utils", "audio-base",            # 声卡工具:iSH 没有声音设备
    "pciutils", "usbutils", "hwdata",      # lspci/lsusb 及硬件数据库
    "sysfsutils", "lm-sensors", "pcmciautils",  # 传感器/PCMCIA
    "kmod",                                # 内核模块工具(没有内核模块可加载)
}
DOC_PACKAGES = {"man-db", "groff", "texinfo"}  # --keep-docs 时保留

# ---- 路径前缀删除(与包无关的规则)----
DROP_PREFIXES = [
    "boot/",               # 内核镜像、grub 主题(保留 boot/ 目录本身)
    "usr/lib/modules/",    # 内核模块(冗余保险,内核包已删)
    "usr/src/",            # 内核头文件/构建残留
    "usr/lib/systemd/",    # systemd 的 unit、journald、udevd 等(init 已被替换)
    "etc/systemd/",
    "usr/lib/udev/", "etc/udev/", "usr/share/systemd/",
    "usr/lib/locale/locale-archive",  # 222MB 全 locale 归档;glibc 内置 C/C.UTF-8 仍可用
]
DOC_PREFIXES = ["usr/share/doc/", "usr/share/man/", "usr/share/info/"]

# usr/share/locale 下只保留这些(约 108MB -> 几 MB)
KEEP_LOCALES = {"locale.alias", "zh_CN", "zh_TW", "zh_Hans", "zh_Hant",
                "en_US", "en_GB", "en"}

RESOLV_CONF = b"nameserver 223.5.5.5\nnameserver 119.29.29.29\nnameserver 1.1.1.1\n"

# 冒烟自检:裁剪后这些关键路径必须仍然存在(注意 AOSC 是 usrmerge 布局)
REQUIRED = ["bin", "usr/bin/bash", "usr/bin/login", "usr/lib/ld-linux.so.2",
            "usr/lib/libc.so.6", "usr/bin/apt", "usr/bin/oma", "etc/os-release",
            "usr/share", "usr/share/locale", "usr/bin/init"]


def norm(name):
    name = os.path.normpath(name)
    return "" if name == "." else name


def main():
    src, dst = sys.argv[1], sys.argv[2]
    keep_docs = "--keep-docs" in sys.argv
    keep_locales = "--keep-locales" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "init"), "rb") as f:
        init_blob = f.read()

    drop_pkgs = set(DROP_PACKAGES)
    prefixes = list(DROP_PREFIXES)
    if not keep_docs:
        drop_pkgs |= DOC_PACKAGES
        prefixes += DOC_PREFIXES

    # ---- 第一遍:收集 dpkg 归属、成员清单(名字+类型)、符号链接表 ----
    owners = {}
    members = []      # [(path, is_dir)]
    link_target = {}  # 符号链接 path -> 解析后的绝对目标
    with tarfile.open(src, "r|xz") as tar:
        for m in tar:
            p = norm(m.name)
            members.append((p, m.isdir()))
            if p.startswith("var/lib/dpkg/info/") and p.endswith(".list"):
                pkg = p[len("var/lib/dpkg/info/"):-len(".list")]
                data = tar.extractfile(m).read().decode()
                for line in data.splitlines():
                    line = norm(line.lstrip("/"))
                    if line:
                        owners.setdefault(line, set()).add(pkg)
            if m.issym():
                t = m.linkname
                if not t.startswith("/"):
                    t = os.path.join(os.path.dirname(p), t)
                link_target[p] = norm(t.lstrip("/"))

    pkg_drop = {p for p, ow in owners.items() if ow and ow <= drop_pkgs}

    def rule_drop(path):
        if not path:
            return False
        if path in pkg_drop:
            return True
        if any(path == pr.rstrip("/") or path.startswith(pr) for pr in prefixes):
            return True
        if not keep_locales and path.startswith("usr/share/locale/"):
            top = path[len("usr/share/locale/"):].split("/", 1)[0]
            if top not in KEEP_LOCALES:
                return True
        return False

    # 父目录保护:任何被保留成员的祖先目录一律保留
    protected = set()
    for p, _ in members:
        if not rule_drop(p):
            parts = p.split("/")
            for i in range(1, len(parts)):
                protected.add("/".join(parts[:i]))

    dropped = {p for p, _ in members if rule_drop(p) and p not in protected}

    # 符号链接指向已删路径则一并删除(收敛到不动点;如 /etc/profile.d/
    # 70-systemd-shell-extra.sh -> 被删的 usr/lib/systemd/...,留着会每次登录报错)
    changed = True
    while changed:
        changed = False
        for p, t in link_target.items():
            if p not in dropped and (t in dropped or rule_drop(t)):
                dropped.add(p)
                changed = True

    # 这些路径会被原地替换内容,它们自身及指向它们的链接不能删
    keep_anyway = {"sbin/init", "usr/bin/init", "etc/alternatives/init",
                   "etc/resolv.conf"}
    dropped -= keep_anyway

    # ---- 大小写冲突处理 ----
    # iSH 的 fakefs 元数据层按路径精确匹配(区分大小写),而数据层落在宿主
    # 文件系统上(大小写语义不定),唯一稳妥的做法是 tar 里完全消除仅大小写
    # 不同的路径。目录冲突:选一个"赢家"拼写,输家改名合并(子项随之重写);
    # 文件冲突:丢弃后来者(内核头文件/iptables 模块之类在 iSH 下用不到)。
    # 赢家默认取全小写拼写(terminfo 约定),例外用 DIR_WINNER 指定
    # (perl 的 Sys::Syslog 与 Pod::* 模块查找用大写目录,文档 .ph/.pod 合并进去)。
    DIR_WINNER = {
        "usr/lib/perl5/core_perl/sys": "usr/lib/perl5/core_perl/Sys",
        "usr/share/perl5/core_perl/pod": "usr/share/perl5/core_perl/Pod",
    }
    by_cf = {}
    for p, is_dir in members:
        if is_dir and p not in dropped:
            by_cf.setdefault(p.casefold(), []).append(p)
    dir_rename = {}
    for cf, variants in by_cf.items():
        if len(variants) < 2:
            continue
        win = DIR_WINNER.get(cf)
        if win not in variants:
            lowers = [v for v in variants if v == v.lower()]
            win = lowers[0] if lowers else variants[0]
        for v in variants:
            if v != win:
                dir_rename[v] = win
                print(f"dir case-merge: {v} -> {win}", file=sys.stderr)

    def rewrite(path):
        parts = path.split("/")
        for i in range(1, len(parts) + 1):  # 含完整路径自身(目录项也要改名)
            pref = "/".join(parts[:i])
            if pref in dir_rename:
                parts[:i] = dir_rename[pref].split("/")
        return "/".join(parts)

    # 文件级冲突(在重写后的路径上判定)
    winner = {}
    case_drop = set()
    for p, is_dir in members:
        if p in dropped:
            continue
        rp = rewrite(p)
        cf = rp.casefold()
        if cf in winner:
            case_drop.add(p)
            if not is_dir:
                print(f"case-collision, dropping file: {p} (kept {winner[cf]})",
                      file=sys.stderr)
            continue
        winner[cf] = rp

    # ---- 第二遍:过滤 + 重写写出 ----
    kept_paths = set()
    stats = {"kept": 0, "dropped": 0, "drop_bytes": 0}
    with tarfile.open(src, "r|xz") as tin, tarfile.open(dst, "w|gz") as tout:
        # 注入 /ish/version:让 iSH App 认为该文件系统已被管理,
        # 从而跳过首次启动时的 "Install iSH's built-in APK?" 提示
        for name, blob, mode in [("ish", None, 0o755), ("ish/version", b"999999\n", 0o644)]:
            m = tarfile.TarInfo(name)
            if blob is None:
                m.type = tarfile.DIRTYPE
            else:
                m.type = tarfile.REGTYPE
                m.size = len(blob)
            m.mode = mode
            m.uid = m.gid = 0
            m.mtime = 0
            tout.addfile(m, None if blob is None else io.BytesIO(blob))
            kept_paths.add(name)

        for m in tin:
            p = norm(m.name)
            if p in dropped or p in case_drop:
                stats["dropped"] += 1
                stats["drop_bytes"] += m.size
                continue
            # 硬链接指向已删文件会导致 iSH 导入时 linkat 失败,跳过
            if m.islnk() and norm(m.linkname.lstrip("/")) in dropped:
                stats["dropped"] += 1
                continue

            rp = rewrite(p)

            if p in ("sbin/init", "usr/bin/init", "etc/alternatives/init"):
                # 替换为静态最小编译 init(systemd 无法在 iSH 运行)
                m = tarfile.TarInfo(rp)
                m.type = tarfile.REGTYPE
                m.size = len(init_blob)
                m.mode = 0o755
                m.uid = m.gid = 0
                m.mtime = 0
                tout.addfile(m, io.BytesIO(init_blob))
            elif p == "etc/resolv.conf":
                m = tarfile.TarInfo(rp)
                m.type = tarfile.REGTYPE
                m.size = len(RESOLV_CONF)
                m.mode = 0o644
                m.uid = m.gid = 0
                m.mtime = 0
                tout.addfile(m, io.BytesIO(RESOLV_CONF))
            else:
                if rp != p:
                    m.name = rp
                    # 链接目标若落在被合并的目录里,一并重写
                    if m.issym() and not m.linkname.startswith("/"):
                        abs_t = norm(os.path.join(os.path.dirname(p), m.linkname))
                        rt = rewrite(abs_t)
                        if rt != abs_t:
                            rel = os.path.relpath(rt, os.path.dirname(rp))
                            m.linkname = rel
                    elif m.issym():
                        rt = rewrite(norm(m.linkname.lstrip("/")))
                        if rt != norm(m.linkname.lstrip("/")):
                            m.linkname = "/" + rt
                    elif m.islnk():
                        m.linkname = rewrite(norm(m.linkname.lstrip("/")))
                if m.isreg():
                    tout.addfile(m, tin.extractfile(m))
                else:
                    tout.addfile(m)
            kept_paths.add(rp)
            stats["kept"] += 1

    missing = [r for r in REQUIRED if r not in kept_paths]
    if missing:
        print(f"FATAL: required paths missing after trim: {missing}", file=sys.stderr)
        os.unlink(dst)
        sys.exit(1)
    print(f"kept {stats['kept']} entries, dropped {stats['dropped']} entries "
          f"({stats['drop_bytes'] / 1e6:.0f} MB uncompressed)", file=sys.stderr)

# 裁剪后如需清理 dpkg 数据库,在 iSH 内执行:
#   oma remove linux-kernel-7.1.7 linux+kernel grub os-prober networkmanager \
#       systemd-base alsa-utils audio-base pciutils usbutils hwdata \
#       sysfsutils lm-sensors pcmciautils kmod man-db groff texinfo
# (不清理也不影响日常使用,只是 dpkg 认为这些包还在)

if __name__ == "__main__":
    main()
