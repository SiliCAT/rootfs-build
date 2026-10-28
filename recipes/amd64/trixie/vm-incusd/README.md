# vm-incusd：Debian Trixie / amd64 / Incus 与来宾工具

这是独立的完整 rootfs 配方，目标 ID 为 `amd64-trixie-vm-incusd`。
包集、源、配置及收尾都在 `recipe.sh` 中定义，没有配方继承。
使用 Debian 官方软件源和 `linux-image-amd64`，没有引入 PVE 仓库或专用内核。

## 运行

在已准备好依赖和权限的构建环境中，从仓库根目录运行：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe recipes/amd64/trixie/vm-incusd/recipe.sh \
  --output outputs/rootfs-trixie-amd64-vm-incusd.tar.zst
```

也可在 Actions 的 **Build rootfs → Run workflow** 中选择 `amd64-trixie-vm-incusd`。
继续使用现有构建容器、私有挂载命名空间、归档外壳和产物记录导出。

配方只接收一个绝对、真实、可写的空 `TARGET_ROOT`；它不安装 builder 工具，不创建
镜像或归档，也不依赖特定 cwd。失败目录保留供诊断，不能作为成功产物或直接重用。

## 包集和运行期策略

44 个显式包按一包一行保留在 `recipe.sh`。
包含 `incus-base`、`apparmor`、`apparmor-utils`、`qemu-guest-agent`、`open-vm-tools`。
不安装 `systemd-container`。没有加入 ZFS、内核 headers 或 DKMS 构建业务。

- 保留 main、updates、security、backports 的四个 suite，均启用四个 components，
  以及 backports 的 deb-src；使用 HTTPS 和正常 Debian 签名校验，不设置 backports pin。
- 保留 `silicat` 用户、UID `1000033`、禁用密码、不创建 home、sudo 组和 NOPASSWD 规则。
- root 密码无条件清空并锁定，失败中止构建。
- SSH 禁止 root、密码及键盘交互登录，清除构建期 host keys。
- 生成 `en_US.UTF-8`，使用 `update-locale LANG=en_US.UTF-8` 设置默认语言。
- 时区通过 `US/Eastern` 和发行版 reconfigure 流程设置；tzdata 可规范化别名。
- 保留 NBD、overlay、USB/IP、`nbds_max=1`、`RESUME=none`、zram 及 volatile journald 配置。
- 屏蔽 rsyslog、两个来宾服务，以及五个 Incus service/socket 单元；安装软件包不意味着
  模板会自动启动这些服务。

mmdebstrap 使用 `root` 模式、`directory` 输出和 `minbase`。
省略 `--architectures`，按 builder 的 Debian 包架构原生构建；当前目标和
`linux-image-amd64` 仍限定 amd64，没有跨架构支持。

## 仅构建期间的裁剪

APT 临时片段为 `/etc/apt/apt.conf.d/90-vm-incusd-build`，关闭语言索引、Recommends 和
Suggests。DPKG 通过原生 `--dpkgopt` 写入 `/etc/dpkg/dpkg.cfg.d/99mmdebstrap`，排除：

```text
/usr/share/doc/*
/usr/share/man/*
/usr/share/locale/*
```

使用原生选项可覆盖 mmdebstrap 的最早解包阶段；builder 已包含 `mmtarfilter` 和 Python。
没有 copyright、locale.alias 例外，也不增加其他目录排除。
locale 生成可能提示 locale.alias 缺失；验收仍须确认 `en_US.UTF-8` 实际生成且默认 LANG 正确。
APT/DPKG 规则值只定义一次，由配方内部导出给 hooks，不是调用者的业务输入。

规则持续覆盖安装、reconfigure、CA/APT 刷新、autoremove 和 rc 包清除。最后一个
customize hook 比对文件内容，再精确移除这两个本次生成的配置。
发现配置被替换或写入了其他规则时直接失败，不删除不明配置，也不改写发行版默认值。
删除规则不会补回已经省略的文件；以后安装、升级遵循系统正常策略。

## 收尾和验收

按 memini 的严格处理方式执行 CA 刷新、APT update、autoremove、分步 rc 状态包清除、
缓存与状态清理。全部包操作完成后才清除 Incus 状态、持久 journal、SSH keys、
machine-id、random-seed、DHCP 状态及 history。临时目录包括隐藏项，保留目录本身。
所有关键错误都会传播，缺少可清理对象属于正常情况。

临时 APT/DPKG 配置移除后，mmdebstrap 才返回；随后再次清理并验证 `policy-rc.d`，
重置 machine-id 并执行必要的最终状态清理。不创建 `/etc/build-meta`。

`verify.sh` 是验收脚本，不定义新的配置。归档外壳自动调用它，核对最终包状态、默认语言、
时区、用户、服务 mask、内核/initramfs、临时配置移除及运行状态清理。
还核对 essential 阶段的 coreutils 和后续安装的 curl 文件确实被裁剪。
没有向 runner 挂载 `/lib/modules`、`/usr/src` 或加载目标模块。

验收不包含系统启动、来宾代理通信、Incus 初始化或实际设备行为测试。
