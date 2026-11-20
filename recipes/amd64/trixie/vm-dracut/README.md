# vm-dracut NFS Root

在 GitHub Actions 的 **Build rootfs** 中选择 `amd64-trixie-vm-dracut`。产物包含 rootfs 归档、每个目标内核对应的 `vmlinuz-<版本>` 与 `initrd.img-<版本>`、模块及驱动覆盖报告和 `SHA256SUMS`。将 rootfs 解包到 NFS 导出目录，并通过 iPXE 加载同一构建中的内核和 initramfs。

启动镜像统一使用 systemd-networkd。每个内核只需构建一份镜像，iPXE 传入 `ip=`、`ifname=`、`bridge=`、`vlan=`、`bond=` 和 `rd.route=` 所需参数。可用具名 `ip=` 配置单个接口，或用 `ip=dhcp` 在匹配的物理接口上启用 DHCPv4。镜像不预设任何接口名或地址。NFS 服务器和导出路径由 `root=nfs4:服务器:/导出路径` 显式提供，附加 `rw` 或 `ro` 控制根挂载方式。IPv6 和 DHCP 提供 NFS Root 路径不在验收范围内。

下例使用文档保留地址，请替换成部署参数。静态和 DHCPv4 的两组参数可加载相同文件。

```text
ifname=bootnet:52:54:00:12:34:56 ip=192.0.2.42::192.0.2.1:255.255.255.0::bootnet:none root=nfs4:192.0.2.20:/vm-root rw
ifname=bootnet:52:54:00:12:34:56 bridge=br0:bootnet ip=br0:dhcp root=nfs4:192.0.2.20:/vm-root rw
```

多条 `ip=` 可以同时使用。把 IP 配在承载流量的桥、VLAN 或 Bond 上，成员口随拓扑配置。两条链路均可到达 NFS 服务器时，可使用 `rd.route=<服务器>/32:<下一跳>:<接口>` 约束流量，下一跳必须能从该接口到达。

正式系统的 networkd 使用留在 `/run/systemd/network` 中的启动配置，保留启动网络并管理 DHCP 续租。接管时通过临时 netdev 配置保留现有虚拟接口的 MAC，避免切换机器 ID 时重新计算网桥 MAC。发布产物中的静态检查报告列出实际收录的 dracut 模块、必需驱动和典型硬件的固件覆盖缺口。

## 启动验收

对同一份内核与 initramfs 切换启动参数，记录以下结果。

- 普通接口分别以静态 IPv4、DHCPv4 启动并挂载显式指定的 NFS Root。
- Bridge、VLAN、Bond 分别作为三层出口。使用 `ip -d link` 核对成员关系、VLAN ID 和 MTU。
- 两个接口均可达 NFS 服务器时，`ip -4 route get <服务器>` 的 `dev`、`src` 及适用时的 `via` 与预期一致，实际 NFS 连接使用该路径。
- 对比 initramfs 和正式系统网络服务启动后的拓扑、地址及路由。用 `findmnt /` 检查 NFS 和挂载模式，实际读写验证连接持续可用。DHCPv4 场景额外观察一次续租。
