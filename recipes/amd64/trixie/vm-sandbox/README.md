# vm-sandbox：Debian Trixie / amd64 / Ceph 客户端与来宾工具

独立的完整 rootfs 配方，目标 ID 为 `amd64-trixie-vm-sandbox`。以 vm-incusd 的业务
为参考，只调整指定包集和 Incus 专用策略；包集、配置及收尾集中在 `recipe.sh`，
不在运行时引用或继承其他配方。使用 Debian 官方源与 `linux-image-amd64`。

## 构建和加工

GitHub 统一入口为 **Actions → Build rootfs → Run workflow**，对应
`.github/workflows/rootfs.yml`。在 `target` 下拉列表选择 `amd64-trixie-vm-sandbox`，
复用 `rootfs-build.yml` 完成依赖准备、测试、归档、元数据、Summary 和 JSON 回执。
memini、vm-incusd 和 vm-sandbox 共用这个手动入口。

本地可在已准备好工具及挂载权限的原生 amd64 Debian builder 中运行：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe recipes/amd64/trixie/vm-sandbox/recipe.sh \
  --output outputs/rootfs-trixie-amd64-vm-sandbox.tar.xz
```

配方只接收空的、真实且可写的绝对路径 `TARGET_ROOT`；外壳负责私有挂载命名空间、
通用验收和压缩包发布。配方不安装 builder 工具，不依赖特定 cwd。

成功的构建产物会出现在 **List rootfs inputs** 中。将其 artifact ID 填入
**Transform rootfs** 即可使用共享的 br-initrd 加工，输出名称为
`rootfs-trixie-amd64-vm-sandbox-br-initrd.tar.xz`。来源校验接受统一基础入口的登记目标，并兼容旧专用入口已有的成功产物；不接受加工结果作为基础输入。
更新默认分支后，GitHub Actions 的目标列表即可显示 vm-sandbox。

## 包集和系统策略

42 个显式包按一包一行维护。相对 vm-incusd，去掉 `incus-base`、`apparmor`、
`apparmor-utils`，增加 `ceph-common`。保留 qemu-guest-agent、open-vm-tools，以及其余
原有包；Ceph 的 Python、librados、librbd 等依赖交给 APT 解析。
不增加 Ceph 服务端包、ZFS、内核 headers 或 DKMS 构建业务。

- 保留 Ceph 包的 Debian 默认行为：`ceph.target`、`rbdmap.service` 默认启用，保留
  `ceph` 系统用户、包配置、目录和权限。配方不额外 mask、disable 或改写 Ceph 单元。
- 不预置集群地址、认证 keyring 或设备映射，不把构建环境当作 Ceph 客户端连接集群。
  `ceph.target` 是分组单元；rbdmap 按部署后的 `/etc/ceph/rbdmap` 和挂载配置工作。
- 去掉五个 Incus service/socket 的 mask，以及 Incus 状态清理与验收。
- 保留 rsyslog、qemu-guest-agent、open-vm-tools 的 mask，仍将来宾服务留给部署时启用。
- 保留四个 suite、四个 components、backports deb-src 和 HTTPS/签名校验，不新增 Ceph
  上游源或版本 pin。
- 保留 `silicat` 用户、UID `1000033`、sudo 及 NOPASSWD；root 密码清空并锁定；SSH
  禁止 root、密码及键盘交互登录，并清除 host keys。
- 保留 en_US.UTF-8 和默认 LANG、US/Eastern 时区语义、NBD/overlay/USB/IP、zram、
  RESUME=none 及 volatile journald。

## 临时裁剪与收尾

mmdebstrap 使用 root 模式、directory 输出、minbase；按原生 builder 架构构建，
当前目标和内核包限定 amd64。

仅构建期间使用 `/etc/apt/apt.conf.d/90-vm-sandbox-build` 关闭语言索引、Recommends 和
Suggests；通过原生 `--dpkgopt` 排除 `/usr/share/doc/*`、`/usr/share/man/*`、
`/usr/share/locale/*`，覆盖最早解包阶段。末尾比对本次生成的 APT/DPKG 配置，再精确
移除；不删除包自身的其他策略。后续安装及升级遵循 Debian 默认策略。

继续严格执行 CA/APT 刷新、autoremove、残余配置清除、缓存和模板身份清理，以及
mmdebstrap 返回后的 policy-rc.d 删除与最终收尾。清空日志而保留 Ceph 目录及权限，
不删除 `/var/lib/ceph`。所有关键错误均传播，不创建 `/etc/build-meta`。

## 验收范围

`verify.sh` 自动检查 Ceph 客户端、来宾工具、内核包状态；拒绝安装被移除的三个包及
systemd-container。离线检查 Ceph 两个单元保持 enabled，检查包用户和目录仍存在，
并运行 `ceph --version`、`rados --version`、`rbd --version`，确认 mount.ceph 可执行。

同时保留语言、时区、UID/sudo、root 锁定、服务 mask、临时配置移除、裁剪、状态清理、
dpkg audit 和目标内核/initramfs 的验收。不会加载 runner 或目标内核模块。

构建验收不连接 Ceph 集群，不证明实际 CephFS 挂载、RBD 映射、来宾通信或系统启动成功。
