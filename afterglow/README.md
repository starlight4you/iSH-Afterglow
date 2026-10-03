# 在 iSH 上运行 Afterglow OS(星霞 OS)— 研究与实现

## 结论

**可行,已验证。** Afterglow OS 13.3.1(i486)的完整用户态可以在 iSH 里直接运行,
包括 bash、python3(多线程)、perl、vim、oma/apt/dpkg 包管理、curl/wget 网络访问。
本目录包含全部所需产物:裁剪脚本、最小 init、iSH 兼容性补丁,以及裁剪后的
`afterglow-ish.tar.gz`(223MB,可直接被 iSH App 导入)。

### 关键架构事实(决定了整个方案)

**iSH 不是整机模拟器,不引导任何 Linux 内核。** 它在用户态解释/JIT 执行 i386 指令,
并把 Linux 系统调用翻译到 iOS/Darwin(`ish/kernel/calls.c` 的 syscall_table 约 240 项,
`ish/kernel/uname.c:25` 上报 `4.20.69-ish`)。所以:

- "给 Afterglow 裁剪内核" 在 iSH 场景下 = **整个内核包都是死重,直接不打包**
  (内核镜像 + 模块约 120MB)。真正运行的"内核"是 iSH 的 syscall 翻译层。
- Afterglow 的 init(systemd)无法运行(需要 cgroups/netlink/真实挂载),
  用一个 3.7KB 的静态最小 init(`init.c`)替换 `/sbin/init` 即可。
- 若想在 iOS 上跑 Afterglow 自己的真实内核,需要用 UTM(QEMU)而不是 iSH。

## 目录内容

| 文件 | 说明 |
|---|---|
| `ish/` | iSH 源码(upstream master + 5 个补丁,`git diff` 可见) |
| `afterglow_base_20260808_i486.tar.xz` | 官方 Afterglow base 镜像(南大镜像站下载,sha256 已校验) |
| `afterglow-rootfs/` | 官方镜像的解包副本(调试用,可删) |
| `init.c` / `init` | 最小 PID 1 源码及静态编译产物(i386,3.7KB) |
| `trim-rootfs.py` | 裁剪脚本:tar.xz → iSH 可导入的 tar.gz |
| `afterglow-ish.tar.gz` | **最终产物**,裁剪后 rootfs(约 223MB,iSH App 可直接导入) |

## 复现步骤

```bash
# 1. 生成裁剪 rootfs(init 二进制由 zig cc 编译,见 init.c 头注释)
python3 trim-rootfs.py afterglow_base_20260808_i486.tar.xz afterglow-ish.tar.gz

# 2. macOS 上端到端验证(需要先把 iSH 构建为 CLI,见下文"本地调试")
tools/fakefsify afterglow-ish.tar.gz /tmp/agfs   # 与 App 首次启动完全相同的导入路径
./build/ish -f /tmp/agfs /bin/login -f root      # 或 /bin/bash

# 3. 打进 iSH App:两种方式
#    a) 构建时替换:把 afterglow-ish.tar.gz 传到某 URL,修改 ish/app/iSH.xcconfig
#       的 ROOTFS_URL 指向它(Xcode "Download Root" phase 会拉取为 root.tar.gz)
#    b) 直接把 tar.gz 拷到 iOS 设备,App 内 Settings → Filesystems → Import
#       (iSH 导入仅支持 tar/tar.gz,见 ish/tools/fakefs.c:106)

# 4. Xcode 打开 ish/iSH.xcodeproj,选自己的签名 Team(DEVELOPMENT_TEAM),
#    构建到设备即可。默认 Boot Command /sbin/init 与 Launch Command
#    /bin/login -f root 与本 rootfs 完全匹配,无需改动。
```

## iSH 侧的 6 个补丁(glibc 2.42 / Rust 用户态 / 模拟器运行必需)

Afterglow 是 glibc 2.42 + 64 位 time_t 的较新用户态,暴露出 iSH 的几个真实缺陷:

1. **`kernel/futex.c` — 实现 FUTEX_WAIT_BITSET / FUTEX_WAKE_BITSET**
   glibc 2.42 在 `futex_time64` ENOSYS 后回退到 `futex(FUTEX_WAIT_BITSET|REALTIME)`,
   iSH 原本不支持(返回 ENOSYS)导致 glibc 直接 fatal:
   "The futex facility returned an unexpected error code"。
   WAIT_BITSET 与 WAIT 的区别是绝对超时 + 时钟选择,补丁做了绝对→相对转换;
   bitset 参数忽略(允许伪唤醒,符合 futex 语义)。
