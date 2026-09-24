# py-vless

[English](README.md)

用 **Python** 写的 Cloudflare Workers **VLESS** 节点。

**这个项目提供什么**

* 一个跑在 Cloudflare 边的 **VLESS** 节点,不需要自己的服务器。

## 部署

1. 在 GitHub 上 **Fork 本仓库**。
2. 打开下面的部署页面:

   ```
   https://deploy.workers.cloudflare.com/?url=https://github.com/jasonxu114514/py-vless
   ```

3. 按提示登录 Cloudflare,选择要部署到的账号。
4. 填写表单要求的变量:

   | 字段 | 填什么 |
   | :--- | :--- |
   | `uuid` | 你的 VLESS 用户 ID,一个 UUIDv4。用 `python -c "import uuid; print(uuid.uuid4())"` 或任意 UUID 生成器生成。**必填。** |
   | `preferred` | 优选域名,逗号分隔。先留默认值即可。 |
   | `proxyip` | 中转服务器,用于访问 Cloudflare 前置的站点。默认已填公共地址,留空则关闭。 |

5. 点 **Deploy**。完成后会显示 Worker 地址,例如
   `https://py-vless.<你的子域>.workers.dev`。
6. 打开 WebUI:

   ```
   https://py-vless.<你的子域>.workers.dev/id/<你的uuid>
   ```

> 想让之后的推送自动重新部署,可以在 Cloudflare 控制台走
> **Workers & Pages** → **创建应用程序** → **Workers** → **连接到 Git**,选你 Fork 的仓库。
> 构建设置:构建命令 `uvx --from workers-py pywrangler sync`,部署命令 `npx wrangler deploy`,
> 根目录留空。注意创建的 Worker 名称必须与 `wrangler.jsonc` 里的 `name`(即 `py-vless`)
> 一致,否则构建会失败;部署命令要用 `npx wrangler deploy` 而不是 `pywrangler deploy`
> (后者会重复执行 sync)。

### 部署之后

**第一次**请求可能因 isolate 预热而报 TLS 握手错误,重试一次即可恢复。

## 环境变量配置

所有配置都通过环境变量完成。在 Cloudflare 控制台里位于
**你的 Worker → Settings → Variables and Secrets**。

| 变量 | 必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `uuid` | **是** | *(无)* | 你的 VLESS 用户 ID,UUIDv4。未设置之前 Worker 会返回配置错误。 |
| `preferred` | 否 | `www.shopify.com`,`mfa.gov.ua`,`www.visa.cn`,`store.ubi.com` | 逗号分隔的域名列表,会被用作节点链接里的**服务器地址**。任何解析到 Cloudflare 边缘的域名都行,也可以换成你自己的域名。最多 30 个。 |
| `path` | 否 | *(任意路径)* | 设置后,只接受该路径上的 WebSocket 升级。除非想收紧限制,否则不用设。 |
| `ws_path` | 否 | `/?ed=2560` | 生成的分享链接里公布的路径。`ed=2560` 是常见客户端使用的 0-RTT early data 提示值。除非客户端有特殊要求,否则不用改。 |
| `ech` | 否 | `cloudflare-ech.com+https://cloudflare-dns.com/dns-query` | **Encrypted Client Hello**。Worker 域名被 SNI 阻断时,普通 TLS 节点会全部失效 —— ECH 让外层 ClientHello 显示别的域名,真实 SNI 加密在内。只有 TLS(443 系)节点带这个参数。写法:`off` 关闭;`auto` 用内置域名;`<域名>` 只换外层域名;`<域名>+<DoH地址>` 两者都自定义(也可用 `|` 分隔)。 |
| `doh` | 否 | `https://cloudflare-dns.com/dns-query` | 查询 ECH 配置用的 DoH 服务器(该域名的 HTTPS DNS 记录)。 |
| `alpn` | 否 | `http/1.1` | **必须是 `http/1.1`**。WebSocket 传输走 HTTP/1.1 Upgrade,若服务端选中 h2 会报 `websocket: protocol "h2" is not supported`。实测把 h2 放进列表会让所有节点失效。留空则不带该参数。 |
| `fp` | 否 | `chrome` | TLS 指纹。**开了 ECH 就不能用 `randomized`**:它会随机挑曲线,导致 Go 构建外层 ClientHello 失败并报 `tls: malformed outer client hello`。原版脚本用 randomized,这里是有意改的。 |
| `proxyip` | 否 | `proxyip.cmliussss.net` | 中转服务器,格式 `host`、`host:port`、`[IPv6]` 或 `[IPv6]:port`(默认端口 443)。用于访问 **Cloudflare 前置的站点**(cloudflare.com、x.com、chatgpt.com 等):Worker 不允许直连与自身同源边缘的站点,必须经它绕行。写法 `1.2.3.4` / `1.2.3.4:8443` / `proxy.example.com` / `[2001:db8::1]:8443`。别名 `pyip`。**留空表示关闭**;值不合法时也会关闭,而不会回退到默认中转。 |

### 关于 ECH

你的 Worker 域名(`*.workers.dev`)如果被 SNI 阻断,**所有 TLS 节点都会失效** —— 因为普通
TLS 会把真实域名明文写在 ClientHello 里,阻断设备一眼就看见。

