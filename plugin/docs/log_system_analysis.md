# 日志系统架构分析与 WebSocket 方案评估

## 📊 当前实现分析

### 架构概览

```
前端 (Vue)                   后端 (FastAPI)
┌─────────────┐              ┌──────────────┐
│ LogViewer   │              │ /plugin/{id} │
│ Component   │  HTTP GET    │ /logs        │
│             │─────────────>│              │
│             │              │              │
│ - 手动刷新  │              │ - 读取文件   │
│ - 轮询？    │              │ - 解析日志   │
│ - 虚拟列表  │<─────────────│ - 返回 JSON  │
└─────────────┘              └──────────────┘
```

### 当前实现细节

#### 前端 (`plugin/frontend/vue-project/src/`)

1. **API 调用** (`api/logs.ts`)
   - `getPluginLogs()`: HTTP GET 请求
   - 一次性获取所有日志（默认 100 行）

2. **状态管理** (`stores/logs.ts`)
   - Pinia store 管理日志状态
   - 需要手动调用 `fetchLogs()` 刷新

3. **组件** (`components/logs/LogViewer.vue`)
   - 使用虚拟列表渲染大量日志
   - 支持搜索、过滤、滚动
   - **没有自动刷新机制**

#### 后端 (`plugin/server/logs.py`)

1. **日志读取**
   - `get_plugin_logs()`: 读取日志文件尾部
   - 使用 `read_log_file_tail()` 读取最后 N 行
   - 支持过滤（级别、时间、关键词）

2. **文件查找**
   - 查找最新的日志文件（按修改时间排序）
   - 支持插件日志和服务器日志

### 当前方案的优缺点

#### ✅ 优点

1. **简单直接**
   - HTTP RESTful API，易于理解和调试
   - 无需维护 WebSocket 连接状态

2. **资源消耗低**
   - 按需请求，不活跃时不消耗资源
   - 适合查看历史日志

3. **兼容性好**
   - 标准 HTTP，所有环境都支持
   - 易于缓存和代理

#### ❌ 缺点

1. **无法实时更新**
   - 需要手动刷新才能看到新日志
   - 用户体验差，容易错过重要日志

2. **轮询浪费资源**
   - 如果实现自动刷新，需要定时轮询
   - 即使没有新日志也会产生请求

3. **延迟问题**
   - 用户需要等待刷新才能看到新日志
   - 对于调试和监控场景不够及时

---

## 🚀 WebSocket 方案分析

### 方案架构

```
前端 (Vue)                   后端 (FastAPI)
┌─────────────┐              ┌──────────────┐
│ LogViewer   │              │ WebSocket    │
│ Component   │  WebSocket   │ Endpoint     │
│             │<────────────>│              │
│             │              │              │
│ - 实时接收  │              │ - 文件监控   │
│ - 自动更新  │              │ - 流式推送   │
│ - 虚拟列表  │              │ - 增量更新   │
└─────────────┘              └──────────────┘
```

### WebSocket 实现方案

#### 1. 后端实现

**文件监控方案**：
- 使用 `watchdog` 库监控日志文件变化
- 当文件有新内容时，读取增量并推送

**推送策略**：
```python
# 伪代码
@router.websocket("/ws/logs/{plugin_id}")
async def log_stream(websocket: WebSocket, plugin_id: str):
    await websocket.accept()
    
    # 1. 发送初始日志（最后 N 行）
    initial_logs = get_plugin_logs(plugin_id, lines=100)
    await websocket.send_json({"type": "initial", "logs": initial_logs})
    
    # 2. 监控文件变化
    file_watcher = LogFileWatcher(plugin_id)
    async for new_logs in file_watcher.watch():
        await websocket.send_json({
            "type": "append",
            "logs": new_logs
        })
```

#### 2. 前端实现

