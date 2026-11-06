# rpi-incusd：Debian Trixie / arm64 / Raspberry Pi、ZFS、Incus

主业务集中在 [recipe.sh](recipe.sh)，与其他配方平级、独立维护，不继承 memini 或
vm-incusd。输入是一个空的绝对目录，输出是已完成配置和清理的 rootfs；通用外壳负责
私有挂载命名空间、验收及最终 `.tar.xz` 归档，不创建 XFS 镜像、分区或 FAT。

## 工作流入口

从 **Actions → Build rootfs → Run workflow** 选择 `arm64-trixie-rpi-incusd`。
共用入口自动使用 `ubuntu-24.04-arm` 和 `linux/arm64` Debian Trixie builder。
固定的 builder digest 是同时包含 amd64、arm64 的多架构索引；不使用 QEMU。
解析和候选查询作业仍可运行在 amd64 runner，因为它们不执行目标系统程序。

基础产物名为 `rootfs-trixie-arm64-rpi-incusd-RUN_ID-ATTEMPT`，内含同名前缀的归档、
SHA-256、包清单、构建记录及校验清单。Summary 和诊断包中的回执与其他目标一致。
`List rootfs inputs` 会列出成功且未过期的新目标；在 `Transform rootfs` 填入其
artifact ID 后，自动选择 ARM runner 和公共 br-initrd 配方，输出
`rootfs-trixie-arm64-rpi-incusd-br-initrd.tar.xz`。

## 保留的业务

- 包集取自原 arm64 构建脚本，去重 `firmware-misc-nonfree`；安装 Debian
  `linux-image-arm64`、`linux-headers-arm64`，以及 backports 的 ZFS utils/DKMS。
- 保留 Incus、AppArmor、存储和网络工具；`raspi-firmware` 仍在单独的安装阶段安装。
  不额外加入 VM 来宾工具、NFS 服务端、rsyslog、logrotate 或无线专用包。
- 保留原 main/backports 源范围和组件、backports deb-src 及 ZFS pin；源地址统一为
  HTTPS。没有借迁移额外加入 updates/security 套件或临时文件裁剪业务。
- 保留 `silicat` UID 1000033、sudo、SSH、root 锁定、en_US.UTF-8 locale 生成、
  US/Eastern 时区、NBD/overlay/USBIP、无 resume、ZRAM、易失日志策略。
  不额外修改默认 LANG。
- 明确保留五个 Incus 服务/socket mask，以及原有 rsyslog、qemu-guest-agent、
  open-vm-tools mask；不初始化 Incus 存储池、网络或运行状态。
- 不写入 root subordinate ID 范围，不写入 `90-zfs-arc-rpi4-4g.conf`；它们属于
  实例初始化。账户管理工具自行生成的其他正常配置不作整文件清空。

## 内核、initrd、DTB 边界

内核和 DTB 来自目标 Debian 包，不从 runner 导出设备树，不重编译内核。
ZFS 只对 rootfs 内实际安装的内核和 headers 构建，检查 DKMS 状态、模块路径及
vermagic；不挂入 runner 的 `/lib/modules` 或 `/usr/src`，不加载模块。

显式保留 `MODULES=most`，对目标版本生成 initramfs，避免按 runner 的硬件裁剪。
验收包含对应内核的模块及 NFS 启动脚本/模块；不写入实例 IP、NFS 导出路径或 MAC。
initrd 内部压缩沿用 Debian 策略，最终 rootfs 归档独立使用 XZ。

`/boot/firmware` 是普通目录布局。Debian hook 复制 kernel、initrd 和 DTB；验收比较
副本与 `/boot`、目标内核包内的 DTB 集合，并检查 kernel/initrd 的版本配对。
公共 br-initrd 在全部包操作及重建结束后也检查这些副本，旧副本使加工失败。
自动生成的 config.txt 只检查文件引用，cmdline.txt 不作为实例启动配置验收；
其 runner 自动探测参数不定义本项目的部署约定。

DTB 按板型提供一组文件，不承诺一个 DTB 适用于所有树莓派。该配方不设计启动服务端、
发布启动文件或配置运行期间的 boot 挂载。实际设备启动及网络行为需另行验证，构建
记录保持 `boot_tested: false`。

## 清理与失败处理

按现有配方规范严格处理失败：全部包操作、目标内核及 initramfs 更新完成后，再清除
Incus 状态、SSH host keys、机器身份、随机种子、DHCP 状态、APT 列表、临时文件
（包括隐藏文件及 `/var/tmp`）、日志和 history。不保留 `/etc/build-meta`。
mmdebstrap 成功返回后再删除并检查临时 `policy-rc.d`，然后执行独立的
[verify.sh](verify.sh)；任何验收失败都不发布成功归档。
