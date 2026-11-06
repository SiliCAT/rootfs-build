# br-initrd：对已有 rootfs 注入早期网桥支持

主业务集中在 [recipe.sh](recipe.sh)。它消费已有 Debian Trixie rootfs 归档，输出
供通用外壳重新归档的目录，不执行 mmdebstrap，不创建磁盘镜像。

配方不含 CPU 架构分支：工具、库和内核模块均取自输入 rootfs。工作流支持 amd64 和
arm64 原生执行，按输入自动选择 runner、容器平台及固定 digest 的 builder。
armel 等其他架构仍明确拒绝，不能仅因共享脚本就宣称支持。当前依赖 Debian Trixie
的 initramfs-tools，不能据此宣称支持任意发行版或没有内核的容器 rootfs。

## 调用

在准备好工具和挂载权限的 Debian builder 中：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe transforms/trixie/br-initrd/recipe.sh \
  --input-rootfs /absolute/input/rootfs-trixie-amd64-vm-incusd.tar.xz \
  --output /absolute/output/rootfs-trixie-amd64-vm-incusd-br-initrd.tar.xz
```

配方的位置参数是 `TARGET_ROOT INPUT_ARCHIVE`。目标是外壳提供的空绝对路径，
输入在目标目录之外，包含 `etc/`、`usr/`、`var/` 等系统目录，不含额外的 `rootfs/` 层。
最终输出统一为 `.tar.xz`；输入可使用新的 `.tar.xz` 或已有的 `.tar.zst`，由 tar
识别压缩类型。历史输入不会被重命名或改写。
本地直接调用者负责输入的来源和校验；工作流会在进入特权 builder 前验证 Actions
来源及完整的校验清单，并将输入目录挂为只读。工作目录所在文件系统须支持 Linux
权限、所有权、设备节点、ACL 和 xattrs。

`BR_INITRD_REPORT` 是可选的构建记录输出路径，须为目标 rootfs 之外、尚不存在的
绝对文件路径。`run-transform.sh` 将其设为状态目录中的 `br-initrd.report.json`，
记录最终验收的 initramfs 路径、内核版本和 SHA-256；不写入最终系统。
配方调用构建侧 Python 验收器，因此直接使用该配方的 builder 也必须提供 Python 3。

## 注入业务

- 安装 `/etc/initramfs-tools/scripts/init-premount/10-br-exp`。
- 安装 `/etc/initramfs-tools/hooks/20-iproute2`。工具由 `copy_exec` 从目标系统复制到
  initramfs 的私有目录，再创建相对链接，避免写穿 BusyBox applet 链接。
- 请求 `bridge`、`llc`、`stp` 模块。工具包含 ip、sleep、true，以及可用的原生 udhcpc
  或 BusyBox udhcpc 包装。没有 DHCP 客户端时，保留人工/MAC 模式，AUTO 模式不可用。
- 保留原脚本的 `update-initramfs -u` 选择规则，不改成 `-k all`，不自动创建缺失的
  initramfs。必须至少实际重建一个文件。
- 刷新 CA 与 APT，执行 autoremove、残余配置清理和缓存/模板身份清理。这些操作
  可能改变输入包集；最终包清单从加工后的 dpkg 数据库重新生成。
- 在全部包操作结束后回读验收最终 initramfs。若包清理还改写了其他 initramfs，
  将其加入验收；本次选中重建的文件被清理删除时失败。

保留原启动参数语义：

```text
br_exp=1 ip=CLIENT:SERVER:GATEWAY:MASK:HOST:BRIDGE:AUTOCONF
br_if=eth0
br_if_mac=aa:bb:cc:dd:ee:ff
```

`ip=` 第六段指定桥名；`br_if_mac` 优先于 `br_if`，显式提供但为空、无效、重复匹配或
等待失败时均不回退。MAC 模式最多扫描 11 次，等待十次一秒。没有指定下层接口时
通过 DHCP 探测进行 AUTO 选择。`br_exp_debug_shell=1` 保留人工调试行为。
配方不设置设备的 MAC 或启动参数，也不在构建机执行此启动脚本。

## 隔离、临时配置和验收

外壳提供私有挂载命名空间，配方另行追踪并释放自己创建的 dev/proc/sys 视图。
不绑定 runner 的 `/lib/modules`、`/usr/src`，不根据 runner 的 `uname -r` 选择内核。

收尾会记录各视图及子挂载的路径、类型、来源、mount ID、父 ID 和传播属性，
保留 mountinfo 的路径转义，诊断信息随构建日志保存。确认根挂载 ID 与创建时一致、
子挂载属于该根且整棵子树为私有后，递归卸载加工期间产生的嵌套/叠加挂载，
再检查整个视图已消失。根挂载被替换、归属无法确认、非私有挂载或真实卸载失败
仍会阻止归档发布，不使用 lazy/force 卸载掩盖失败。视图之外的挂载不会被清理。

包操作期间临时准备 DNS 和返回 101 的 `policy-rc.d`；结束后恢复输入中原有的文件、
权限和符号链接，原本不存在时删除临时文件。常规失败也尝试恢复与卸载；被强制 KILL
或平台销毁时不能保证清理，失败现场不发布为成功产物。

验收会解开最终重建的 initramfs，核对桥脚本字节、工具及入口、DHCP 分支、桥模块
或内建支持，以及目标模块 vermagic。归档外壳随后执行挂载检查、压缩完整性、目录
比较和 SHA-256 验证。实际启动、网络设备发现和 DHCP 行为仍须启动测试。

输入安装了 raspi-firmware 时，最终还会调用公共验收器比较 /boot/firmware 的内核、
initramfs 副本与 /boot 原文件，并核对配套 DTB。副本失同步直接失败，不自行改写
实例 cmdline 或改变 `update-initramfs -u` 的内核选择规则。

memini、vm-incusd、vm-sandbox 和 arm64 rpi-incusd 共用本配方及统一的 Build rootfs 基础入口。
可从候选清单选择 vm-sandbox 的 artifact ID，输出名称保留 `vm-sandbox-br-initrd`。

GitHub 入口、来源 ID 获取及最终产物说明见 [加工工作流](../../../docs/rootfs-transform.md)。
