import React, { useEffect, useMemo, useState } from 'react';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { api, type Agent, type AccountRecord, type AccountsPayload, type Capabilities, type DeviceInfo, type ExperienceSnapshot, type Friend, type StickerSnapshot, type HealthStatus, type Overview, type ProviderSummary, type Task } from './api';

type PageKey = 'overview' | 'chat' | 'agents' | 'accounts' | 'tasks' | 'memory' | 'stickers' | 'devices' | 'automation' | 'settings';

const NAV: { key: PageKey; label: string; hint: string; icon: string }[] = [
  { key: 'overview', label: '概览', hint: '运行状态', icon: 'home' },
  { key: 'chat', label: '聊天', hint: '好友与 Agent', icon: 'chat' },
  { key: 'agents', label: 'Agent', hint: '创建与协作', icon: 'agent' },
  { key: 'accounts', label: '账号与模型', hint: '多账号路由', icon: 'key' },
  { key: 'tasks', label: '任务', hint: '队列与日志', icon: 'task' },
  { key: 'memory', label: '记忆与反思', hint: '经验与检索', icon: 'memory' },
  { key: 'stickers', label: '表情包', hint: '收藏与含义', icon: 'image' },
  { key: 'devices', label: '设备', hint: '手机与健康', icon: 'device' },
  { key: 'automation', label: '自动化', hint: '事件与主动', icon: 'bolt' },
  { key: 'settings', label: '设置', hint: '安全与网络', icon: 'gear' },
];

const TITLES: Record<PageKey, { title: string; subtitle: string }> = {
  overview: { title: '概览', subtitle: '本机真实运行快照' },
  chat: { title: '聊天', subtitle: '好友、Agent 与群组' },
  agents: { title: 'Agent', subtitle: '本机可用的智能体后端' },
  accounts: { title: '账号与模型', subtitle: '账号、提供商与用量' },
  tasks: { title: '任务', subtitle: '真实任务队列快照' },
  memory: { title: '记忆与反思', subtitle: '长期记忆与经验引擎' },
  stickers: { title: '表情包', subtitle: '本地收藏、含义卡与使用场景' },
  devices: { title: '设备', subtitle: '手机、手环与健康数据' },
  automation: { title: '自动化', subtitle: '条件、定时与主动联系' },
  settings: { title: '设置', subtitle: '安全、网络与数据' },
};

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactElement> = {
    home: <><path d="m3 11 9-8 9 8" /><path d="M5 10v10h14V10M9 20v-6h6v6" /></>,
    chat: <><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" /><path d="M8 9h8M8 13h5" /></>,
    agent: <><rect x="4" y="7" width="16" height="12" rx="3" /><path d="M12 3v4M8 12h.01M16 12h.01M9 16h6" /></>,
    key: <><circle cx="8" cy="15" r="4" /><path d="m11 12 9-9M17 5l3 3" /></>,
    task: <><rect x="4" y="4" width="16" height="16" rx="3" /><path d="m8 12 2.5 2.5L16 8" /></>,
    memory: <><path d="M9 4a3 3 0 0 0-3 3v1a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3" /><path d="M15 4a3 3 0 0 1 3 3v1a3 3 0 0 1 0 6v1a3 3 0 0 1-3 3" /><path d="M12 4v16" /></>,
    device: <><rect x="7" y="2" width="10" height="20" rx="3" /><path d="M11 18h2" /></>,
    bolt: <><path d="M13 2 4 14h7l-1 8 9-12h-7z" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="3" /><circle cx="9" cy="10" r="2" /><path d="m21 15-5-5L5 20" /></>,
    gear: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.chat}</svg>;
}

