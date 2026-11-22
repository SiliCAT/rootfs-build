# Debian Trixie Rootfs 公共身份及状态清理技术规范

版本：v0.3（原生 IQN 保留策略；实际构建验收待完成）  
适用项目：`SiliCAT/rootfs-build`  
审查基线：`ded9f89e32d3914730a8a39cc83393b634a3bd73`  
适用范围：memini、schroot-maintenance、vm-dracut、vm-incusd、vm-sandbox、rpi-incusd，以及后续同类 Debian Trixie 母版配方。

## 1. 设计目标与强制原则

1. **最大化公共层，最小化专属层。** 所有配方采用同一清理对象目录、原语及验收标准；允许冗余而无害的清理。不得仅因为某配方目前未安装某软件包，就删去相应公共规则。
2. 公共规则按对象的存在性、文件类型、软件包能力和产物类型执行，不按配方名称分别维护。不存在的可选对象正常 SKIP，不创建无意义的目录和文件。
3. 公共层包括安全的自动清理，也包括无法安全自动修改的对象的统一检查。后者不等于配方例外。
4. **母版可以具备软件包原生的首次启动机制，但原则上不得另外创造实例初始化框架。** 不生成正式 hostname、IP、DNS、SSH host keys、IQN、hostid 或实例映射，不新增通用 firstboot 服务。母版中允许保留有明确软件包语义的未初始化标记；不把生成正式身份作为清理动作。C04.01 暂时保留软件包原生生成的 IQN（不得据此认定克隆后的身份唯一）。
5. 保留发行版正常安装数据、包拥有的必要目录及权限、通用配置、区域环境、包用户、内核及合法配方策略。清理对象不是整个 `/etc`、`/var/lib`、`/var/cache`、`/root` 或 `/home`。
6. 只有无法通过公共规则安全表达的业务才能进入第二层；每个例外必须说明原因、影响范围、最终形态、验收方式及重新审查条件。
7. 技术规范先于实现。内嵌一致代码块与公共脚本均可采用，但不得改变本规范的对象、动作和最终状态。规范与具体实现分离；本轮以 builder 端公共 Python 模块实现首个配方。

术语：**应/必须**为拟定的约束；**待验证**为已经纳入公共目录但尚未批准自动改写的业务；在落实待验证条目前必须完成包行为、产物及运行兼容性审查。本版不代表已通过实际构建；当前仅接入 schroot-maintenance，其他五个配方和加工入口不修改。

## 2. 两处执行时点

| 时点 | 名称 | 环境及职责 |
|---|---|---|
| T1 | mmdebstrap 尾部（customize-hook） | 可在目标 chroot 中执行包工具，依赖必要的构建网络和挂载。先完成所有联网及包操作，再处理状态；涉及启动镜像内容的身份须在最后一次生成启动镜像前收敛。最后完成尾部清理。 |
| T2 | mmdebstrap 成功返回后的外部兜底 | 使用 builder 的文件操作工具直接检查目标目录；重新收敛可能被 mmdebstrap 或最后 hooks 恢复、生成的对象。原则上不依赖目标网络、目标服务、目标内核或目标程序可执行性，不重新安装包或运行 APT，不启动服务，不生成正式实例身份。 |

T1 内部顺序（不是新增第三处清理）：

- 完成各配方正常安装、配置、CA 刷新、联网验证、包收尾与相关 DKMS 等业务；涉及依赖的必要顺序由配方明确。
- 所有联网业务完成后再清除 builder DNS。将可能进入 initramfs 的身份及设备配置收敛到规定状态，再执行既有最终 initramfs/firmware 生成业务。若生成器仍需要某通用配置，保留合法占位而非盲删。
- 镜像生成完毕后，完成其他公共文件及服务状态清理。若任何后续动作再生成身份，重新收敛并检查启动产物；不允许仅清理 rootfs 而放行含旧身份的 initrd。

T2 内部顺序：

- mmdebstrap 返回成功，确认其目标挂载和进程已收尾；验证目标路径及操作范围。
- 重复可离线执行的文件原语；按所有权核对删除本次构建的临时策略；执行必要的最终产品策略落位。
- 对最终目录及其启动产物执行只读验收，然后才能归档。验收不是第三次清理。T2 若发现需要重新运行包工具或生成镜像的异常，应失败并修正 T1，不在兜底中偷偷引入联网和重建业务。

