# 配置网页 (console)

Agent Context System 的本机配置网页,五页: **Explore** (3D 星图,只读)、**Organize** (Category 与成员、Tier、Manual-only)、**Profiles** (Client × Profile 激活表与预览)、**Lessons** (浏览、新建、编辑 Lesson)、**Inbox** (Report、Digest、Decision log)。

## 运行

```bash
cd ui/console/web && npm install && npm run build   # 首次或改了前端后
python3 ui/console/server.py                        # http://127.0.0.1:5180
```

- `server.py` 只用标准库,只监听 127.0.0.1。`--db` (或 `NODE_INDEX_DB`) 指定 Index,默认 `~/.local/share/node-index/index.db`;`--repo` (或 `NODE_INDEX_REPO`) 指定仓库,默认本仓库。读接口以 `mode=ro` 打开 Index;只有「应用」的 Build 用读写连接。
- 写接口改的是 `--repo` 下的 `shared/` 与 `host/host.yml`,经右上角「应用」(Build → Activate → commit → push) 生效。**试验时用临时仓库与临时 Host config**,否则「应用」会写真实的 `~/.claude` 等 Client 目录并 push。接口见 `docs/openapi.yaml` 的 `x-http`。
- `/api/graph` 按库文件(含 `-wal`/`-journal`)的 mtime 与大小缓存序列化结果: 库没变时不查库、不序列化;带 `ETag`、`Cache-Control: no-cache`,浏览器每次来问,没变就回 304;客户端接受时返回 gzip。测试见 `tests/test_console_server.py`。
- 开发前端: 先起 `server.py`,再 `cd web && npm run dev`(5181 端口,`/api` 代理到 5180)。

## 页面与代码

- 路由是 hash: `#/explore`、`#/organize`、`#/profiles`、`#/lessons`、`#/inbox`。`App.tsx` 放顶栏与页面;Explore (three.js) 单独分包、按需加载。
- `web/src/lib/api.ts`: 接口类型与调用。`lib/status.tsx`: 全局状态 (未应用改动、应用进度、Inbox 待处理数),应用中每 0.5 秒轮询 `/api/apply/status`,结束后各页重新取数。
- `components/TopNav.tsx`: 顶栏与右上角状态 (`{N} 处未应用`、撤销、应用、步骤标签、应用失败、未推送)。
- `pages/Organize.tsx`: 拖动用 pointer 事件实现 (不是 HTML5 拖放),这样光标旁的「移动到 X / 复制到 X」随时跟着 ⌥ 变。拖到自身所在 Category 不生效。
- `pages/Lessons.tsx`: 左栏搜索与列表 (默认只列 active),右栏查看或编辑。新建后先重新取列表再选中新条目。
- `pages/Inbox.tsx`: 改动预览来自 `/api/inbox/{id}/preview`;「复制指令」的文字由后端 `inbox.copy_instruction` 生成。
- 配色是 Design-Principles 的 token (`index.css` 的 `.dark`),只有深色。

## Explore (星图) 的结构

- `web/`: Vite + React + TypeScript,Tailwind v4 + shadcn/ui(base-nova),Sonner 做提示。
- `web/src/lib/graph.ts`: 取数、深度与子孙数、配色。
- `web/src/lib/layout.ts`: 3D 力导向布局(d3-force-3d,固定种子,压扁成星盘)。在 Web Worker 里算,结果(节点 id、边、坐标,均为二进制 typed array)存进 IndexedDB `star-layout` 的一条记录;摘要或访问次数变化不触发重算。结构变了做增量布局(见下)。改布局参数要把 `LAYOUT_VERSION` 加一,下次打开会做一次全量。
- `web/src/components/Galaxy.tsx`: react-three-fiber 场景。真实节点是一组带辉光的点精灵;外加纯装饰的星系尘埃、远景星尘、极淡星云和外层网格球;Bloom 后期。
- `DetailPanel.tsx`、`SearchCommand.tsx`: 详情面板与 ⌘K 搜索。

## 视觉规则

