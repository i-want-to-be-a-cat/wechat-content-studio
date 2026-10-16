# WeChat Content Studio

公众号内容创作工作台，包含标题生成、批量原创、去 AI 味、排版和文章插图。

## 本地运行

```bash
cp .env.example .env
./start.command
```

打开 `http://127.0.0.1:4173/`。

文本默认使用 Ollama + Qwen3：

```bash
ollama serve
ollama pull qwen3:8b
```

图片可使用 ComfyUI。需要导出 API 格式 workflow，并在 `.env` 中配置：

```env
IMAGE_PROVIDER=comfyui
IMAGE_WORKFLOW_PATH=/absolute/path/to/workflow_api.json
```

## GitHub Pages

GitHub Pages 只能运行静态前端，不能运行 FastAPI、Ollama 或 ComfyUI。线上使用时，需要单独部署后端，然后把后端地址写入浏览器：

```js
localStorage.setItem('studioApiBase', 'https://your-api.example.com')
```

也可以通过地址参数临时指定：

```text
https://your-user.github.io/your-repo/?api=https%3A%2F%2Fyour-api.example.com
```

不要把模型 API Key、`.env`、SQLite 数据库或 ComfyUI workflow 中的私密凭据提交到公开仓库。