**连接管理**：
```typescript
// composables/useLogWebSocket.ts
export function useLogWebSocket(pluginId: string) {
  const ws = ref<WebSocket | null>(null)
  const isConnected = ref(false)
  
  function connect() {
    ws.value = new WebSocket(`ws://localhost:48916/ws/logs/${pluginId}`)
    
    ws.value.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'initial') {
        // 替换所有日志
        logs.value = data.logs
      } else if (data.type === 'append') {
        // 追加新日志
        logs.value.push(...data.logs)
      }
    }
  }
  
  function disconnect() {
    ws.value?.close()
  }
  
  return { connect, disconnect, isConnected }
}
```

### WebSocket 方案的优缺点

#### ✅ 优点

1. **实时性**
   - 新日志立即推送到前端
   - 无需手动刷新，用户体验好

2. **资源效率**
   - 只推送增量数据，减少网络传输
   - 避免无效的轮询请求

3. **适合监控场景**
   - 实时查看日志输出
   - 适合调试和问题排查

4. **项目已有基础设施**
   - 项目已大量使用 WebSocket（`main_routers/websocket_router.py`）
   - FastAPI 原生支持 WebSocket
   - 无需引入新依赖

#### ❌ 缺点

1. **连接管理复杂**
   - 需要处理连接断开、重连
   - 需要管理多个插件的连接

2. **资源消耗**
   - 每个连接需要维护文件监控
   - 多个用户同时查看会消耗更多资源

3. **实现复杂度**
   - 需要实现文件监控逻辑
   - 需要处理文件轮转、切换等情况

4. **浏览器限制**
   - 每个域名有 WebSocket 连接数限制
   - 移动端可能有限制

---

## 🎯 推荐方案：混合方案

### 方案设计

**结合两种方案的优点**：

1. **初始加载**：使用 HTTP GET 获取历史日志
2. **实时更新**：使用 WebSocket 推送新日志
3. **降级策略**：WebSocket 不可用时回退到轮询

### 实现架构

```
┌─────────────────────────────────────────┐
│          前端 LogViewer                 │
├─────────────────────────────────────────┤
│ 1. 初始加载：HTTP GET (历史日志)        │
│ 2. 建立连接：WebSocket (实时推送)       │
│ 3. 连接失败：降级到轮询 (每 5 秒)       │
└─────────────────────────────────────────┘
              │              │
              ▼              ▼
    ┌─────────────┐  ┌──────────────┐
    │ HTTP GET    │  │ WebSocket    │
    │ /logs       │  │ /ws/logs/{id}│
    └─────────────┘  └──────────────┘
```

### 实现细节

#### 后端实现

```python
# plugin/server/logs.py

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import asyncio
from collections import defaultdict

# 全局文件监控管理器
log_watchers: Dict[str, LogFileWatcher] = {}

class LogFileWatcher:
    """日志文件监控器"""
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.observer = None
        self.last_position = 0
        self.clients: Set[WebSocket] = set()
    
    async def watch(self, websocket: WebSocket):
        """开始监控并推送日志"""
        self.clients.add(websocket)
        
        # 发送初始日志
        initial_logs = get_plugin_logs(self.plugin_id, lines=100)
        await websocket.send_json({
            "type": "initial",
            "logs": initial_logs["logs"]
        })
        
        # 开始监控文件变化
        if not self.observer:
            self._start_watching()
    
    def _start_watching(self):
        """启动文件监控"""
        log_dir = get_plugin_log_dir(self.plugin_id)
        handler = LogFileEventHandler(self)
        self.observer = Observer()
        self.observer.schedule(handler, str(log_dir), recursive=False)
        self.observer.start()
    
    async def notify_clients(self, new_logs: List[Dict]):
        """通知所有连接的客户端"""
        disconnected = []
        for client in self.clients:
            try:
                await client.send_json({
                    "type": "append",
                    "logs": new_logs
                })
            except Exception:
                disconnected.append(client)
        
        # 移除断开的连接
        for client in disconnected:
            self.clients.discard(client)
        
        # 如果没有客户端了，停止监控
        if not self.clients and self.observer:
            self.observer.stop()
            self.observer = None

@router.websocket("/ws/logs/{plugin_id}")
async def log_stream_endpoint(websocket: WebSocket, plugin_id: str):
    """日志流式推送端点"""
    await websocket.accept()
    
    # 获取或创建监控器
    if plugin_id not in log_watchers:
        log_watchers[plugin_id] = LogFileWatcher(plugin_id)
    
    watcher = log_watchers[plugin_id]
    await watcher.watch(websocket)
    
    try:
        # 保持连接
        while True:
            data = await websocket.receive_text()
            # 可以处理客户端消息（如过滤条件变更）
    except WebSocketDisconnect:
        watcher.clients.discard(websocket)