对 T1、T2 都适用的规则记作 **双**；仅 T1 为 **内**；仅 T2 为 **外**。所有规则均在最终产物验收中复核（包工具类规则可以通过离线包数据库和 T1 成功证据复核）。

## 3. 统一清理原语

| 原语 | 定义 | 成功条件 |
|---|---|---|
| `REMOVE` | 删除已明确识别的文件、链接或专用状态目录；目录递归删除必须明确授权 | 对象不存在，含悬空链接检查；可选对象原本不存在为 SKIP |
| `EMPTY_DIR` | 清空已存在的真实目录内容，含隐藏项；保留目录与原属主、权限 | 目录为空；不存在则 SKIP |
| `EMPTY_FILE` | 将目标普通文件收敛为零字节；规则明确要求时才创建文件 | 文件大小为 0；类型、属主、权限符合规则；可选文件缺失则 SKIP |
| `RESET_FILE` | 写入标准规定的确定性内容 | 内容精确匹配基线；类型和元数据符合规则 |
| `FILTER_FILE` | 仅删除符合明确条件的条目，不改变合法内容及原有元数据 | 禁止条目不存在；其他内容被保留 |
| `VERIFY_ONLY` | 只读检查；检查不能自动改写的配置、状态和产物 | 满足允许的内容与结构约束；无法确定安全性的异常阻止发布，不猜测修复 |

普通文本日志清零使用 `EMPTY_FILE`（批量枚举）；必要二进制数据库不能套用通用日志清零规则。APT/DPKG 原生收尾作为 T1 前置业务，不是额外的文件清理原语。

### 3.1 原语安全约束

- 目标必须为本次构建所拥有、真实且绝对的 rootfs 路径，不能是 `/`、宿主根目录、已上线实例、源模板或其他工作目录；不得仅凭字符串前缀判断安全。
- 校验目标路径及祖先链，禁止借助绝对链接、`..`、异常父目录链接越出目标。发行版合法的 merged-/usr 链接按目标内部语义解析；不得按宿主的绝对链接语义跟随。叶节点链接按规则删除链接本身或拒绝，不改写其指向对象。
- 删除、清空前核对挂载信息。不得越过目标内的外部挂载或 bind mount；`find -xdev` 单独不足以防止同设备 bind mount。T1 禁止清理仍承载活跃服务或构建业务的目录；T2 确认所有残余挂载及写入进程已退出。
- 写入文件不得跟随异常链接或通过硬链接修改不属于该对象的内容；检查文件类型，必要时安全替换目标目录项。保留要求的权限、所有权及其他必要元数据。
- 清理路径固定并限定范围，不使用来自不可信配置的路径执行任意删除。通配符必须限定在已校验的目录内。
- 不存在不算错误；权限不足、异常类型、读取失败、写入失败、未知数据语义或最终验收失败不得吞掉。不得用广泛的 `|| true` 掩盖失败。
- 日志采用 ACT / OK / SKIP / ERR，记录规则 ID、目标相对路径及结果，不打印密码、私钥、CHAP、认证 token 等敏感值。重复执行必须收敛，不产生新的正式身份。

## 4. 第一层公共对象目录

以下路径均相对于目标 rootfs，不是 builder 或宿主绝对路径。表中的“缺失可跳过”适用于可选软件包对象。要求存在的基础文件单独注明。

### C01 — 系统静态身份

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C01.01 | `/etc/hostname` | EMPTY_FILE | 双 | 必须为 root:root 0644 普通空文件 |
| C01.02 | `/etc/hosts` | RESET_FILE | 双 | 必须为 root:root 0644 普通文件，内容见下 |
| C01.03 | `/etc/machine-id` | EMPTY_FILE | 双 | 必须为 root:root 0644 普通空文件，保留现有空文件的 systemd 启动语义 |
| C01.04 | `/var/lib/dbus/machine-id` | REMOVE | 双 | 不存在；如为链接，删除链接本身 |
| C01.05 | `/var/lib/systemd/random-seed` | REMOVE | 双 | 不存在，不重新生成 |
| C01.06 | `/etc/machine-info` | REMOVE | 双 | 不携带具体机器描述；不存在 |
| C01.07 | `/etc/hostname-`、`/etc/hosts-`、`/etc/machine-id-`、`/etc/machine-info-` | REMOVE | 双 | 已知身份备份不存在 |
| C01.08 | `/etc/hostid` | VERIFY_ONLY（拟 REMOVE） | 双 | 不得携带构建 hostid；ZFS/启动镜像兼容性待验证后批准自动删除 |