function Badge({ tone = 'neutral', children }: { tone?: 'ok' | 'warn' | 'muted' | 'neutral' | 'danger'; children: React.ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function Stat({ label, value, note }: { label: string; value: string | number; note?: string }) {
  return <div className="stat"><span className="stat-label">{label}</span><strong className="stat-value">{value}</strong>{note && <span className="stat-note">{note}</span>}</div>;
}

function SectionTitle({ title, caption }: { title: string; caption?: string }) {
  return <div className="section-title"><h3>{title}</h3>{caption && <span>{caption}</span>}</div>;
}

function OverviewPage({ overview, friends, agents, tasks }: { overview: Overview; friends: Friend[]; agents: Agent[]; tasks: Task[] }) {
  return <div className="page-stack">
    <div className="hero">
      <div>
        <Badge tone="ok">本机只读连接</Badge>
        <h2>今天，先看清楚正在发生什么。</h2>
        <p>桌面壳已接入现有 Python Core 的真实快照。聊天写入、账号切换和新语音通话仍在后续阶段，不在这里伪装。</p>
      </div>
      <div className="hero-meta"><span>数据根目录</span><code>{overview.root}</code><span>快照时间</span><code>{overview.generated_at.slice(0, 19).replace('T', ' ')}</code></div>
    </div>
    <div className="stats-grid">
      <Stat label="好友" value={overview.friends} note="本机 profiles" />
      <Stat label="Agent 后端" value={overview.agents} note="已注册适配器" />
      <Stat label="任务" value={overview.tasks} note="TaskManager" />
      <Stat label="Agent Mail" value={overview.mail_messages} note="消息数" />
      <Stat label="长期记忆" value={overview.memories} note="memories 表" />
    </div>
    <div className="two-col">
      <section className="panel">
        <SectionTitle title="最近任务" caption={`${tasks.length} 条快照`} />
        {tasks.length === 0 ? <p className="empty">TaskManager 暂无任务。</p> : <div className="list">
          {tasks.slice(0, 5).map((task) => <div className="row" key={task.id}>
            <div className="row-main"><span className="row-title">{task.title || task.id}</span><span className="row-sub">{task.id}</span></div>
            <Badge tone={task.status === 'FAILED' ? 'danger' : task.status === 'DONE' ? 'muted' : 'warn'}>{task.status || 'UNKNOWN'}</Badge>
          </div>)}
        </div>}
      </section>
      <section className="panel">
        <SectionTitle title="好友" caption="仅显示非敏感摘要" />
        <div className="list">
          {friends.slice(0, 5).map((friend) => <div className="row" key={friend.qq}>
            <div className="avatar">{(friend.name || friend.qq).slice(0, 1)}</div>
            <div className="row-main"><span className="row-title">{friend.name}</span><span className="row-sub">{friend.qq} · {friend.provider}</span></div>
            <Badge>{friend.voice === 'off' ? '文字' : '语音'}</Badge>
          </div>)}
          {friends.length === 0 && <p className="empty">尚未发现好友目录。</p>}
        </div>
      </section>
    </div>
    <section className="panel">
      <SectionTitle title="Agent 后端" caption="安装 ≠ 已登录 ≠ 可用" />
      <div className="agent-grid">
        {agents.map((agent) => <div className="agent-card" key={agent.backend}>
          <div className="agent-head"><strong>{agent.display_name}</strong><Badge tone={agent.installed === true ? 'ok' : agent.installed === false ? 'muted' : 'warn'}>{agent.installed === true ? '已安装' : agent.installed === false ? '未安装' : '需配置'}</Badge></div>
          <span className="row-sub">{agent.backend} · {agent.transport}</span>
          <p>{agent.capabilities.join(' · ')}</p>
        </div>)}
      </div>
    </section>
  </div>;
}

function ChatPage({ friends }: { friends: Friend[] }) {
  const [selected, setSelected] = useState(friends[0]?.qq ?? '');
  useEffect(() => { if (!friends.some((x) => x.qq === selected)) setSelected(friends[0]?.qq ?? ''); }, [friends, selected]);
  const friend = friends.find((x) => x.qq === selected);
  return <div className="chat-layout">
    <aside className="panel conversation-list">
      <SectionTitle title="会话" caption={`${friends.length} 个好友`} />
      {friends.map((item) => <button key={item.qq} className={`conversation ${item.qq === selected ? 'active' : ''}`} onClick={() => setSelected(item.qq)}>
        <span className="avatar">{(item.name || item.qq).slice(0, 1)}</span>
        <span className="conversation-main"><strong>{item.name}</strong><small>{item.provider} · {item.model || '默认模型'}</small></span>
      </button>)}
      {friends.length === 0 && <p className="empty">没有好友 profile。</p>}
    </aside>
    <section className="panel chat-panel">
      {friend ? <>
        <header className="chat-head"><div><h3>{friend.name}</h3><span>{friend.qq} · {friend.provider}</span></div><Badge>只读</Badge></header>
        <div className="chat-placeholder">
          <div className="placeholder-mark"><Icon name="chat" size={28} /></div>
          <h4>聊天写入将在 Phase 2 接入</h4>
          <p>当前桌面壳只读取好友摘要与状态，不会把占位消息写入现有 QQ 聊天记录。旧 Qt 控制台仍是可用的发送入口。</p>
        </div>
        <div className="composer disabled"><span>消息发送尚未迁移</span><button disabled>发送</button></div>
      </> : <p className="empty">选择左侧好友查看摘要。</p>}
    </section>
  </div>;
}

function AgentsPage({ agents }: { agents: Agent[] }) {
  return <div className="page-stack">
    <div className="notice">这里展示真实注册的 Agent 适配器。“已安装”不等于已登录或已授权；创建和群聊协作仍使用现有 Qt 控制台。</div>
    <div className="agent-grid wide">
      {agents.map((agent) => <article className="agent-card large" key={agent.backend}>
        <div className="agent-head"><div className="agent-icon"><Icon name="agent" /></div><div><strong>{agent.display_name}</strong><span className="row-sub">{agent.backend}</span></div><Badge tone={agent.installed === true ? 'ok' : 'muted'}>{agent.installed === true ? '已安装' : agent.installed === false ? '未安装' : '需配置'}</Badge></div>
        <div className="agent-meta"><span>传输：{agent.transport}</span><span>会话：{agent.session_support ? '支持' : '不支持'}</span></div>
        <div className="chips">{agent.capabilities.map((cap) => <span key={cap} className="chip">{cap}</span>)}</div>
      </article>)}
    </div>
  </div>;
}

function AccountsPage({ friends, summary, catalog, onChanged }: { friends: Friend[]; summary: ProviderSummary | null; catalog: AccountsPayload | null; onChanged: () => void }) {
  const [label, setLabel] = useState('');
  const [provider, setProvider] = useState('codex');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [probe, setProbe] = useState('');
  const [bindQq, setBindQq] = useState('');
  const [bindAccount, setBindAccount] = useState('');
  const [bindStatus, setBindStatus] = useState('');
  const [chatId, setChatId] = useState('');
  const [chatPrompt, setChatPrompt] = useState('');
  const [chatResult, setChatResult] = useState('');
  const counts = useMemo(() => friends.reduce<Record<string, number>>((acc, friend) => { acc[friend.provider] = (acc[friend.provider] || 0) + 1; return acc; }, {}), [friends]);
  const rows = summary?.providers?.length ? summary.providers : Object.entries(counts).map(([provider, count]) => ({ provider, friends: count, kind: 'existing', installed: null as boolean | null }));
  return <div className="page-stack">
    <div className="notice">ProviderRouter 基础层已接通：读取现有 profile 与 Agent 安装状态；不保存新凭据、不修改全局配置。额度与成本统计仍未接入。</div>
    <div className="account-grid">
      {rows.map((row) => <section className="panel account" key={row.provider}>
        <div className="agent-head"><strong>{row.provider}</strong><Badge tone={row.installed === true ? 'ok' : row.installed === false ? 'muted' : 'neutral'}>{row.kind === 'agent' ? (row.installed === true ? '已安装' : row.installed === false ? '未安装' : '需配置') : '现有配置'}</Badge></div>
        <p className="muted">被 {row.friends} 个好友使用</p>
        <div className="meter"><span style={{ width: `${Math.min(100, row.friends * 18)}%` }} /></div>
        <span className="row-sub">额度与成本统计：待接入</span>
      </section>)}
      {rows.length === 0 && <p className="empty">没有好友 provider。</p>}
    </div>
    {summary?.accounts?.length ? <section className="panel table-wrap"><table><thead><tr><th>好友</th><th>Provider</th><th>模型</th><th>审批</th></tr></thead><tbody>{summary.accounts.map((a) => <tr key={a.qq}><td>{a.name}</td><td>{a.provider}</td><td>{a.model || '—'}</td><td>{a.approval}</td></tr>)}</tbody></table></section> : null}
    <section className="panel">
      <SectionTitle title="账号目录" caption="仅非敏感引用；密钥仍由现有 DPAPI 管理" />
      <div className="pair-row">
        <input className="text-input" placeholder="账号名称" value={label} onChange={(e) => setLabel(e.target.value)} />
        <select className="text-input" value={provider} onChange={(e) => setProvider(e.target.value)}>{['codex','claude','antigravity','opencode','cursor','qwen_code','kimi_code','acp','custom','deepseek','ollama'].map((p) => <option key={p} value={p}>{p}</option>)}</select>
        <input className="text-input" placeholder="模型（可选）" value={model} onChange={(e) => setModel(e.target.value)} />
        <input className="text-input" type="password" placeholder="API Key（可选，DPAPI 保存）" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
        <button className="button primary" onClick={async () => { if (!label.trim()) return; await api.addAccount({ label: label.trim(), provider, model: model.trim(), api_key: apiKey.trim() || undefined }); setLabel(''); setModel(''); setApiKey(''); onChanged(); }}>添加账号</button>
      </div>
      {catalog?.items?.length ? (
        <div className="list">
          {catalog.items.map((account) => (
            <div className="row" key={account.id}>
              <div className="row-main">
                <span className="row-title">{account.label}</span>
                <span className="row-sub">{account.provider} · {account.model || "默认模型"} · {account.key_id || "无凭据引用"}</span>
              </div>
              <button className="button secondary" onClick={async () => { const result = await api.testAccount(account.id); setProbe(`${account.label}: ${result.ok ? `连接成功，${result.model_count} 个模型` : `连接失败 ${result.error || ''}`}`); }}>测试</button>
              <button className="button secondary" onClick={async () => { await api.removeAccount(account.id); onChanged(); }}>删除</button>
            </div>
          ))}
        </div>
      ) : (
        <p className="empty">账号目录为空。添加只保存 provider/model 引用，不保存 API Key。</p>
      )}
      {probe && <p className="empty">{probe}</p>}
      <div className="pair-row" style={{ marginTop: 12 }}>
        <input className="text-input" placeholder="好友 QQ" value={bindQq} onChange={(e) => setBindQq(e.target.value)} />
        <select className="text-input" value={bindAccount} onChange={(e) => setBindAccount(e.target.value)}>
          <option value="">选择账号</option>
          {catalog?.items.map((account) => <option key={account.id} value={account.id}>{account.label}</option>)}
        </select>
        <button className="button primary" onClick={async () => { if (!bindQq.trim() || !bindAccount) return; await api.bindAccount(bindQq.trim(), bindAccount); const routed = await api.routeAccount(bindQq.trim()); setBindStatus(routed.bound ? `已绑定：${routed.account?.label}` : '绑定失败'); onChanged(); }}>绑定好友</button>
      </div>
      {bindStatus && <p className="empty">{bindStatus}</p>}
      <div className="pair-row" style={{ marginTop: 12 }}>
        <select className="text-input" value={chatId} onChange={(e) => setChatId(e.target.value)}>
          <option value="">选择账号调用</option>
          {catalog?.items.map((account) => <option key={account.id} value={account.id}>{account.label}</option>)}
        </select>
        <input className="text-input" placeholder="测试消息" value={chatPrompt} onChange={(e) => setChatPrompt(e.target.value)} />
        <button className="button primary" disabled={!chatId || !chatPrompt.trim()} onClick={async () => { const result = await api.chatAccount(chatId, chatPrompt.trim()); setChatResult(result.ok ? result.content : `失败：${result.error || ''}`); }}>调用模型</button>
      </div>
      {chatResult && <p className="empty">模型返回：{chatResult}</p>}
    </section>
  </div>;
}

function TasksPage({ tasks }: { tasks: Task[] }) {
  return <div className="page-stack">
    <div className="notice">任务列表来自现有 TaskManager 的只读快照；执行、暂停和日志操作尚未迁移。</div>
    <section className="panel table-wrap">
      <table><thead><tr><th>任务</th><th>ID</th><th>状态</th><th>更新时间</th></tr></thead><tbody>
        {tasks.map((task) => <tr key={task.id}><td>{task.title || '未命名'}</td><td><code>{task.id}</code></td><td><Badge tone={task.status === 'FAILED' ? 'danger' : task.status === 'DONE' ? 'muted' : 'warn'}>{task.status}</Badge></td><td>{task.updated_at || task.created_at || '—'}</td></tr>)}
      </tbody></table>
      {tasks.length === 0 && <p className="empty">暂无任务。</p>}
    </section>
  </div>;
}

function StickersPage({ friends }: { friends: Friend[] }) {
  const [qq, setQq] = useState(friends.find((friend) => friend.provider !== 'unknown')?.qq ?? friends[0]?.qq ?? '');
  const [data, setData] = useState<StickerSnapshot | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!qq) return;
    let alive = true;
    setError('');
    api.stickers(qq).then((value) => { if (alive) setData(value); })
      .catch((exc) => { if (alive) { setData(null); setError(exc instanceof Error ? exc.message : String(exc)); } });
    return () => { alive = false; };
  }, [qq]);
  return <div className="page-stack">
    <div className="notice">表情包来自现有好友收藏；含义卡和图片只在本机读取，不会重新上传或伪造发送。</div>
    <section className="panel">
      <SectionTitle title="选择好友" caption={data ? `${data.count} 张` : '读取收藏'} />
      <div className="pair-row">
        <select className="text-input" value={qq} onChange={(e) => setQq(e.target.value)}>
          {friends.map((friend) => <option key={friend.qq} value={friend.qq}>{friend.name} · {friend.qq}</option>)}
        </select>
        <button className="button secondary" onClick={() => qq && void api.stickers(qq).then(setData).catch((exc) => setError(String(exc)))}>刷新</button>
      </div>
      {error && <p className="empty">读取失败：{error}</p>}
    </section>
    {data && data.items.length === 0 && <p className="empty">这个好友还没有收藏表情。旧控制台里已收藏的会显示在这里。</p>}
    {data && data.items.length > 0 && <div className="sticker-grid">
      {data.items.map((item) => <article className="sticker-card" key={item.key}>
        <div className="sticker-preview">{item.data_url ? <img src={item.data_url} alt={item.meaning || item.text || '表情'} loading="lazy" /> : <span>无预览</span>}</div>
        <div className="sticker-body">
          <strong>{item.text || item.meaning || '未命名表情'}</strong>
          {item.meaning && <p>{item.meaning}</p>}
          <div className="sticker-meta"><span>{item.emotion || '情绪未标注'}</span><span>{item.by || '自动'}</span></div>
          {item.usage && <small>{item.usage}</small>}
        </div>
      </article>)}
    </div>}
  </div>;
}