- 主次: 星的大小与亮度由层级决定。中心「我」与三个根最亮最大,枢纽按子孙数对数放大,叶子是暗尘。
- 配色: 暖黑底 + 唯一的橙色强调(界面);星系色只用在数据上(Skill 珊瑚、Knowledge 青、Project 淡紫、Reference 蓝)。
- 焦点: 悬停或选中时,自身与直接邻居保持亮度并显示名称,其余星和尘埃退暗。
- 增量布局的取舍: 新增节点时旧节点钉在原位,星图不会因为每日维护整体重排,看熟的位置保持不变;代价是长期只增量会慢慢偏离全量布局的样子。要重排就把 `LAYOUT_VERSION` 加一。
- 装饰克制: 装饰粒子不能多过真实节点一个量级,否则节点淹没在噪点里。星系尘埃按节点数的 2 倍撒(`CLOUD_PER_NODE`),总量封顶 8000(`CLOUD_MAX`);远景星尘 1200、星云光斑 40;都在 `Galaxy.tsx` 顶部。
- 默认只显示中心与根的名称;不放图例和操作说明。

## 性能

- 按需渲染(`frameloop="demand"`): 只有自转、相机飞行、尘埃淡入淡出时才持续出帧。无操作 30 秒后停止自转,画面静止,GPU 回到空载。任何鼠标、滚轮、键盘操作恢复自转。
- 像素比上限 1.5;后期合成关闭多重采样,画布本身不开抗锯齿(画面先进合成器的离屏缓冲)。
- 布局计算在 Web Worker 里,不阻塞主线程;有缓存时首帧约 55 ms。
- 子孙数(`graph.ts`): 逐节点迭代 DFS,共用一块 `Int32Array` 做访问标记,内存 O(n)、不递归。原先每个节点存完整子孙 Set,合成的 1 万节点深层 DAG 上要 6.2 秒、1.1 GB 堆;现在 0.5 秒、8 MB,结果逐个一致(2026-10-20,Node 22)。
- 拾取(`Galaxy.tsx` 的 `Picker`): 可见节点的屏幕投影按 32px 网格分桶缓存,相机位姿或投影变了才重建(O(n));相机不动时悬停只查鼠标所在格子。pointermove 合并到每帧最多一次。相机在动(自转、飞行)时每帧仍要重建一次。
- 布局增量(`layout.ts`): 结构变化时,旧节点固定在上次坐标,新节点放到已就位的父/子节点附近;挂上新边的旧节点解除固定,从原位出发。模拟里只放要动的节点、它们的邻居、以及 90 以内的旧节点,跑 120 步。要动的节点超过 20% 时改做全量 400 步。合成数据实测(云端容器,Node 22,2026-10-20): 5480 节点新增 1%,全量 42 秒,增量 3.7 秒,旧节点 0 个移动,新节点到父节点的距离中位数与原有父子边一致。
- 焦点(`Stars`): 压暗由 shader 的 `uFocus` 统一完成,每颗星和每条线只多一个 `lit` 属性(0 普通、1 邻居、2 焦点)。切换焦点时只改旧焦点与新焦点那一小撮的 `lit`,并用 `addUpdateRange` 只上传这几个元素;改动多于八分之一才整块上传。筛选(类别、待确认)变化时才整块重写大小与连线颜色。
- 画质自适应(`QualityProbe`): 只在自转期间采样帧间隔(那时每帧都出),每 90 帧取中位数,超过 25 ms(低于约 40fps)就降一档,只降不升,刷新页面恢复。2 档全效果;1 档关星云光斑、Bloom 半分辨率且 5 级模糊;0 档再把像素比降到 1。当前档位见画布外层的 `data-quality`。浏览器把帧率限到 30(如省电模式)时也会降档。
- 新增持续动画时,要在 `useFrame` 里调 `invalidate()` 续帧;只改 BufferAttribute 的 effect 结尾也要调一次,否则按需模式下画面不更新。

## 交互

- 拖动旋转、滚轮缩放、右键平移;空闲时缓慢自转,30 秒无操作后停下。
- 点星打开详情并飞过去;Esc 或关闭按钮回到全景。
- ⌘K 或 `/` 搜索(名称与摘要子串,名称命中在前)。
- 左上类别开关;右上点「待确认」切换只看待确认。
- `window.__graph` 是调试用只读句柄(`graph`、`select(id)`)。

## 已知限制

- 首次打开(或 `LAYOUT_VERSION` 变了、IndexedDB 被清)时做全量布局,在 Worker 里约 1 秒,随节点数增长很快(5480 节点约 40 秒);期间显示加载占位。日常的结构变化走增量。
- 尘埃粒子是随机装饰,不对应任何节点,也不能点。
