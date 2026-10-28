# 构建与归档接口

在仓库根目录，使用已经准备好的 root 构建环境：

```sh
sudo -n scripts/build-rootfs.sh \
  --recipe recipes/amd64/trixie/memini/recipe.sh \
  --output outputs/rootfs-trixie-amd64-memini.tar.zst
```

外壳不安装依赖。构建环境需要 Bash、GNU coreutils/findutils、GNU tar（含 ACL、
xattr 和 SELinux 支持）、Zstandard、awk，以及 util-linux/procps 提供的 flock、setsid
和 ps/pkill；mmdebstrap、APT、Debian keyring、CA 和模拟器等配方前提由工作流准备阶段提供。
工作流或容器还须允许配方所需的 chroot、挂载和命名空间操作。

## 外壳和配方

外壳只接受 `--recipe` 指定的可执行脚本，不 source 配方、不向其传递业务选项，
也不根据设备名、发行版、架构或包名增加分支。

每次构建会在 `--work-dir` 下创建唯一目录，其中包含空的 `rootfs/` 和 `build.log`。
`--work-dir` 默认为 `${TMPDIR:-/tmp}`，必须预先存在；临时构建目录是私有的，
其中 rootfs 目录权限为 `0755`。配方收到 rootfs 的绝对路径，执行期间保持调用者
的工作目录；配方不能依赖特定 cwd。

配方负责包集、系统配置、内核与设备检查，以及 mmdebstrap 返回后的严格收尾。
配方必须等待所有工作结束，不得留下后台进程、脱离会话的写入者或残留挂载。
不能绑定 runner 的 `/lib/modules`、`/usr/src` 到目标 rootfs。

外壳随后执行 `scripts/verify-rootfs.sh`，检查实际的挂载表以及 `etc/`、`usr/`、
`var/` 三个真实目录。如果配方目录中有 `verify.sh`，它也必须可执行并接收唯一的
rootfs 绝对路径；失败状态会传回调用者。设备专有的验收仍在配方或该可选脚本中，
通用检查不代表系统启动或硬件功能已经验证。

## 打包、验证和产物

归档直接包含 `./etc/`、`./usr/` 等系统内容，不增加 `rootfs/` 包装目录，不创建
XFS 镜像、分区或 loop 设备。

GNU tar 使用 pax 格式，保存数值 UID/GID、权限、时间、硬链接、符号链接、稀疏文件、
ACL、全部 xattrs 和 SELinux 标签。不会统一改写 owner，也不会解引用符号链接。
归档过滤 atime/ctime 字段，不承诺恢复访问时间、ctime 或逐字节可重复构建。

默认使用 Zstandard level 6，线程数为 `min(nproc, 4)`；可用 `--compression-level`
选择 1 到 19，或用 `--threads` 选择 1 到 64。这些参数只影响压缩，不改变配方。

完整流程为：

1. 执行配方、通用检查和可选的配方验收。
2. 在输出目录所在文件系统的私有暂存目录中生成归档。
3. 测试 Zstandard 完整性，用 tar 将归档与原目录比较，再检查目录和挂载状态。
4. 生成并验证 SHA-256 校验文件。
5. 发布完整的校验文件和归档，然后清理成功的工作目录。

结果文件是指定的 `FILE.tar.zst` 和 `FILE.tar.zst.sha256`，权限均为 `0644`。
归档通过不覆盖目标的硬链接操作发布，是最后的完成标记。输出文件系统需要支持
硬链接；已有归档、校验文件和符号链接都不会被覆盖。

输出旁的 `FILE.tar.zst.lock` 是保留的并发锁文件，不是待上传产物，不应在构建运行中删除。
同一输出的并发调用会失败；不同输出各自使用独立目录，可以并行运行。

SHA-256 文件记录归档的文件名。验证时进入产物目录：

```sh
sha256sum --check rootfs-trixie-amd64-memini.tar.zst.sha256
```

Actions 上传归档和校验文件，不直接上传展开的 rootfs 目录。[memini 工作流](github-actions.md)
还会导出包清单、构建信息及覆盖全部产物的 SHA256SUMS；当前没有 Release 发布步骤。

## 成功、失败和取消

`--keep-work` 会在成功后也保留 rootfs 和 `build.log`。不指定时，成功构建清理工作目录；
配方输出同时写入终端，工作流可以保留自己的日志。
如果已发布有效产物，但临时目录不能完全清理，会报告警告并保留剩余内容，不撤回有效产物。

任何构建或检查失败都会返回非零状态；配方与压缩工具的失败状态会尽量原样传回。
参数错误为 `2`，路径检查失败为 `5`，并发输出锁冲突为 `75`，缺少工具为 `90`，
结构、残留挂载或后台进程检查失败为 `95`。

HUP、INT、TERM 对应 `129`、`130`、`143`。外壳会向当前阶段的进程组发出信号，
最多等待十秒，必要时使用 KILL；如果仍无法确认停止，会报告并保留所有工作。
配方不应创建脱离构建会话的进程；工作流平台强制终止整个虚拟机不受脚本控制。

失败时保留构建目录、日志和隐藏的归档暂存目录，终端会报告位置。不会自动递归卸载，
也不会递归删除失败现场。检查进程和残留挂载、确认不再写入并完成必要卸载后，
才可人工删除这些目录。重试总是使用新的空构建目录。

最终路径不接收未完成的压缩数据。校验文件和归档分别发布，并非文件系统事务；
例如在两次操作之间发生 SIGKILL，可能只留下校验文件，需要在确认前一次构建已停止后
人工清理。中断发生在完整归档发布之后时，产物可能已经有效，仍应检查工作流状态与校验和。

## 解包

在空目录中以具备所需权限的 root 身份解包，目标文件系统也需要支持相应元数据：

```sh
sudo -n tar --zstd --extract \
  --file=rootfs-trixie-amd64-memini.tar.zst \
  --directory=/absolute/empty/target \
  --numeric-owner --same-owner --same-permissions \
  --acls --xattrs --xattrs-include='*' --selinux
```

这能恢复诸如 UID `1000033`、设备节点和 capabilities；具体能否恢复还受文件系统、
用户命名空间映射和运行权限约束。普通用户解压不能替代部署时的完整元数据恢复。

## 本地测试

```sh
python3 -m unittest discover -s tests -v
```

测试用微型配方实际压缩、验证和解包，不构建完整 Debian。普通用户运行时只模拟
外壳的 UID 预检；真实 root 所有权测试和文件系统不支持的 ACL 测试会明确跳过。
取消、失败、并发锁、压缩、归档比较、残留挂载检测和元数据恢复均有相应检查。
残留挂载测试替换挂载表输入，不需要执行特权 mount。