`/etc/hosts` 统一内容（末尾有换行）：

```text
127.0.0.1       localhost
::1             localhost ip6-localhost ip6-loopback
ff02::1         ip6-allnodes
ff02::2         ip6-allrouters
```

不预置 `127.0.1.1` 对应的实例名，不执行 `hostname` 修改内核 UTS 名称。空的 machine-id 与不存在并非完全等价（systemd first-boot 判定不同）；沿用当前配方的空文件方案。

### C02 — 网络身份及运行状态

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C02.01 | `/etc/resolv.conf` | EMPTY_FILE | 双 | 联网业务完成后，收敛为 root:root 0644 普通空文件；不保留 builder DNS/domain；合法 resolver 链接若确需例外须审查，不能沿链接清空 |
| C02.02 | `/var/lib/dhcp/`、`/var/lib/dhcpcd/` | EMPTY_DIR | 双 | 存在时无租约和运行状态，保留目录；确认包目录语义 |
| C02.03 | `/var/lib/systemd/rfkill/` | EMPTY_DIR | 双 | 无设备开关持久状态 |
| C02.04 | `/etc/udev/rules.d/70-persistent-net.rules` | VERIFY_ONLY | 双 | 无构建 MAC 或实例设备映射；确认内容后可批准精确过滤或删除 |
| C02.05 | `/var/lib/NetworkManager/`、`/var/lib/systemd/network/` | VERIFY_ONLY | 双 | 检查租约、设备匹配和运行记录；仅明确识别的状态文件允许清除，不整目录删除 |
| C02.06 | `/etc/network/`、`/etc/systemd/network/` 等网络配置 | VERIFY_ONLY | 双 | 保留通用能力及合法配方策略；不存在 builder IP、MAC、域名、路由等专属信息 |

母版没有配置 DNS 即不承诺马上具备外网解析能力。实际部署的网络提供机制属于部署条件。schroot 的 DNS 由现有会话配置提供，不在母版内新建 resolver 初始化服务。

### C03 — 主机认证及用户凭据

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C03.01 | `/etc/ssh/ssh_host_*` | REMOVE | 双 | 不存在主机公钥、私钥及对应备份；不存在 SSH 包时仍执行检查 |
| C03.02 | root 密码 | 原生 `passwd -dl root` + VERIFY_ONLY | 内 | 锁定且无旧密码哈希，仅允许锁定标记；外部从目标 shadow 安全核验，不打印哈希 |
| C03.03 | `/root/.ssh/`、目标用户的 `.ssh/` | FILTER_FILE / VERIFY_ONLY | 双 | 精确清除 known_hosts 及已确认构建连接记录；禁止私钥、未授权公钥；空目录及合法静态配置保留；未知密钥文件不默默放行 |
| C03.04 | `/root/.config/incus/`、目标用户相应目录 | VERIFY_ONLY | 双 | 无构建期远端配置、客户端证书私钥及认证状态；确认具体文件后才自动清除 |
| C03.05 | `/etc/apt/auth.conf`、`auth.conf.d/` 等 | VERIFY_ONLY | 双 | 无构建凭据；仅移除已知构建专用文件，不删除包默认配置；不得打印内容 |

SSH host keys 的再生成仍由包原生机制或实例部署负责，不新增 firstboot 服务。来自 `/etc/skel`、配方显式授权的静态文件不应因路径名而无条件删除。

