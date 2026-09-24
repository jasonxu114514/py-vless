# py-vless

[English](README.md)

用 **Python** 写的 Cloudflare Workers **VLESS** 节点。

**这个项目提供什么**

* 一个跑在 Cloudflare 边的 **VLESS** 节点,不需要自己的服务器。

## 部署

1. 在 GitHub 上 **Fork 本仓库**。
2. 打开下面的部署页面,把 `<你的GitHub用户名>` 换成你的账号:

   ```
   https://deploy.workers.cloudflare.com/?url=https://github.com/<你的GitHub用户名>/py-vless
   ```

3. 按提示登录 Cloudflare,选择要部署到的账号。
4. 填写表单要求的变量:

   | 字段 | 填什么 |
   | :--- | :--- |
   | `uuid` | 你的 VLESS 用户 ID,一个 UUIDv4。用 `python -c "import uuid; print(uuid.uuid4())"` 或任意 UUID 生成器生成。**必填。** |
   | `preferred` | 优选域名,逗号分隔。先留默认值即可。 |

5. 点 **Deploy**。完成后会显示 Worker 地址,例如
   `https://py-vless.<你的子域>.workers.dev`。
6. 打开 WebUI:

   ```
   https://py-vless.<你的子域>.workers.dev/id/<你的uuid>
   ```

### 部署之后

**第一次**请求可能因 isolate 预热而报 TLS 握手错误,重试一次即可恢复。

## 环境变量配置

所有配置都通过环境变量完成。在 Cloudflare 控制台里位于
**你的 Worker → Settings → Variables and Secrets**。

| 变量 | 必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `uuid` | **是** | *(无)* | 你的 VLESS 用户 ID,UUIDv4。未设置之前 Worker 会返回配置错误。**故意不设默认值** —— 如果自带一个默认值,任何未配置的部署都会变成谁都能用。 |
| `preferred` | 否 | `www.shopify.com`,`mfa.gov.ua`,`www.visa.cn`,`store.ubi.com` | 逗号分隔的域名列表,会被用作节点链接里的**服务器地址**。任何解析到 Cloudflare 边缘的域名都行,也可以换成你自己的域名。最多 30 个。 |
| `path` | 否 | *(任意路径)* | 设置后,只接受该路径上的 WebSocket 升级。除非想收紧限制,否则不用设。 |
| `ws_path` | 否 | `/?ed=2560` | 生成的分享链接里公布的路径。`ed=2560` 是常见客户端使用的 0-RTT early data 提示值。除非客户端有特殊要求,否则不用改。 |

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

协议处理沿用原版脚本所声明的社区来源:
[ca110us/epeius](https://github.com/ca110us/epeius)、
[3Kmfi6HP/EDtunnel](https://github.com/3Kmfi6HP/EDtunnel),
以及 [zizifn/excalidraw-backup](https://github.com/zizifn/excalidraw-backup) 的 VLESS 参考实现。
