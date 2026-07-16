# astrbot_plugin_wecomocr

这是一个用途受限的 AstrBot 插件：接收一张离校清单图片或 PDF，通过百度 AI Studio 的 `PaddleOCR-VL-1.6` 提取信息，让用户确认或修改，最后调用 WPS AirScript 填表。

## 工作流程

1. 白名单来源中的用户上传一张 JPG、JPEG、PNG 或 PDF。
2. 如果同一条消息包含多个文件，插件只处理第一个，并明确告知其余文件已忽略。
3. 插件返回以下六个字段：姓名、工号、单位、复旦邮箱、离职日期、保留邮箱。
4. 用户可回复 `姓名改为张三`；多个修改可用换行或分号分隔。
5. 用户回复 `提交`、`确认提交` 或 `确认无误` 后，插件向 WPS AirScript 提交六个字段并清空本轮状态。

白名单来源中的消息会阻止默认 LLM 调用。确认阶段只接受严格的字段修改或提交指令；其他内容会被拒绝并立即清空本轮状态。

## 安装与配置

安装插件后，在 AstrBot 插件配置页填写：

- `allowed_sources`：允许使用插件的精确 `unified_msg_origin` 列表，通常格式为 `平台:消息类型:会话ID`。默认空列表会拒绝全部来源。
- `allowed_sender_ids`：可选的发送者 ID 白名单。留空表示白名单来源中的所有发送者均可使用。
- `baidu_api_key`：百度 AI Studio PaddleOCR API Key。
- `wps_script_url`：完整的 WPS AirScript 同步任务地址，例如 `https://365.kdocs.cn/api/v3/ide/file/.../script/.../sync_task`。
- `airscript_token`：WPS 的 `AirScript-Token`。

其余超时、模型及调试选项通常保持默认即可。若暂时不知道消息来源，可先开启 `debug`，发送一条消息后从 AstrBot 日志中的“忽略非白名单来源”记录复制完整来源，再关闭调试。

> 配置页中的 API Key 和 Token 属于敏感信息。不要提交到 Git，也不要发送给普通用户；应限制 AstrBot 配置文件及日志的访问权限。

## WPS 脚本入参

插件仅向 `https://365.kdocs.cn` 域名且以 `/sync_task` 结尾的接口发送请求，结构固定为：

```json
{
  "Context": {
    "argv": {
      "data": {
        "姓名": "张三",
        "工号": "20260001",
        "单位": "某单位",
        "复旦邮箱": "zhangsan@fudan.edu.cn",
        "离职日期": "2026.07.16",
        "保留邮箱": "否"
      }
    }
  }
}
```

## 行为与限制

- 会话按“消息来源 + 发送者 ID”隔离，内存中只保存当前待确认的六个字段。
- 插件重载或 AstrBot 重启会清空未提交会话。
- 确认阶段再次上传文件、发送无关文本或不符合格式的操作，会按安全规则结束本轮。
- WPS 请求超时可能代表结果未知。插件会清空本轮并提示管理员先检查表格，以避免重复提交。
- OCR 依赖 `requests`，由 `requirements.txt` 声明；不依赖本地 PaddlePaddle/PaddleOCR 环境。