### C04 — iSCSI / 远程存储身份

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C04.01 | `/etc/iscsi/initiatorname.iscsi` | KEEP（原生保留，不清理） | 双 + 最终验收 | 原样保留软件包生成的文件；不存在则 SKIP。不检查或限定 open-iscsi 版本，不创建、不删除、不重置，不调用生成器。独立 initiator 实例在实际使用前必须由部署方核实并分配唯一身份。 |
| C04.02 | `/etc/iscsi/iscsid.conf` | VERIFY_ONLY | 双 | 保留通用包配置，检查无实例 CHAP/目标相关覆盖 |
| C04.03 | `/var/lib/iscsi/nodes/`、旧 `/etc/iscsi/nodes/` | EMPTY_DIR | 双 | 无已发现或配置的目标记录；保留既有必要目录 |
| C04.04 | `/var/lib/iscsi/{send_targets,static,isns,slp}/` 及旧 `/etc/iscsi/` 对应目录 | EMPTY_DIR（确认包路径适用） | 双 | 无 discovery 记录；不存在则 SKIP |
| C04.05 | 新旧路径中的 `ifaces/`、`/etc/iscsi/iscsi.initramfs` | VERIFY_ONLY | 双 | 保留通用 iface，禁止构建机 MAC、绑定、固定 target、CHAP 与实例 IQN；不无差别删除默认 iface |
| C04.06 | 启动镜像中 `/etc/initiatorname.iscsi` 等 iSCSI 配置 | VERIFY_ONLY | 内+最终验收 | 允许软件包原生复制的 IQN（C04.01 的保留例外），禁止构建环境连接凭据及实例专属目标配置；镜像专项验收待实现。iSCSI 根启动的独立身份由部署方负责。 |

C04.01 暂定保留发行版安装期间生成的 IQN，与软件包版本解耦。该项是公共层统一保留规则，不是某个配方的专属例外。母版仍不得主动生成正式实例 IQN；多个独立 initiator 不得直接共用克隆所得的 IQN。

### C05 — 本地存储设备身份

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C05.01 | `/etc/zfs/zpool.cache`、`/etc/hostid` | VERIFY_ONLY（拟 REMOVE） | 双 | 无具体池配置/构建 hostid；确认 ZFS 工具及 initramfs 要求之后启用删除，hostid 由 C01.08 负责 |
| C05.02 | `/etc/lvm/devices/system.devices` | VERIFY_ONLY（拟 REMOVE） | 双 | 无构建设备绑定；核对 `use_devicesfile` 等策略，确认删除不会改变预期通用设备扫描语义 |
| C05.03 | `/etc/lvm/backup/`、`/etc/lvm/archive/` | EMPTY_DIR | 双 | 无构建 VG 元数据备份，保留目录 |
| C05.04 | `/run/lvm/` | EMPTY_DIR | 外 | 无构建运行记录；由 C09 的运行目录规则协调执行 |
| C05.05 | `/etc/mdadm/mdadm.conf` | VERIFY_ONLY | 双 | 通用 `HOMEHOST <system>`、`MAILADDR root` 等保留；无构建 ARRAY、具体 HOMEHOST 或设备路径；时间戳注释不等于机器身份 |
| C05.06 | `/etc/fstab` | VERIFY_ONLY | 双 | 允许 Debian 默认未配置占位或经过审查的通用条目；无构建 UUID、设备及挂载路径 |
| C05.07 | `/etc/crypttab` | VERIFY_ONLY | 双 | 无构建加密设备配置及凭据；缺失或仅注释合法 |

不得运行宿主设备探测、导入/导出池、启动阵列或激活 LVM 进行所谓清理。审查配置与离线产物，不操作真实存储设备。构建所需的驱动、模块、通用扫描配置继续保留。

