# QQ 表情与图片映射

`stickers.json` 把模型输出的 `[[别名]]` 转成 OneBot 消息段。支持三种形式：

```json
{
  "笑": {"type": "face", "id": "3"},
  "呆": {"type": "image", "file": "呆.gif"},
  "难绷": {
    "type": "mface",
    "emoji_id": "从 NapCat 事件获取的 emoji_id",
    "emoji_package_id": "从 NapCat 事件获取的 emoji_package_id",
    "key": "从 NapCat 事件获取的 key",
    "summary": "难绷"
  }
}
```

- `face`：QQ 内置表情，`id` 用数字字符串。
- `image`：本地图片，文件必须放在本目录；路径不能跳出本目录。
- `mface`：NapCat 商城表情，字段从收到的 OneBot 事件里取。

图片文件不会随仓库发布，需要自行放入本目录。缺少文件或映射无效时，该表情不会出现在给模型的表情清单里，也不会把占位符发给 QQ。
