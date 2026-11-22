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
- 构建期裁剪手册、文档和翻译，保留包的版权文件及 `/usr/share/locale/locale.alias`。
  构建期 `policy-rc.d` 返回 101，归档前删除；运行期版本由实例初始化注入。
  实例初始化完成前不得执行可能触发服务启动的软件包操作。
- 归档阶段会检查包状态、关键命令、服务策略和构建清理。

## 区域环境

统一 Debian Trixie 区域环境：显式安装 `tzdata`、`locales`，生成 `en_US.UTF-8`，
通过 `update-locale --reset LANG=en_US.UTF-8` 设置 `/etc/locale.conf`，并保留 `C.UTF-8`。
`/etc/localtime` 指向规范时区 `America/New_York`，由 `dpkg-reconfigure tzdata`
完成配置；不主动创建 `/etc/timezone` 或读写旧式 `/etc/default/locale`。
不设置全局 `LC_ALL`。构建验收检查软件包、locale 生成和字符集、默认配置、
`locale.alias`、时区链接、冬夏时区行为及 tzdata 的 zoneinfo 文件完整性。
schroot 实例的会话环境初始化（例如 profile.d）由部署方负责，不进入母版；
宿主不应通过 copyfiles 或 bind mount 覆盖母版的时区及 locale 数据。

## 公共清理 C01–C09

遵循 [公共清理技术规范](../../../../docs/rootfs-common-cleanup-spec.md)，无配方专属清理。
`scripts/rootfs-cleanup.py` 在 builder 上运行，T1 为最后 customize hook，T2 为
mmdebstrap 返回后的离线兜底；最终 verify 只检查，不修复。清理必须持有由空目录预检签发的本次构建授权记录。不向母版安装清理模块或自制 firstboot。
所有 APT/DPKG 操作先于 T1 文件清理；保护显式包，严格 update，autoremove、rc purge、clean、audit。
hostname、machine-id、resolv.conf 均为标准空文件，hosts 统一为 localhost 基线。
实际部署需由现有会话配置提供 DNS。open-iscsi 2.1.11-1+deb13u2 采用原生
GenerateName=yes 标记；版本或原生能力不符则失败，不在构建中生成正式 IQN。
未知或未验证安全语义的配置/状态拒绝发布而非盲删。本配方没有启动镜像；
遇到启动镜像拒绝放行，待镜像读取器实现后再推广。实际构建及原生 IQN 生成测试仍需验证。

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
