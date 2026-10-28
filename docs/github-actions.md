# GitHub Actions：memini 单目标构建

当前入口是 `.github/workflows/rootfs.yml`，在 Actions 页面显示为 **Build rootfs**。
目前只有手动触发，目标为 `amd64-trixie-memini`，不会自动推送 Release 或发布环境。

## 在 GitHub 上运行

1. 将代码提交并合入仓库默认分支，使 GitHub 能发现 `workflow_dispatch` 入口。
2. 打开仓库的 **Actions → Build rootfs → Run workflow**。
3. 选择需要构建的分支和目标 `amd64-trixie-memini`，启动构建。
4. 在该次运行的 Artifacts 中下载成功产物及日志。

此流程不需要额外的仓库 secret。检出只申请 `contents: read`，使用 Actions 自带的
artifact 上传身份；不将 GitHub token 传入构建容器。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `.github/workflows/rootfs.yml` | 手动入口、目标选择、同一目标与 ref 的并发控制 |
| `.github/workflows/rootfs-build.yml` | 共用单目标编排、容器启动、上传与诊断 |
| `targets/targets.json` | 目标身份、配方路径、runner、执行方式和 builder 镜像 |
| `scripts/prepare-builder.sh` | 在 Debian builder 内安装并检查构建工具 |
| `scripts/run-target.sh` | 连接目标登记、归档外壳和构建记录导出 |
| `scripts/target.py` | 校验目标、生成包版本与构建信息、导出指定日志 |
| `recipes/amd64/trixie/memini/recipe.sh` | 独立的设备配方，继续拥有包集、配置和收尾 |

目标解析只接受登记的条目，不把输入当脚本、路径或 shell 片段执行。第一版明确支持
原生 amd64；其他架构、QEMU 和其他 runner 会被拒绝，不能仅添加一个登记就宣称已支持。
这不是配方继承系统。

## 构建环境

使用 `ubuntu-24.04` 托管 runner，通过 Docker 运行 digest 固定的 Debian Trixie slim
镜像；checkout 和 upload-artifact 也固定到提交 SHA。

容器使用 root、`--privileged` 和 `--init`，以满足 mmdebstrap 的挂载、chroot 和
进程回收需求。仓库只读挂载到 `/work`，本次运行的临时状态目录挂载到 `/build`。
没有挂载 runner 的 `/lib/modules`、`/usr/src` 或 Docker socket。

CPU affinity 限定为 runner 可用 CPU 中的前两个，使依赖 nproc 的 DKMS 构建并行度
受到限制。压缩也使用两个线程。单次构建超时为 120 分钟。

准备脚本先完成 APT 工具安装、CA 更新以及权限检查，接着以真实 root 运行测试，
然后才进入配方。
它使用 Debian 自带的签名 keyring 和正常证书校验，不关闭 TLS 或 APT 签名验证。
构建需要访问 Docker Hub 及其镜像层服务、Debian 的 main/updates/security/backports
软件源，以及 GitHub Actions 的检出和 artifact 服务。

runner 的运行内核仅作为构建环境信息记录。ZFS 的目标版本、headers、模块及 initramfs
由配方在 rootfs 内安装、构建和检查。实际启动、模块加载和 memini 硬件仍需要单独验收。

## 成功产物

成功 artifact 名包含目标、run ID 和 attempt，默认保留 14 天；内部包括：

```text
rootfs-trixie-amd64-memini.tar.zst
rootfs-trixie-amd64-memini.tar.zst.sha256
rootfs-trixie-amd64-memini.packages.tsv
rootfs-trixie-amd64-memini.build.json
rootfs-trixie-amd64-memini.SHA256SUMS
```

`packages.tsv` 记录最终安装的包、版本和架构，不包括仅残留配置的已卸载包。
`build.json` 记录源码提交、运行链接、固定 builder、工具版本、目标内核、ZFS 包版本、
产物摘要和构建时间，并明确 `boot_tested: false`。
`SHA256SUMS` 覆盖其余四个文件。下载并展开 Actions artifact 后可执行：

```sh
sha256sum --check rootfs-trixie-amd64-memini.SHA256SUMS
```

只上传已经验证的压缩包及这些记录，不上传 rootfs 目录、并发锁或失败暂存归档。
artifact 的额外压缩关闭，文件元数据保留在 tar 内。解包说明见 [产物接口](artifacts.md)。

固定 builder 不等于整个系统可逐字节复现：目标 APT 源仍会更新。包版本清单用于追溯
实际安装结果；快照软件源和版本冻结可以作为后续独立改进。

## 日志与失败

镜像拉取和资源检查分别保留 pull、disk、memory 日志。容器标准输出会实时写入 Actions
日志，并保存为临时目录中的 `logs/workflow.log`。
无论构建成功或失败，后续步骤都会尝试停止并移除构建容器，随后导出 build.log、
DKMS make.log、APT term.log 和 dpkg.log 中仍存在的文件。不会追随 rootfs 中指向
runner 外部文件的符号链接。导出日志供 runner 读取时，只调整日志副本的权限。

诊断 artifact 单独上传，默认保留 7 天。源码和完整 rootfs 仍留在本次 runner 的临时
目录，作业结束后由 GitHub 回收。超时、强制取消或 runner 故障时，后续步骤和日志上传
不能保证完成；不能把未成功的运行当作已验收产物。

## 验证范围

本地可使用 `actionlint` 检查工作流，使用 ShellCheck 检查 shell 脚本，并运行：

```sh
python3 -m unittest discover -s tests -v
```

测试包括真实微型归档往返、失败/取消、并发、挂载检测、目标校验、包清单和日志导出。
完整 memini 构建应在具备所需权限与网络信任的 Debian 容器中验证；实际 GitHub 上的
检出、运行和 artifact 上传则以对应的 Actions 运行结果为准。