### C06 — 服务状态及数据库

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C06.01 | `/var/lib/incus` | REMOVE | 双 | 整个专用实例状态目录不存在；沿用已有 Incus Host 配方语义，未安装时 SKIP |
| C06.02 | `/var/lib/ceph/`、`/etc/ceph/` | VERIFY_ONLY | 双 | 保留包用户、目录、权限和通用配置；无构建集群 FSID、keyring、连接及服务实例数据；不删整个目录 |
| C06.03 | `/var/lib/nfs/sm/`、`sm.bak/` | EMPTY_DIR | 双 | 空目录，原有属主、权限保留 |
| C06.04 | `/var/lib/nfs/state` | EMPTY_FILE（存在时，候选） | 双 | 初始空文件；保留原属主、权限，不在未安装配方创建。实际母版已观察为空；完整 NFS 运行兼容性待验收 |
| C06.05 | `/var/lib/nfs/etab`、`rmtab` | EMPTY_FILE（存在时，候选） | 双 | 无导出/连接运行记录，保留包要求的文件结构；服务端兼容性待验收 |
| C06.06 | `/var/lib/nfs/nfsdcltrack/` | VERIFY_ONLY | 双 | 无实例客户端追踪数据；核对 SQLite/目录初始化行为后再采用 REMOVE 或 EMPTY_DIR，不能直接 truncate 数据库 |
| C06.07 | `/var/log/journal/` | REMOVE | 双 | 不携带持久 journal |
| C06.08 | `/var/lib/systemd/timesync/clock` | REMOVE | 双 | 无构建期时间同步记录；不修改实际系统时间 |

### C07 — subordinate ID 与用户映射

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C07.01 | `/etc/subuid`、`/etc/subgid`、`/etc/subuid-`、`/etc/subgid-` | FILTER_FILE | 双 | 仅过滤首字段为 `root` 的条目，保留其他用户、注释及文件元数据；不存在不创建；异常链接失败 |

沿用 rpi-incusd 已实现并有单元测试的过滤语义，扩展到全部六项配方。root subid 是本地实例映射策略，不是全网唯一 ID；其他合法映射不能一并清空。实例实际映射由实例初始化决定，不预置固定范围。

### C08 — 软件包管理及构建策略收尾

| ID | 对象 / 业务 | 动作 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C08.01 | CA 证书刷新、APT update | 原生工具 | 内 | CA 可用，`apt-get --error-on=any update` 成功；所有网络要求只在此阶段满足 |
| C08.02 | 不需要的自动依赖 | `apt-get autoremove --purge` | 内 | 成功且不破坏配方必需包清单；先校验候选业务与安装标记 |
| C08.03 | dpkg rc 残余配置 | 精确枚举并 purge | 内 | 无 rc 残余；不误清其他状态 |
| C08.04 | APT 下载缓存 | `apt-get clean` + VERIFY_ONLY | 内，外复核 | 无下载残留；保留包管理目录及锁文件的必要结构 |
| C08.05 | dpkg 完整性 | `dpkg --audit` | 内，外复核 | 无未配置或异常包，配方必需包仍 installed |
| C08.06 | `/var/lib/apt/lists/` | EMPTY_DIR | 双 | 目录为空 |
| C08.07 | 本次构建的临时 APT/DPKG 片段 | REMOVE（所有权/内容校验） | 内或外 | 删除本次生成的临时策略；保留原生配置及配方明确持久策略 |
| C08.08 | `/etc/build-meta` | REMOVE | 双 | 不存在；构建记录应在归档外由框架管理 |
| C08.09 | 构建期 `policy-rc.d` | REMOVE（工具恢复后） | 外 | 构建服务抑制策略不残留；全部母版最终不存在该构建策略 |

保存 `/var/lib/dpkg/status`、`info/`、`/var/lib/apt/extended_states`、debconf 数据库及包拥有的正常缓存/状态。APT/DPKG 临时策略的归属不得仅按文件名推断（例如 schroot 的持久 `Acquire::Languages`）。若 T2 无法离线安全修复包状态，必须失败，不运行新的包操作。

### C09 — 日志、临时文件及历史

