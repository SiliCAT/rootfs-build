# memini：Debian Trixie / amd64 / ZFS

`recipe.sh` 来自原始 `trixie-zfs.sh`。保留原包集、APT 源、ZFS pin、系统配置、
UID 为 `1000033` 的 `silicat` 用户和清理策略。主要修改均在这一个配方文件中完成，
没有配方继承或自动叠加层。

## 调用接口

```text
recipe.sh TARGET_ROOT
recipe.sh --help
```

- `TARGET_ROOT` 是调用者创建的绝对路径，必须是真实、可写的空目录，包括隐藏项也必须为空。
- 构建以 root 身份执行，使用 mmdebstrap 的 `root` 模式和 `directory` 格式，保留 `minbase`。
- 不接收其他业务参数，不依赖调用者当前目录或外壳私有变量。
- 成功时目录直接包含 `etc/`、`usr/` 等 rootfs 内容，不再包含镜像载体或额外的 `rootfs/` 包装层。
- 只有全部构建、目标内核检查和收尾成功才返回零。失败目录仅供诊断，不能用于发布或直接重试。

## 构建环境与调用者的职责

工作流准备阶段负责安装 mmdebstrap、APT、Debian archive keyring、CA 证书以及 GNU
coreutils、findutils 等构建工具，并保证 root 构建所需的 chroot、挂载和命名空间能力。
配方缺少构建工具时直接失败，不在宿主机运行依赖安装或刷新宿主 CA。

优先使用原生 amd64 builder。若使用其他宿主架构，工作流还须准备并验证 amd64
binfmt/QEMU 执行能力；本配方不安装或配置模拟器，也未验证模拟构建。

调用者管理构建目录、日志、取消与失败恢复。必须等待配方结束，确认没有残留子挂载，
再验证和打包。不要在仍有构建进程写入时删除目录。

禁止将 runner 的 `/lib/modules` 或 `/usr/src` 绑定到目标 rootfs。
这些目录中的目标内核和 headers 必须由配方安装。

## 配方拥有的内容与执行顺序

1. 安装完整的 Trixie 包集，使用原有 main、updates、security、backports 源和 ZFS pin。
2. 执行 locale、时区、用户、SSH、sudo、模块、zram 和服务策略配置。
3. 完成包操作后，检查 rootfs 中每个内核对应的 image 和 headers，显式运行该版本的
   DKMS autoinstall，验证 ZFS 安装状态、模块路径和 vermagic，生成或更新对应 initramfs。
4. 检查 dpkg 状态，清理缓存、身份、服务状态、临时文件、日志和 history。
5. 等 mmdebstrap 成功返回并完成其服务策略恢复后，再删除 `policy-rc.d`，重置
   machine-id 并重复必要的最终清理。

包集一包一行，APT 源和所有 hook 均保留在 `recipe.sh`，便于人工修改和审阅。
这里的目标包安装与工作流准备阶段的构建工具安装是两个不同的职责。

## 检查的边界

目标内核版本取自 rootfs 的模块目录并核对已安装的内核包，不使用 runner 的 `uname -r`，
也不向 runner 加载 ZFS 模块。DKMS 构建会复用包安装钩子已完成的结果。

本配方保持原包集，没有额外添加 `zfs-initramfs` 或改变设备的启动方案。
检查 initramfs 存在并不等于验证了 ZFS 根文件系统启动。
真实的模块加载、系统启动和 memini 硬件行为需要单独的虚拟机或设备测试。

归档、文件元数据保留、产物校验和、包清单导出以及 GitHub 上传由外壳和工作流负责，
不属于此配方的接口。
