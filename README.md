# rootfs-build

按独立配方构建 rootfs，并交付保留 Linux 文件元数据的 `tar.zst` 压缩包。

- [构建外壳与产物接口](docs/artifacts.md)
- [GitHub Actions：memini 单目标构建](docs/github-actions.md)
- [memini：Debian Trixie / amd64 / ZFS 配方](recipes/amd64/trixie/memini/README.md)

在已准备好依赖和权限的构建环境中执行：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe recipes/amd64/trixie/memini/recipe.sh \
  --output outputs/rootfs-trixie-amd64-memini.tar.zst
```

GitHub 上可从 **Actions → Build rootfs → Run workflow** 手动构建 memini。
工作流在 Debian 容器中准备工具，成功后上传压缩包、校验和、包版本及构建信息，
并单独保存日志。入口需先提交到默认分支；外壳本身不自动安装依赖。