| ID | 对象 | 原语 | 时点 | 最终状态 / 验收 |
|---|---|---|---|---|
| C09.01 | `/tmp/`、`/var/tmp/` | EMPTY_DIR | 双 | 为空，保留原有目录权限（含 sticky bit） |
| C09.02 | `/run/` | VERIFY_ONLY + 精确 REMOVE | 外 | 无构建 PID、socket、锁和实际运行文件；可保留 mmdebstrap/包要求的空目录；不在 T1 递归清理活跃目录 |
| C09.03 | `/var/log/` 普通日志 | EMPTY_FILE | 双 | 构建日志为空或按包/工具规则不存在；专用 journal 由 C06.07 删除。压缩日志、二进制日志或未知格式须按语义处理，不能一律写成损坏的零字节档案 |
| C09.04 | root 及目标实际用户的 `.bash_history`、`.sh_history`、`.python_history`、`.lesshst`、`.viminfo` | REMOVE | 双 | 指定历史文件不存在；仅在目标内经过验证的用户目录枚举，不递归删除用户配置 |
| C09.05 | `/var/cache/ldconfig/aux-cache` | REMOVE | 双 | 不存在；保留 `/etc/ld.so.cache` 等运行所需数据 |
| C09.06 | `/var/crash/` | EMPTY_DIR | 双 | 无构建崩溃转储，保留目录 |
| C09.07 | `/root/.cache/` 及相关用户缓存 | VERIFY_ONLY | 双 | 不含认证或构建敏感数据；确认范围后可清理具体缓存，不扩大到整个 home |

## 5. 第二层：配方专属边界

schroot-maintenance 的运行期 `/usr/sbin/policy-rc.d` 迁移到实例初始化，不再属于母版构建。所有配方最终归档均应删除构建期策略；部署方在实例初始化阶段注入运行期 root:root 0755、`#!/bin/sh` 和 `exit 101` 的策略，之后才能执行可能触发服务启动的软件包操作。schroot-maintenance 的配方专属清理业务为零。

memini、vm-incusd、vm-sandbox 暂无已确认必须保留的独有清理规则。rpi-incusd 的 root subid 清理迁入公共层；其固件配置/启动参数审查及 vm-dracut 镜像审查按实际产物类型纳入公共验收，而非先建立专属清理分支。产品需要的内核、DKMS、固件和服务 mask 属于构建业务，并非清理差异。

任何新增例外必须记录：规则 ID、适用范围、不能采用公共状态的原因、替代状态、验收证据、撤销或复审条件；未获批准不得跳过公共规则。

## 6. 启动产物与最终验收

- 对存在的 initramfs-tools、dracut 产物按实际类型读取；对 Raspberry Pi 检查 `/boot/firmware` 的副本和 cmdline。没有启动镜像的 schroot 正常 SKIP。
- 检查所有实际内核版本的最终启动镜像，不只最后一个内核。允许 C04.01 所规定的原生 IQN 保留例外；不得含构建 hostname、hostid、ZFS 池缓存、固化 builder 设备 UUID、网络地址、启动参数或认证信息。不要求所有配置文件均不存在，允许合法通用配置和已验证的占位。
- 对比根目录清理后的状态、镜像内数据及固件副本。验收工具只读；如需重新生成镜像，应修正 T1 的构建顺序而不是 T2 自行重建。
- 每条规则产生明确的 PASS / SKIP / FAIL 证据。SKIP 只适用于声明为可选、实际不存在或类型不适用的对象；未实施的必需检查不是 SKIP。
- 最终验收失败不得发布归档。`verify-rootfs.sh` 的目录与挂载检查继续保留；独立 `verify.sh` 或 memini 现有内嵌验收应承担业务检查。memini 新增独立 verify.sh 仍为后续专项。
- 自动化测试应包括：缺失对象、已干净、实际残留、隐藏文件、重复执行、异常链接/祖先路径/硬链接、挂载隔离、权限失败、包配置保留、T2 清除重新生成的对象、含旧身份的 initramfs 拒绝发布等。实际六配方构建及实例测试与单元测试分开记录。

## 7. iSCSI 专项调查与规则依据

### 7.1 已查明的事实

2026-11-21 只读 schroot 母版中有具体 `InitiatorName`；母版自产的 `open-iscsi.postinst` 第 21、23 行指向该文件与 `iscsi-iname`。Debian Trixie 的 2.1.11-1+deb13u2 源码确认：首次 configure 且身份文件尚不存在时，由 postinst 直接生成并写入 IQN，权限 0600。该操作不需要连接 target 或启动 iscsid，故 `policy-rc.d` 不会阻止它；当前不能据此断言构建过程运行过 iSCSI 服务，也没有证据表明 IQN 来自 builder 文件复制。

