# rootfs-build

按独立配方构建 rootfs，并交付保留 Linux 文件元数据的 `tar.xz` 压缩包。

- [构建外壳与产物接口](docs/artifacts.md)
- [GitHub Actions 构建](docs/github-actions.md)
- [消费已有产物的 br-initrd 加工工作流](docs/rootfs-transform.md)
- [memini：Debian Trixie / amd64 / ZFS 配方](recipes/amd64/trixie/memini/README.md)
- [vm-incusd：Debian Trixie / amd64 / Incus 与来宾工具配方](recipes/amd64/trixie/vm-incusd/README.md)
- [rpi-incusd：Debian Trixie / arm64 / 树莓派、ZFS 与 Incus 配方](recipes/arm64/trixie/rpi-incusd/README.md)
- [vm-sandbox：Debian Trixie / amd64 / Ceph 客户端与来宾工具配方](recipes/amd64/trixie/vm-sandbox/README.md)

在已准备好依赖和权限的构建环境中执行：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe recipes/amd64/trixie/memini/recipe.sh \
  --output outputs/rootfs-trixie-amd64-memini.tar.xz
```

GitHub 上统一从 **Actions → Build rootfs → Run workflow** 手动选择 memini、vm-incusd、vm-sandbox 或 arm64 的 rpi-incusd。
工作流在 Debian 容器中准备工具，成功后上传压缩包、校验和、包版本及构建信息，
并单独保存日志。入口需先提交到默认分支；外壳本身不自动安装依赖。

已有基础产物可先从 **Actions → List rootfs inputs → Run workflow** 查询候选清单，
再从 **Actions → Transform rootfs → Run workflow** 只填写选中的 artifact ID，
执行共享的 [br-initrd 配方](transforms/trixie/br-initrd/README.md)。支持 amd64 和 arm64 原生加工，
根据输入产物自动调度对应架构，配方保持共用。