ECH 把这件事反过来:让**外层** ClientHello 显示另一个域名,而真实 SNI 加密在内层。只要那个
外层域名没被阻断,节点就还能用。默认外层域名是 `cloudflare-ech.com`(Cloudflare 在它的
HTTPS DNS 记录里公开了 ECH 公钥),通过 Cloudflare 的 DoH 查询。

实测结论(用 xray-core 26.3.27 打真实 Worker):

| 配置 | 结果 |
| :--- | :--- |
| `fp=chrome` + `alpn=http/1.1` + ECH | ✅ 全部通(含 CF 前置站点) |
| `fp=randomized` + ECH | ❌ `tls: malformed outer client hello`,所有节点失效 |
| `alpn=h2,http/1.1` + `type=ws` | ❌ `websocket: protocol "h2" is not supported` |

所以订阅里生成的链接是 `fp=chrome` + `alpn=http/1.1`。

> 注意:EHC 值不合法时**必须留空而不是写错**。Xray 在解析失败时会故意塞一个无效配置让连接
> 直接失败,而不是降级为无 ECH —— 一个笔误会让你完全连不上。

### 关于 proxyIP

Cloudflare 不允许 Worker 连接到位于**它自己边缘**上的源站,所以 `cloudflare.com`、`x.com`、
`chatgpt.com` 这类站点在不配 proxyIP 时会直接失败。proxyIP 就是一台**不在 Cloudflare 边缘**
的第三方服务器,连接改从它绕行。

它之所以能工作,是因为客户端的 TLS ClientHello 里本来就写着**真实目标域名**(那正是 VLESS 里
填的地址),中转服务器看 SNI 就知道该往哪转发 —— 所以字节是**原样透传**的。

由此带来三点需要清楚:

* **只有真正走 TLS 的流量能透过 proxyIP。** 明文流量没有 SNI,中转服务器不知道该去哪。
* **proxyIP 能看到你所访问的目标。** 不要把敏感用途交给别人的服务器。
* **它会消耗对方带宽**,所以公共 proxyIP 经常失效。建议自建:一台不在 Cloudflare 的小鸡,
  用 [x-ui-yg](https://github.com/yonggekkk/x-ui-yg) 之类的脚本即可开出一个。

默认值是公共中转 `proxyip.cmliussss.net`。**留空即关闭**,直连行为等同于没有这个功能;
填了但格式不合法时同样关闭 —— 这是刻意的,免得一个笔误把你的流量悄悄送进一台你没打算用的服务器。

也可以按单个连接覆盖,两种写法都支持:

```
/id/<uuid>?pyip=1.2.3.4            # 网页里改
wss://…/?ed=2560&pyip=1.2.3.4      # 客户端 ws 路径里改
/<uuid> 路径里含 /pyip=1.2.3.4      # 与原版 JS 完全兼容的写法
```

网页里的 proxyIP 输入框和 `preferred` 一样是 **per-isolate、重新部署后失效**,要长期生效请用
环境变量。输入框**留空 = 用默认中转**,填 `none` = 关闭,或填自己的地址。

## 使用

### 节点链接

打开 `/id/<uuid>`,页面会给出两种单节点链接:

* **ws** —— 明文,对应 80 系端口(80、8080、8880、2052、2082、2086、2095)
* **ws + tls** —— TLS,对应 443 系端口(443、2053、2083、2087、2096、8443)

把链接复制进客户端,或扫二维码导入。

> **端口必须与加密方式匹配。** 80 系端口是明文(`security=none`),443 系端口用 TLS
> (`security=tls`)。按其中一种配好的客户端无法在另一种上工作。

### 订阅

| 地址 | 内容 |
| :--- | :--- |
| `/id/<uuid>/sub` | 全部:每个优选域名 × 13 个端口 |
| `/id/<uuid>/sub?tls=0` | 仅 7 个明文端口 |
| `/id/<uuid>/sub?tls=1` | 仅 6 个 TLS 端口 |

返回内容是 base64 编码的 `vless://` 列表。把它粘进客户端的"添加订阅"即可。使用 4 个默认
域名时共 52 个节点。

### 推荐的客户端参数

| 参数 | 值 |
| :--- | :--- |
| 协议 | `vless` |
| 传输 | `ws` |
| 加密 | `none` |
| 地址 | 任一优选域名 |
| 端口 | 443(带 TLS)或 80(不带),或 13 个端口中的任意一个 |
| Host / SNI | 你的 Worker 主机名 |
| 路径 | `/?ed=2560` |

当 Worker 自己的主机名被阻断时,在客户端里开启 **分片(Fragment)** 会有帮助,因为它会把
TLS 握手拆开发送。

## 致谢

感谢 [Linux.do](https://linux.do) 社区。

协议处理沿用原版脚本所声明的社区来源:
[ca110us/epeius](https://github.com/ca110us/epeius)、
[3Kmfi6HP/EDtunnel](https://github.com/3Kmfi6HP/EDtunnel),
以及 [zizifn/excalidraw-backup](https://github.com/zizifn/excalidraw-backup) 的 VLESS 参考实现。