function MemoryPage({ data, onChanged }: { data: ExperienceSnapshot | null; onChanged: () => void }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState<ExperienceSnapshot['recent']>([]);
  const [searching, setSearching] = useState(false);
  const [problem, setProblem] = useState('');
  const [action, setAction] = useState('');
  const [cause, setCause] = useState('');
  const [saving, setSaving] = useState(false);
  const search = async () => {
    if (q.trim().length < 2) return;
    setSearching(true);
    try { setResults(await api.searchExperiences(q.trim())); }
    finally { setSearching(false); }
  };
  if (!data || !data.available) return <UnavailablePage title="记忆与反思" body={data?.note || "retrieval_engine.sqlite3 尚不可用。"} phase="需要现有 Experience 数据库" />;
  return <div className="page-stack">
    <div className="notice">这是现有 TaskExperienceStore 的只读投影；状态机、验证签名和混合检索仍由 Python 原模块执行，本页不会把未验证记录说成“已验证经验”。</div>
    <div className="stats-grid">
      <Stat label="经验总数" value={data.total} note="task_experiences" />
      <Stat label="已验证" value={data.verified} note="VALIDATED + high_verified" />
      <Stat label="候选" value={data.by_state.CANDIDATE || 0} note="未验证" />
      <Stat label="失败/部分" value={(data.by_state.FAILED || 0) + (data.by_state.PARTIAL || 0)} note="可检索警示" />
    </div>
    <section className="panel">
      <SectionTitle title="检索经验" caption="只读 FTS/子串检索" />
      <div className="pair-row"><input className="text-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="输入问题、动作或根因关键词" onKeyDown={(e) => { if (e.key === 'Enter') void search(); }} /><button className="button primary" onClick={() => void search()} disabled={searching}>{searching ? '检索中…' : '检索'}</button></div>
      {results.length > 0 && <div className="list">{results.map((item) => <div className="row" key={item.id}><div className="row-main"><span className="row-title">{item.problem || item.task_id}</span><span className="row-sub">{item.provider} · {item.confidence}</span></div><Badge>{item.state}</Badge></div>)}</div>}
    </section>
    <section className="panel">
      <SectionTitle title="记录候选经验" caption="只写入 CANDIDATE / low_unverified，不冒充已验证" />
      <div className="pair-row">
        <input className="text-input" placeholder="问题" value={problem} onChange={(e) => setProblem(e.target.value)} />
        <input className="text-input" placeholder="做法" value={action} onChange={(e) => setAction(e.target.value)} />
        <input className="text-input" placeholder="根因（可选）" value={cause} onChange={(e) => setCause(e.target.value)} />
        <button className="button primary" disabled={saving} onClick={async () => {
          if (problem.trim().length < 4 || action.trim().length < 4) return;
          setSaving(true);
          try { await api.addExperienceCandidate(problem.trim(), action.trim(), cause.trim()); setProblem(''); setAction(''); setCause(''); onChanged(); }
          finally { setSaving(false); }
        }}>{saving ? '写入中…' : '写入候选'}</button>
      </div>
      <p className="empty">候选不会进入“已验证”计数；验证仍由现有任务与 Verifier 完成。</p>
    </section>
    <section className="panel">
      <SectionTitle title="最近经验" caption={data.note} />
      {data.recent.length === 0 ? <p className="empty">暂无经验记录。</p> : <div className="list">
        {data.recent.map((item) => <div className="row" key={item.id}>
          <div className="row-main"><span className="row-title">{item.problem || item.task_id}</span><span className="row-sub">{item.provider} · {item.confidence} · {item.created_at}</span></div>
          <Badge tone={item.state === 'VALIDATED' ? 'ok' : item.state === 'FAILED' ? 'danger' : 'neutral'}>{item.state}</Badge>
        </div>)}
      </div>}
    </section>
  </div>;
}

