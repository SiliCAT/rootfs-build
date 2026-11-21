# schroot-maintenance

Debian Trixie amd64 的 schroot 离线维护工具环境。用于检查磁盘镜像、文件系统、
分区表和存储元数据，也可在网络可达时查询远程存储服务和访问 Incus。

## 构建

已登记的目标是 `amd64-trixie-schroot-maintenance`。在已准备好的原生 amd64
构建容器内，以 root 身份执行：

```sh
mkdir -p /build
scripts/run-target.sh amd64-trixie-schroot-maintenance /build
```

在 `/build/output` 中生成 `rootfs-trixie-amd64-schroot-maintenance.tar.xz`、
包清单、构建记录和校验文件。也可在统一的 **Build rootfs** 工作流中选择该目标。
该配方的归档用于 schroot。

## 包与配置

- 包清单位于 `recipe.sh` 的 `PACKAGES`，构建基于 mmdebstrap `minbase`。
  Incus 使用 `incus-client`。`partprobe` 由 `parted` 提供，`sfdisk` 由 `fdisk` 提供。
- APT 保留 Trixie、updates、security、backports 的二进制与源码源，均启用
  `main contrib non-free non-free-firmware`。成品清理索引和缓存，首次使用时运行
  `apt-get update`。关闭翻译索引，保留 Debian 的推荐包安装策略。
- 构建期裁剪手册、文档和翻译，保留包的版权文件。预设 `C.UTF-8` 和 UTC。
  成品的 `policy-rc.d` 返回 101，供遵循 Debian 服务启动策略的软件包使用。
- 归档阶段会检查包状态、关键命令、服务策略和构建清理。

## sudo

用户由宿主提供 `/etc/passwd`。schroot 内识别为 `sudo` 组成员的用户可以免密
执行 `sudo`。组成员资格由 schroot 可见的组信息决定。镜像中的 root 密码锁定。

## 使用范围

可直接对普通镜像文件运行 `sfdisk --dump`、`blkid -p`、`debugfs`、
`unsquashfs` 或 `qemu-img info`。操作真实设备、创建映射和挂载需要宿主提供
相应的设备、驱动和权限。进程与网络诊断所见范围由宿主的 schroot 配置决定。

`nbd-client -l` 可枚举允许列出的 NBD 导出，`iscsiadm` 支持 SendTargets
发现，`showmount -e` 可查询提供 MOUNT 服务的 NFS 服务器。连接和挂载
远程存储还取决于宿主配置。