同一版本原生 `startup-checks.sh` 在缺少 NAMEFILE 时失败，在文件包含 `GenerateName=yes` 时生成并写入真实 IQN；`iscsid.service` 在 ExecStartPre 执行该检查。这是历史兼容机制的调查结论，不作为当前构建要求。不应另造 firstboot 服务。

### 7.2 initramfs 边界

该版本 initramfs-tools hook 默认会复制 `/etc/iscsi/initiatorname.iscsi` 到镜像中的 `/etc/initiatorname.iscsi`（除非按原生配置禁用相应 hook），也可能复制 `iscsi.initramfs`。所以五个已有启动镜像的配方必须实际审核镜像，不能只检查最终 rootfs。当地实际包 hook 的 `cp` 行也与该源码一致。

`local-top/iscsi` 使用显式 `iscsi_initiator=` 或相应配置中的身份；没有显式身份时读取镜像中的 InitiatorName；iBFT auto 路径另有行为。它不会调用正式系统的 startup-checks 来解释 GenerateName=yes。因而标记文件不能代替从 iSCSI 根启动所需的有效身份。允许软件包原生复制 IQN，但实例启动前必须确认早期启动身份与独立实例配置一致。

C04.01 不再要求在镜像生成前重置 IQN，T1/T2 均不改写该文件。需要独立 iSCSI 身份（包括早期启动）时，部署方另行审查并按需调用发行版原生工具；不向公共母版加入初始化框架。

### 7.3 版本与待验收事项

- 现有只读母版已实测为 2.1.11-1+deb13u2；该版本仅是调查依据，不设构建版本门槛。
- Debian unstable 的 2.1.13-1 移除了历史 GenerateName 逻辑；因此不再将该标记、启动脚本路径或原生生成器能力作为构建放行条件。
- 如部署业务需要独立 initiator，另行测试发行版原生身份生成工具以及持久化行为；无需在公共构建过程中生成身份。
- 逐个检查具备启动镜像的产物时，允许 C04.01 原生 IQN 保留例外，仍检查其他构建残留与凭据。不修改已上线 initiator 的 IQN，避免影响 ACL。

## 8. 实物取证及当前审阅状态

只读 `maintenance.base` 的两轮取证已确认：

| 对象 | 当前实测 | 结论 |
|---|---|---|
| hostname | Docker 式临时名称 | 必须清除 |
| resolv.conf | Docker 生成、10.0.0.2、AWS 内部 search domain | 必须清除 |
| hosts | 四行 localhost 通用基线 | 符合 C01.02 |
| machine-id / D-Bus / random-seed / hostid | 空文件或不存在 | 当前正常 |
| iSCSI InitiatorName | 具体 IQN | C04.01 暂定原样保留；独立实例启用前处理 |
| mdadm.conf | HOMEHOST `<system>`、MAILADDR root，无 ARRAY；有生成时间注释 | 通用配置正常；时间戳是否去除属可重复构建专项，不是身份污染 |
| fstab | UNCONFIGURED FSTAB FOR BASE SYSTEM 注释 | 正常 |
| crypttab | 无活动条目 | 正常（不能断言文件一定存在） |
| NFS | sm、sm.bak 空目录，state 为空，具有包设置的属主权限 | 正常；保留结构及权限，不硬编码数字 UID/GID |
| subuid/subgid | 空文件 | 正常 |
| /root/.ssh、/run/lock/subsys | 空目录结构 | 正常 |
| 临时目录、APT lists、普通日志 | 无非预期内容 | 正常 |

以上是一个实际母版的状态，不代表其他五项产物已验收。待验证对象仍属于公共业务目录；下一轮实现应先解决证据和安全语义，而不是直接为每个配方分别编写删除列表。

## 9. 明确保留与非目标

- 不修改现有 tzdata/locale 基线、APT 源和 pin、文件精简策略、包集、用户、sudo、SSH 配置、服务 mask、网络启动能力；若清理动作会改变其语义，必须专项审查。
- 不追求无条件字节级可重复构建；普通生成时间戳与身份残留应分开处理。禁止因清理时间戳而破坏合法配置。
- 不对当前生产实例或只读母版执行清理，不实现实例初始化框架，不修改真实机器身份。
- 本规范首先针对六个基础配方；`br-initrd` 二次加工入口随后按相同公共规则审查，避免重新导入状态。此版本不自动修改 transform。