function UnavailablePage({ title, body, phase }: { title: string; body: string; phase: string }) {
  return <section className="panel unavailable">
    <div className="placeholder-mark"><Icon name={title === '设备' ? 'device' : title === '自动化' ? 'bolt' : 'memory'} size={28} /></div>
    <h3>{title}尚未接入</h3>
    <p>{body}</p>
    <Badge tone="warn">{phase}</Badge>
  </section>;
}

function DevicesPage({ health, onAuthorize }: { health: HealthStatus | null; onAuthorize: () => void }) {
  if (!health) return <p className="empty">健康能力状态尚未返回。</p>;
  return <div className="page-stack">
    <div className="notice">HealthBridge 只接受手机端真实上报的能力与授权范围。桌面端不会生成睡眠、步数或心率数据；未报告的品牌显示“待手机上报”。</div>
    <div className="stats-grid">
      <Stat label="真实事件" value={health.event_count} note={health.has_real_events ? '来自手机上报' : '尚无真实数据'} />
      <Stat label="已支持适配器" value={health.adapters.filter((a) => a.supported).length} note="能力检测" />
      <Stat label="已授权品牌" value={health.adapters.filter((a) => a.authorized_scopes.length > 0).length} note="用户授权" />
    </div>
    {health.message && <p className="empty">{health.message}</p>}
    <div className="agent-grid wide">
      {health.adapters.map((adapter) => <article className="agent-card large" key={adapter.adapter}>
        <div className="agent-head"><div className="agent-icon"><Icon name="device" /></div><div><strong>{adapter.label}</strong><span className="row-sub">{adapter.adapter}</span></div><Badge tone={adapter.supported ? 'ok' : 'muted'}>{adapter.supported ? '已上报能力' : '待手机上报'}</Badge></div>
        <div className="agent-meta"><span>授权：{adapter.authorized_scopes.length ? adapter.authorized_scopes.join(' · ') : '无'}</span></div>
        {adapter.supported && <button className="button secondary" onClick={async () => { await api.authorizeHealth(adapter.adapter, ['sleep', 'steps']); onAuthorize(); }}>授权睡眠与步数</button>}
        {adapter.note && <p className="row-sub">{adapter.note}</p>}
      </article>)}
    </div>
  </div>;
}

