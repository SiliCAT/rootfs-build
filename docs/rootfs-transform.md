# 消费已有产物的 rootfs 加工工作流

入口是 **Actions → Transform rootfs → Run workflow**。第一版只消费本仓库
`Build rootfs`（`.github/workflows/rootfs.yml`）成功运行产生的基础产物，支持
memini 和 vm-incusd 的 Debian Trixie / amd64 归档。不会重新执行基础构建。

## 查询候选并选择输入

1. 打开 **Actions → List rootfs inputs → Run workflow**，选择当前分支并运行。
2. 完成后打开该次运行的 **Summary**，查看可用基础 rootfs 候选表。
3. 复制所选行的 **Artifact ID**。
4. 打开 **Actions → Transform rootfs → Run workflow**，只填写 `source_artifact_id`。

加工入口从 artifact 的 API 元数据自动解析 run ID，再执行现有来源校验。
不自动选择“最新产物”，也不会在选中的产物失效时替换为其他输入。
内部来源描述、构建记录及回执仍保留 run ID，便于追溯；只是用户不再需要手工填写。

候选表包含目标、创建时间（UTC）、源码提交、run ID、尝试次数、artifact ID、
运行链接和下载链接。列出当前登记的全部目标，最多显示按创建时间从新到旧排序的
20 项；只接受本仓库成功基础构建及成功尝试产生的、尚未过期且架构受支持的主产物。
日志产物、加工结果和已经移除的目标名称不会进入候选表。

查询通过受控分页读取仓库的产物 API，每页 100 条，最多 10 页，再筛选、排序并展示
已扫描范围内最新的最多 20 项。达到扫描上限时 Summary 会明确提示清单覆盖范围；
已知有效 ID 仍可直接填写。没有候选时明确显示“没有可用基础产物”；API 查询、权限
或网络错误则使查询作业失败，不伪装成空清单。

查询只读取 API 元数据，不下载大型 rootfs，不启动 builder，不需要特权或仓库写权限。
同一个 run 及其尝试的查询会复用结果。诊断包中的 JSON 回执不是候选发现的前提，
因此诊断包 7 天的保留期限不会遮蔽仍在 14 天保留期内的主产物。

候选是查询时的快照。加工启动及下载前会重新核对来源和有效期；下载后继续检查
ZIP digest、五个基础文件、校验清单和构建记录，候选列表不替代这些验收。

## 已知产物的直接输入

也可从成功的 **Build rootfs** 运行详情 **Summary** 顶部的
“供 Transform rootfs 使用”复制 `source_artifact_id`，无需先运行候选查询。
相同的 ID 保存在诊断 ZIP 根目录 `artifact-reference.json` 的 `artifact_id` 字段中。

对于没有新摘要的旧运行，可展开 **build / build → Upload rootfs and build records**，
查看 `Artifact ID` 或 `Artifact download URL`；运行详情产物链接的 `artifacts/` 后
也是 artifact ID。请选择主 rootfs 产物，不选择 `logs-` 产物。
也可用已登录的 GitHub CLI 查询，不需要创建新 secret：

```sh
gh run list --repo SiliCAT/rootfs-build --workflow rootfs.yml --status success
gh api repos/SiliCAT/rootfs-build/actions/runs/RUN_ID/artifacts \
  --jq '.artifacts[] | {id, name, expired}'
```

将 `RUN_ID` 换成实际数字。选择形如
`rootfs-trixie-amd64-vm-incusd-RUN_ID-ATTEMPT` 的 rootfs 产物。
artifact ID 精确锁定一次上传，不隐式寻找“最新”。过期后需要新的基础构建。
新工作流入口提交到默认分支后才会出现在 Actions 的手动运行列表中。

## 执行流程

1. 使用工作流自带、只读 `GITHUB_TOKEN` 根据 artifact ID 查询所属 run ID，
   再校验运行和产物元数据，确认同一仓库、
   成功完成的基础工作流、产物归属、名称、尝试号和未过期状态。
2. 根据输入架构选择经过验证的原生 runner 和固定 digest 的 Debian builder。
   当前只登记 amd64；未知架构明确失败，不回退到 amd64 或自动开启 QEMU。
3. 再次核对来源描述，下载指定 artifact。若 GitHub 提供 ZIP digest，则同时校验它。
   跨主机重定向不携带 GitHub token。下载的 ZIP 只允许五个预期的普通文件；
   不接受路径跳转、符号链接或额外条目。