## 10. 参考依据

- 项目源码审查基线：[rootfs-build commit](https://github.com/SiliCAT/rootfs-build/commit/ded9f89e32d3914730a8a39cc83393b634a3bd73)。
- [mmdebstrap Debian Trixie 手册](https://manpages.debian.org/trixie/mmdebstrap/mmdebstrap.1.en.html)：setup 复制 hostname 和 resolv.conf；customize 与工具收尾顺序。
- [open-iscsi Trixie postinst](https://sources.debian.org/src/open-iscsi/2.1.11-1%2Bdeb13u2/debian/open-iscsi.postinst)：包首次配置时生成 IQN。
- [open-iscsi Trixie startup-checks.sh](https://sources.debian.org/src/open-iscsi/2.1.11-1%2Bdeb13u2/debian/extra/startup-checks.sh)：缺少文件时失败，GenerateName 标记的原生生成机制。
- [open-iscsi Trixie iscsid.service](https://sources.debian.org/src/open-iscsi/2.1.11-1%2Bdeb13u2/debian/iscsid.service)：ExecStartPre 调用 startup-checks。
- [open-iscsi initramfs-tools hook](https://sources.debian.org/src/open-iscsi/2.1.11-1%2Bdeb13u2/debian/extra/initramfs/hooks/iscsi)、[local-top 脚本](https://sources.debian.org/src/open-iscsi/2.1.11-1%2Bdeb13u2/debian/extra/initramfs/local-top/iscsi)：镜像复制及早期登录机制。
- [Debian 维护者 2016 年说明](https://groups.google.com/g/open-iscsi/c/oG4ByBK6Y6w)：从首次 daemon 启动生成改为包安装时生成。
- [Debian 2.1.13-1 变更记录](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1144935)：unstable 版本删除 GenerateName 兼容逻辑，提示必须做版本能力核对。

---

**评审目标：** 先冻结公共对象、原语、最终状态及两个执行时点；将待验证条目逐项定案，再按统一技术标准实现。任何代码组织方式均不得偏离规范，任何配方差异均须有实际业务依据。

## 11. 首个实现及待验收边界

- `scripts/rootfs-cleanup.py`：builder 端 C01–C09 共同文件原语。T1 为最后 customize-hook，T2 为 mmdebstrap 成功返回后的离线兜底，verify 为只读检查；不安装到目标，不执行目标服务或原生 IQN 生成。
- `recipes/amd64/trixie/schroot-maintenance/recipe.sh`：原包集、区域环境、APT 源、文件精简与 sudo 保持；增加 C08 原生收尾，保护显式包；不再安装运行期 policy-rc.d。
- 清理仅对 recipe 从空目录开始的独占构建树执行；公共模块需要外部 root-owned 构建授权记录，拒绝直接对任意已经部署的 rootfs 发起清理。T2 拒绝全部残余挂载。
- 已定案文件规则实际执行；hostid、zpool.cache、system.devices、非空 NFS state/etab/rmtab 等待验证对象仍以保守 VERIFY_ONLY 覆盖。发现未知有效网络/凭据/服务配置先阻止发布，不静默删改。当前只认可首个配方已经审查的默认内容，未来应扩充经确认的通用配置规则，而不是为配方制造例外。
- 当前清理器对 /run 采用外部空目录结构验收；非空未知运行对象失败。APT 原生 clean 后检查下载缓存，构建专用 dpkg 片段内容匹配传入选项才删除，保留持久 APT 片段。
- 当前无需启动镜像；如果存在 initrd/initramfs 或 firmware，清理器明确拒绝放行，而非假装已实现镜像内审计。完整的 initramfs/firmware 读取器是推广其他五个配方的前置条件。
- `tests/test_rootfs_cleanup.py` 提供 fixture 回归测试。实际 GitHub Actions 构建、归档检查、部署后 DNS 与运行期 policy-rc.d及适用的实例身份仍须验收；单元测试不能替代实际构建。