function SettingsPage({ capabilities }: { capabilities: Capabilities | null }) {
  const [code, setCode] = useState('');
  const [pairError, setPairError] = useState('');
  const [pairBusy, setPairBusy] = useState(false);
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [legacyStatus, setLegacyStatus] = useState('');
  const refreshDevices = async () => { try { setDevices(await api.devices()); } catch { setDevices([]); } };
  useEffect(() => { void refreshDevices(); }, []);
  const makeCode = async () => {
    setPairBusy(true); setPairError('');
    try { const result = await api.pairingCode(); setCode(result.code); }
    catch (exc) { setPairError(exc instanceof Error ? exc.message : String(exc)); }
    finally { setPairBusy(false); }
  };
  return <div className="page-stack">
    <section className="panel">
      <SectionTitle title="桌面桥" caption="只读 HTTP/JSON" />
      <div className="settings-row"><span>桥地址</span><code>http://127.0.0.1:8791</code></div>
      <div className="settings-row"><span>认证</span><span>本机回环；令牌可选</span></div>
      <div className="settings-row"><span>数据写入</span><span>关闭</span></div>
    </section>
    <section className="panel">
      <SectionTitle title="旧版控制台" caption="保留完整旧功能入口" />
      <p className="muted">发送聊天、完整表情管理、人格与日志仍在旧版 Qt 控制台。新版这里只做只读聚合，不假装已经迁移。</p>
      <div className="pair-row"><button className="button secondary" onClick={async () => { try { const result = await api.openLegacy(); setLegacyStatus(result === 'only-available-in-tauri' ? '浏览器预览不能打开旧版；请在安装版里使用。' : `已打开：${result}`); } catch (exc) { setLegacyStatus(exc instanceof Error ? exc.message : String(exc)); } }}>打开旧版控制台</button></div>
      {legacyStatus && <p className="empty">{legacyStatus}</p>}
    </section>
    <section className="panel">
      <SectionTitle title="手机配对" caption="一次性配对码，5 分钟有效" />
      <p className="muted">配对码只在本机生成；手机用它在设置页换取独立设备令牌。当前仍通过明文 HTTP，正式隧道尚未接入。</p>
      <div className="pair-row">
        <button className="button primary" onClick={() => void makeCode()} disabled={pairBusy}>{pairBusy ? '生成中…' : '生成设备配对码'}</button>
        {code && <code className="pair-code">{code}</code>}
      </div>
      {pairError && <p className="empty">生成失败：{pairError}</p>}
    </section>
    <section className="panel">
      <SectionTitle title="已配对设备" caption={`${devices.length} 台`} />
      {devices.length === 0 ? <p className="empty">暂无设备。手机用配对码连接后会出现在这里。</p> : <div className="list">
        {devices.map((device) => <div className="row" key={device.id}>
          <div className="row-main"><span className="row-title">{device.name}</span><span className="row-sub">{device.id} · {device.scopes.join(' · ')} · {device.expires_at ? `有效期至 ${new Date(device.expires_at * 1000).toLocaleDateString()}` : '长期有效'}</span></div>
          <button className="button secondary" onClick={async () => { await api.revokeDevice(device.id); await refreshDevices(); }}>撤销</button>
        </div>)}
      </div>}
    </section>
    <section className="panel">
      <SectionTitle title="能力状态" caption="按真实后端返回" />
      <div className="cap-list">
        {capabilities && Object.entries(capabilities.features).map(([key, value]) => <div className="settings-row" key={key}><span>{key}</span><code>{value}</code></div>)}
      </div>
    </section>
  </div>;
}