2. **`fs/tty.c/h` — 实现 TCGETS2/TCSETS2/TCSETSF2/TCSETSW2 ioctl(termios2)**
   glibc 2.42 的 isatty/tcgetattr 走 termios2;没有它 `login`、交互 shell 的
   readline、`stty` 全部挂掉。顺带把控制台默认 cflags 从 0 改为
   CS8|CREAD|HUPCL|B38400(原来 stty 显示 speed 0)。
3. **`fs/sock.c/h` — setsockopt 接受 IP_RECVERR 为空操作**
   glibc resolver 给 DNS socket 设置 IP_RECVERR,失败(EINVAL)则放弃整个解析。
4. **`kernel/exit.c` — halt_system 跳过正在退出/非线程组领导者的任务**
   PID 1 退出时向所有任务发信号的扫描存在 UAF 竞争(oma 等 Rust 程序退出时
   100% 宿主段错误)。
5. **`app/AppGroup.m` — App Group 容器不可用时回退到 App 自身容器**
   无签名/模拟器构建取不到 group container 会在启动时直接崩
   (RootsDir 拿到 nil URL);真机签名后走原路径,补丁只是兜底。
   另:`tools/fakefs.c` 的导入报错现在带具体路径,方便诊断。
6. **`fs/sock.c` — socketpair 传转换后的基础类型**
   原来把含 Linux 标志位(SOCK_NONBLOCK/SOCK_CLOEXEC)的 type 原样传给
   Darwin,Darwin 直接拒绝(EPROTONOSUPPORT);oma(Rust/tokio)等程序
   启动时创建 UnixStream pair 即 panic:"Protocol not supported"。
   (neofetch 不存在于 base 镜像——你看到的 neofetch 报错其实是
   bash 的 command-not-found 钩子调起 oma 撞上了同一个 bug。)

以上 1-4、6 对 Alpine(musl)用户态同样有益,适合上游提交。

## 裁剪明细(tar 流级别,元数据完整保留)

| 删除内容 | 原因 | 解压后体积 |
|---|---|---|
| linux-kernel-7.1.7 + linux+kernel | iSH 不引导内核 | ~120MB |
| /usr/lib/locale/locale-archive | 全 locale 归档;C/C.UTF-8 内置于 glibc,其余可运行时 `localedef` 再生成 | 222MB |
| /usr/share/locale 非中英条目 | 只留 zh_CN/zh_TW/zh_Hant/en* | ~100MB |
| grub, os-prober | 无引导过程 | ~30MB |
| networkmanager, systemd-base | 需要 netlink;iSH 网络由 iOS 透明提供 | ~21MB |
| alsa-utils/audio-base, pciutils, usbutils, hwdata, sysfsutils, lm-sensors, pcmciautils, kmod | 无真实硬件 | ~20MB |
| /usr/lib/systemd, /etc/systemd, udev | init 已替换;注意 **libsystemd.so.0 保留**(login/PAM 链接它) | ~15MB |
| man-db, groff, texinfo + doc/man/info | 文档(`--keep-docs` 可保留) | ~55MB |
| /usr/src、/boot 内容、/etc/profile.d 悬空链接 | 内核构建残留等 | ~70MB |

规则实现注意点:包级删除遵循 dpkg 语义(路径的所有登记包都被删才删,
且保护被保留文件的父目录,否则 /usr/share 这类共享目录会被误删);
指向已删路径的符号链接一并删除(收敛到不动点)。

**大小写冲突处理**(模拟器实测踩出来的坑):fakefs 元数据按路径精确匹配
(区分大小写),数据层落在宿主文件系统上(大小写语义不定且不一致),
唯一稳妥做法是 tar 内零冲突。目录冲突选"赢家"合并(子项重写):
terminfo 的 X/x 等 8 对按惯例取小写(xterm-256color 必须在 x/ 下,
否则 ncurses 找不到);perl 的 sys/Sys、pod/Pod 两对例外取大写
(Sys::Syslog、Pod::* 是模块查找路径);文件级冲突(内核头文件
ip6t_HL.h/ip6t_hl.h、iptables 模块等)丢弃后来者。

**locale**:删掉 222MB 的 locale-archive 后,用 iSH CLI 在 fakefs 里跑了
`localedef` 预生成 C.UTF-8 / zh_CN.UTF-8 / en_US.UTF-8(消除 bash 的
setlocale 警告),再 unfakefsify 导出回 tar.gz——所以产物里 locale 是好的。