4. 校验 `.SHA256SUMS`、归档独立校验文件，以及 `.build.json` 中的 target、代码提交、
   来源仓库、运行 URL 和归档 SHA-256。校验成功后才执行特权构建。
5. 在 Debian 容器中准备依赖、运行测试，再执行 `scripts/run-transform.sh`。
   仓库和输入目录只读挂载；token 只用于宿主机的下载步骤，不传给 builder。
6. 共享配方解包、注入、重建、清理并回读验收。沿用通用外壳的私有挂载命名空间、
   进程取消处理、无覆盖发布、归档比较和校验流程。
7. 从最终 rootfs 重新导出包清单和构建记录，成功时上传完整产物。
8. 主产物上传成功后生成运行摘要和 `logs/artifact-reference.json`，随后单独上传诊断日志。

加工业务保存在 [共享配方](../transforms/trixie/br-initrd/recipe.sh)，架构调度表位于
`scripts/transform.py` 的 `BACKENDS`。增加架构需要验证对应执行后端和实际输入系统，
无需复制 br-initrd，也不需要为每个架构增加独立加工工作流。

## 输出和来源

对于 vm-incusd 输入，结果为：

```text
rootfs-trixie-amd64-vm-incusd-br-initrd.tar.zst
rootfs-trixie-amd64-vm-incusd-br-initrd.tar.zst.sha256
rootfs-trixie-amd64-vm-incusd-br-initrd.packages.tsv
rootfs-trixie-amd64-vm-incusd-br-initrd.build.json
rootfs-trixie-amd64-vm-incusd-br-initrd.SHA256SUMS
```

memini 使用对应的基础名称。主产物保留 14 天，诊断日志保留 7 天。

`.build.json` 的顶层代码提交和运行 URL 指本次加工；`base_build` 保留完整的基础构建
记录；`input` 记录原始 run/attempt/artifact ID、归档名和 SHA-256；`transform` 记录
保留的 `update-initramfs -u` 选择规则及最终验收的 initramfs 路径、内核和 SHA-256。
最终包清单来自加工后的系统，而非复制输入清单。`boot_tested` 保持 false。

失败诊断包含下载日志、构建日志、可用的包操作/DKMS 日志、来源描述和已生成的
验收记录。失败不上传 rootfs 主产物，重试使用新的工作目录。

## 加工结果的两个回执出口

**Summary** 将“本次加工输出”和“基础输入（来源）”分开展示：前者的 run/artifact ID
属于本次新产物，后者属于被消费的基础构建。当前加工入口只接受成功的基础构建，
不能把“本次加工输出”的 ID 再填为基础输入，因此加工摘要不提供输出 ID 的加工参数复制块。

诊断 ZIP 根目录也包含 `artifact-reference.json`。其格式为 `schema_version: 1`、
`kind: "transform"`；顶层字段描述本次加工输出，`input` 对象描述基础来源。两部分均包含
目标、仓库、源码提交、run ID、尝试号、运行链接、artifact ID、名称、下载链接、
artifact digest、归档名及归档 SHA-256。输入的 artifact digest 若原始 API 未提供则为
`null`，摘要显示“未提供”，不推测一个值。

公共字段说明见 [基础构建的回执格式](github-actions.md#运行摘要与产物回执)。
两个回执出口共用 `scripts/artifact-reference.py`，在 runner 上执行，不需要 GitHub token 或
写仓库权限。上传失败或跳过时不生成回执，诊断仍尝试上传；已经上传的主产物及构建记录
保持原样，输入校验仍要求五个基础文件。

## 本地复用已验证的下载目录

`transform.py fetch` 成功下载的目录包含五个基础文件和来源描述 `source.json`。
将此目录放在状态目录的 `input/`，在匹配架构的特权 Debian builder 中执行：

```sh
scripts/prepare-builder.sh
scripts/run-transform.sh /absolute/state-directory
```

状态目录不要混用不同运行。脚本会再次核对输入文件，工作目录和输出分别位于
`work/`、`output/`，输入应保持只读。无需联网获取产物时，也可直接使用通用外壳的
`--input-rootfs` 接口，但应自行验证来源；这不会自动生成 GitHub 来源记录。

当前没有“先构建基础系统再加工”的串联入口，也没有 Release 发布步骤。