function TitleBar() {
  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
  const action = (kind: 'minimize' | 'maximize' | 'close') => {
    if (!isTauri) return;
    const win = getCurrentWindow();
    if (kind === 'minimize') void win.minimize();
    else if (kind === 'maximize') void win.toggleMaximize();
    else void win.close();
  };
  return <div className="titlebar" data-tauri-drag-region>
    <div className="titlebar-identity" data-tauri-drag-region><span className="titlebar-dot" /><strong>Open QQ Dot</strong><span>vNext</span></div>
    <div className="titlebar-hint" data-tauri-drag-region>本机 · 只读连接</div>
    <div className="titlebar-controls">
      <button aria-label="最小化" onClick={() => action('minimize')}>—</button>
      <button aria-label="最大化" onClick={() => action('maximize')}>□</button>
      <button aria-label="关闭" className="titlebar-close" onClick={() => action('close')}>×</button>
    </div>
  </div>;
}

export default function App() {
  const [page, setPage] = useState<PageKey>('overview');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [friends, setFriends] = useState<Friend[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [providers, setProviders] = useState<ProviderSummary | null>(null);
  const [accounts, setAccounts] = useState<AccountsPayload | null>(null);
  const [experiences, setExperiences] = useState<ExperienceSnapshot | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [starting, setStarting] = useState(false);

  const load = async () => {
    setRefreshing(true);
    try {
      const [o, f, a, t, c, p, e, h, ac] = await Promise.all([api.overview(), api.friends(), api.agents(), api.tasks(), api.capabilities(), api.providers(), api.experiences(), api.health(), api.accounts()]);
      setOverview(o); setFriends(f); setAgents(a); setTasks(t); setCapabilities(c); setProviders(p); setExperiences(e); setHealth(h); setAccounts(ac); setError('');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally { setLoading(false); setRefreshing(false); }
  };

  useEffect(() => { void load(); }, []);
  const title = TITLES[page];

  return <div className="desktop-root">
    <TitleBar />
    <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">Q</div><div><strong>Open QQ Dot</strong><span>VNext Preview</span></div></div>
      <nav>{NAV.map((item) => <button key={item.key} className={`nav-item ${page === item.key ? 'active' : ''}`} onClick={() => setPage(item.key)}><Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.hint}</small></span></button>)}</nav>
      <div className="sidebar-foot">
        <div className={`connection ${error ? 'offline' : ''}`}><span className="dot" /><div><strong>{error ? 'Bridge 离线' : 'Bridge 已连接'}</strong><small>{error || '127.0.0.1:8791'}</small></div></div>
        <span className="version">vNext 0.1 · read only</span>
      </div>
    </aside>
    <main className="main">
      <header className="topbar">
        <div><h1>{title.title}</h1><p>{title.subtitle}</p></div>
        <div className="topbar-actions">
          {overview && <Badge tone="ok">只读快照</Badge>}
          <button className="button secondary" onClick={() => void load()} disabled={refreshing}>{refreshing ? '刷新中…' : '刷新'}</button>
          <button className="button primary" disabled title="Phase 2">新建</button>
        </div>
      </header>
      {loading ? <div className="loading-grid">{Array.from({ length: 6 }).map((_, i) => <div className="skeleton" key={i} />)}</div> : error ? <div className="offline-panel"><h3>无法连接 Python Core</h3><p>{error}</p><p>请先运行 <code>python vnext_bridge.py --port 8791</code>，再刷新。桌面壳不会用假数据填充。</p><div className="offline-actions"><button className="button secondary" onClick={() => void load()} disabled={refreshing}>{refreshing ? '刷新中…' : '重新检查'}</button><button className="button primary" disabled={starting} onClick={async () => { setStarting(true); try { const result = await api.startBridge(); if (result === 'only-available-in-tauri') setError('浏览器预览不能启动进程；请用 Tauri 运行，或手动启动 Bridge。'); else { await new Promise((r) => setTimeout(r, 1200)); await load(); } } catch (exc) { setError(exc instanceof Error ? exc.message : String(exc)); } finally { setStarting(false); } }}>{starting ? '启动中…' : '启动本机 Bridge'}</button></div></div> : <>
        {page === 'overview' && overview && <OverviewPage overview={overview} friends={friends} agents={agents} tasks={tasks} />}
        {page === 'chat' && <ChatPage friends={friends} />}
        {page === 'agents' && <AgentsPage agents={agents} />}
        {page === 'accounts' && <AccountsPage friends={friends} summary={providers} catalog={accounts} onChanged={() => void load()} />}
        {page === 'tasks' && <TasksPage tasks={tasks} />}
        {page === 'memory' && <MemoryPage data={experiences} onChanged={() => void load()} />}
        {page === 'stickers' && <StickersPage friends={friends} />}
        {page === 'devices' && <DevicesPage health={health} onAuthorize={() => void load()} />}
        {page === 'automation' && <UnavailablePage title="自动化" body="现有主动引擎仍在旧 UI 运行；新桌面端的事件规则、免打扰和通知 outbox 尚未接入。" phase="Phase 5" />}
        {page === 'settings' && <SettingsPage capabilities={capabilities} />}
      </>}
    </main>
    </div>
  </div>;
}