**不动的部分**:python-3(228MB)被 cracklib→libpwquality→PAM 与 debconf 硬依赖,
强行删除需要改 PAM 配置,列为可选的进一步激进裁剪;vim/perl/开发头文件保留。
dpkg 数据库未改动(依赖完好);如想彻底干净,在 iSH 里跑一次
`oma remove linux-kernel-7.1.7 grub networkmanager ...` 即可。

## iOS App 构建与模拟器运行(已验证)

**直接用 Xcode 打开 `ish/iSH.xcodeproj` 构建即可**(含空格路径也没问题,
相关缺陷已修)。工程里做了这些改动:

- `app/Project.xcconfig`:部署目标 11.0 → 15.0(Xcode 27 最低支持 15.0);
  `deps/libarchive.xcodeproj` 同步修改
- `tools/staticdefine.sh`:重写为 python 直接按参数数组调编译器
  (原来 `command.split()` 会把含空格路径切碎,导致 cpu-offsets.h 为空、
  汇编报一堆 `FIBER_BLOCK_code` 未定义)
- `app/xcode-meson.sh`:`meson "$SRCROOT"` 加引号;去掉 `sudo -i printenv PATH`
  (Xcode 里会挂起等密码),改为直接补充常见 brew 路径;xcode-ninja.sh 同理
- `iSH.xcodeproj`:`Generate APK Repositories`、`Compile JavaScript` 阶段加引号;
  两处 `Download Root` 阶段改为:**优先使用仓库上级目录的 `afterglow-ish.tar.gz`**
  (即本目录的产物),不存在时才回退下载 Alpine

VDSO 需要一个能出 i386 ELF 的 clang+lld;本机 brew 装不了 llvm
(/opt/homebrew 部分目录属主是 uid 502),用 zig 伪装:
`/opt/homebrew/opt/llvm/bin/clang` → `zig cc -target x86-linux-musl -mcpu=i486`
(vdso/meson.build 会在这个硬编码路径找到它;zig 本体在 /opt/homebrew/opt/zig-ish)。

模拟器运行(实测通过):构建 → `xcrun simctl install booted <iSH.app>` →
`launch`。首次启动导入 rootfs 约 1-2 分钟,进入 Afterglow root shell,
locale/terminfo/profile 全干净。模拟器无签名时 App Group 容器可能拿不到,
`app/AppGroup.m` 已加回退(用 App 自身容器)。

## 验证记录(macOS 上 iSH CLI + fakefs,与 App 同一代码路径)

- `uname` = Linux 4.20.69-ish i686;os-release = Afterglow 13.3.1 ✓
- `/bin/login -f root` → `id` = uid=0(root) ✓(pty 下完整登录链路)
- python3 ThreadPoolExecutor 8 线程 ✓;perl ✓;`oma --version` ✓(退出无段错误)
- DNS(getent 走 223.5.5.5)✓;`curl https://mirrors.nju.edu.cn/` → 200 ✓
- 裁剪确认:内核模块/systemctl/nmcli/lspci/grub 均不存在 ✓

## 已知限制(都是 iSH 架构固有的)

- 无 AF_NETLINK:`ip`/`ss`/NetworkManager 永远不可用(网络由 iOS 管,无需配置)
- inotify 是 ENOSYS stub:文件监控类工具退化为轮询或失败
- io_uring/memfd_create/clone3 缺失:glibc/应用自动回退,已实测无碍
- 32 位 time_t 回退路径存在 Y2038 隐患(2038 年前无需担心)
- 性能:解释+JIT 模拟,i486 目标码反而比 i686 略快;CLI 操作流畅,编译类重负载慢
- fakefs(SQLite)下 766MB rootfs 正常;首次导入耗时约 1 分钟级

## 本地调试环境备忘

本机 brew 因 /opt/homebrew/share/{pwsh,zsh} 属主是 uid 502 而无法安装 llvm;
iSH 的 VDSO 需要能输出 i386 ELF 的 clang+lld,改用 zig 伪装:

- `/tmp/fakecc/clang` → `zig cc -target x86-linux-musl -mcpu=i486`(剥掉 -fuse-ld)
- 工作区路径含空格会弄断 iSH 构建(staticdefine.sh),故构建在 /tmp/ishbuild
- 调试构建:`PATH=/tmp/fakecc:$PATH meson setup build -Dlog_handler=stderr -Dlog=strace`