```

#### 前端实现

```typescript
// composables/useLogStream.ts
export function useLogStream(pluginId: string) {
  const logsStore = useLogsStore()
  const ws = ref<WebSocket | null>(null)
  const isConnected = ref(false)
  const reconnectTimer = ref<number | null>(null)
  const pollTimer = ref<number | null>(null)
  
  // 初始加载历史日志
  async function loadInitialLogs() {
    await logsStore.fetchLogs(pluginId, { lines: 100 })
  }
  
  // 连接 WebSocket
  function connect() {
    if (ws.value?.readyState === WebSocket.OPEN) return
    
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/logs/${pluginId}`
    
    ws.value = new WebSocket(wsUrl)
    
    ws.value.onopen = () => {
      isConnected.value = true
      clearInterval(pollTimer.value) // 停止轮询
    }
    
    ws.value.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'initial') {
        logsStore.logs[pluginId] = data.logs
      } else if (data.type === 'append') {
        logsStore.logs[pluginId].push(...data.logs)
      }
    }
    
    ws.value.onerror = () => {
      isConnected.value = false
      startPolling() // 降级到轮询
    }
    
    ws.value.onclose = () => {
      isConnected.value = false
      // 3 秒后重连
      reconnectTimer.value = window.setTimeout(connect, 3000)
    }
  }
  
  // 降级到轮询
  function startPolling() {
    if (pollTimer.value) return
    pollTimer.value = window.setInterval(async () => {
      await logsStore.fetchLogs(pluginId, { lines: 100 })
    }, 5000) // 每 5 秒轮询一次
  }
  
  // 断开连接
  function disconnect() {
    if (reconnectTimer.value) {
      clearTimeout(reconnectTimer.value)
    }
    if (pollTimer.value) {
      clearInterval(pollTimer.value)
    }
    ws.value?.close()
  }
  
  onMounted(async () => {
    await loadInitialLogs()
    connect()
  })
  
  onUnmounted(() => {
    disconnect()
  })
  
  return { isConnected, connect, disconnect }
}
```

---

## 📋 实施建议

### 阶段 1：评估和准备（当前阶段）

1. ✅ **已完成**：分析当前架构
2. ⏳ **进行中**：评估 WebSocket 方案
3. ⏳ **待做**：决定是否实施

### 阶段 2：基础实现（如果决定实施）

1. **后端**：
   - 添加 `watchdog` 依赖（如果项目中没有）
   - 实现 `LogFileWatcher` 类
   - 添加 WebSocket 端点

2. **前端**：
   - 创建 `useLogStream` composable
   - 在 `LogViewer` 组件中集成
   - 添加连接状态指示器

### 阶段 3：优化和测试

1. **性能优化**：
   - 限制推送频率（防抖）
   - 批量推送多条日志
   - 优化文件监控性能

2. **用户体验**：
   - 添加连接状态提示
   - 自动重连机制
   - 降级到轮询的平滑过渡

---

## 🎯 结论

### 推荐：**实施 WebSocket 方案（混合模式）**

**理由**：

1. ✅ **项目已有 WebSocket 基础设施**
   - 主服务器已大量使用 WebSocket
   - FastAPI 原生支持，无需额外配置

2. ✅ **显著提升用户体验**
   - 实时日志更新
   - 无需手动刷新
   - 适合调试和监控场景

3. ✅ **资源消耗可控**
   - 只推送增量数据
   - 连接断开时自动停止监控
   - 有降级策略保证可用性

4. ✅ **实现复杂度适中**
   - 可以复用现有的 WebSocket 基础设施
   - 文件监控逻辑相对简单
   - 前端实现不复杂

### 不推荐：纯 HTTP 轮询方案

**理由**：
- ❌ 浪费资源（即使没有新日志也会请求）
- ❌ 延迟高（需要等待轮询间隔）
- ❌ 用户体验差（需要手动刷新）

---

## 📝 下一步行动

如果决定实施 WebSocket 方案，建议：

1. **先实现 MVP（最小可行产品）**
   - 基础的文件监控和推送
   - 简单的 WebSocket 连接
   - 测试单个插件的日志流

2. **逐步完善**
   - 添加重连机制
   - 实现降级策略
   - 优化性能和用户体验

3. **测试和验证**
   - 多用户同时查看日志
   - 长时间运行稳定性
   - 文件轮转场景测试

---

**创建时间**：2025-12-20  
**分析者**：AI Assistant  
**状态**：待决策

