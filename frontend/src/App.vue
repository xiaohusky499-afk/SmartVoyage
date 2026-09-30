<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import {
  AlertCircle, ArrowRight, Check, ChevronRight, Clock3,
  Compass, History, Map, MapPin, Menu, MessageSquare, RefreshCw,
  Send, Settings2, ShieldCheck, Sparkles, Sun, TrainFront, Trash2,
  UserRound, Users, X,
} from 'lucide-vue-next'

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true })
const navigation = [
  { id: 'chat', label: '智能对话', icon: MessageSquare },
  { id: 'plan', label: '旅行规划', icon: Map },
  { id: 'history', label: '最近对话', icon: History },
  { id: 'preferences', label: '个人偏好', icon: UserRound },
]
const quickActions = [
  { label: '查天气', icon: Sun, prompt: '我想查询目的地的天气，请先问我城市和出行日期。' },
  { label: '查车票', icon: TrainFront, prompt: '我想查询火车票，请先问我出发地、目的地和日期。' },
  { label: '找景点', icon: MapPin, prompt: '请推荐旅行景点，先问我想去的城市和偏好。' },
  { label: '旅游团', icon: Users, prompt: '我想了解旅游团，请先问我目的地和出行日期。' },
  { label: '旅行保险', icon: ShieldCheck, prompt: '我想了解旅行保险，请先问我目的地和出行信息。' },
]
const agentNames = {
  WeatherQueryAssistant: '天气智能体',
  TicketAssistant: '票务智能体',
  TripAssistant: '行程智能体',
}
const preferencePresets = [
  { name: '旅行节奏', values: ['轻松', '适中', '紧凑'] },
  { name: '预算档位', values: ['经济', '适中', '舒适'] },
  { name: '兴趣主题', values: ['自然风光', '历史人文', '美食', '亲子'] },
  { name: '交通偏好', values: ['火车', '飞机', '不限'] },
]

const view = ref('chat')
const mobileNavOpen = ref(false)
const query = ref('')
const messages = ref([])
const memory = ref({ short_term_messages: [], user_profile: {}, entity_history: [], current_task: {} })
const agents = ref([])
const apiConnected = ref(false)
const loading = ref(false)
const refreshing = ref(false)
const progressText = ref('')
const notice = ref('')
const insightsOpen = ref(false)
const preferenceType = ref(preferencePresets[0].name)
const preferenceChoice = ref(preferencePresets[0].values[0])
const profileKey = ref('')
const profileValue = ref('')
const savingProfile = ref(false)
const confirmClear = ref(false)
const plan = reactive({ destination: '', departure: '', days: '3', date: '' })
const messageInput = ref(null)
const messageViewport = ref(null)
const activeQuestion = ref(1)
const showBackToLatest = ref(false)

const statusLabel = computed(() => {
  if (!apiConnected.value) return '后端尚未连接'
  const online = agents.value.filter(agent => agent.reachable).length
  return `${online}/${agents.value.length} 个智能体在线`
})
const profileEntries = computed(() => Object.entries(memory.value.user_profile || {}))
const entityHistory = computed(() => [...(memory.value.entity_history || [])].reverse())
const questions = computed(() => messages.value.flatMap((message, index) =>
  message.role === 'user' ? [{ index, preview: message.content.slice(0, 36) }] : []))
const preferenceValues = computed(() => preferencePresets.find(item => item.name === preferenceType.value)?.values || [])

function onPreferenceTypeChange() {
  preferenceChoice.value = preferenceValues.value[0] || 'custom'
  profileKey.value = ''
  profileValue.value = ''
}

function isNearLatest() {
  const viewport = messageViewport.value
  return !viewport || viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80
}

function updateQuestionPosition() {
  const viewport = messageViewport.value
  if (!viewport) return
  showBackToLatest.value = !isNearLatest()
  const viewportTop = viewport.getBoundingClientRect().top
  let visible = 1
  viewport.querySelectorAll('[data-question-index]').forEach((element, index) => {
    if (element.getBoundingClientRect().top <= viewportTop + 48) visible = index + 1
  })
  activeQuestion.value = visible
}

function scrollToLatest() {
  const viewport = messageViewport.value
  if (!viewport) return
  viewport.scrollTop = viewport.scrollHeight
  updateQuestionPosition()
}

function jumpToQuestion(messageIndex) {
  const viewport = messageViewport.value
  const target = viewport?.querySelector(`[data-question-index="${messageIndex}"]`)
  if (!target) return
  viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 16
  updateQuestionPosition()
}

function setView(nextView) {
  view.value = nextView
  mobileNavOpen.value = false
  notice.value = ''
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function renderMarkdown(value) {
  const safeHtml = DOMPurify.sanitize(markdown.render(value || ''))
  if (!safeHtml.includes('<table')) return safeHtml

  const template = document.createElement('template')
  template.innerHTML = safeHtml
  template.content.querySelectorAll('table').forEach(table => {
    const headings = [...table.querySelectorAll('thead th')].map(cell => cell.textContent.trim())
    table.querySelectorAll('tbody td').forEach(cell => {
      cell.innerHTML = cell.innerHTML.replace(/&lt;br\s*\/?&gt;/gi, '<br>')
    })
    if (headings.length === 4) {
      table.classList.add('result-list')
      table.querySelectorAll('tbody tr').forEach(row => {
        [...row.cells].forEach((cell, index) => { cell.dataset.label = headings[index] })
      })
      return
    }
    table.classList.add('response-table')
    if (headings.length > 4) table.classList.add('response-table-wide')
    const scroll = document.createElement('div')
    scroll.className = 'table-scroll'
    table.replaceWith(scroll)
    scroll.append(table)
  })
  return template.innerHTML
}

async function apiJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const body = await response.json()
  if (body.status !== 'success') throw new Error(body.message || '服务返回异常')
  return body.data ?? body
}

async function loadAgents() {
  agents.value = await apiJson('/api/agents')
  apiConnected.value = true
}

async function loadMemory(initial = false) {
  memory.value = await apiJson('/api/memory')
  if (initial) {
    messages.value = (memory.value.short_term_messages || []).map(item => ({
      role: item.role,
      content: item.content,
      time: item.timestamp || '',
    }))
    await nextTick()
    scrollToLatest()
  }
}

async function refreshData(initial = false) {
  refreshing.value = true
  const results = await Promise.allSettled([loadAgents(), loadMemory(initial)])
  if (results[0].status === 'rejected') apiConnected.value = false
  if (results.every(result => result.status === 'rejected')) {
    notice.value = '无法连接后端。请先启动 SmartVoyage API 服务（127.0.0.1:8088）。'
  } else if (notice.value.startsWith('无法连接后端')) {
    notice.value = ''
  }
  refreshing.value = false
}

function fillQuery(prompt) {
  query.value = prompt
  setView('chat')
  nextTick(() => messageInput.value?.focus())
}

function buildPlan() {
  if (!plan.destination.trim()) {
    notice.value = '请先填写目的地。'
    return
  }
  const parts = [`请帮我规划${plan.destination.trim()}${plan.days}天旅行`]
  if (plan.departure.trim()) parts.push(`从${plan.departure.trim()}出发`)
  if (plan.date) parts.push(`${plan.date}出发`)
  parts.push('请结合景点、天气和可查询的交通信息，按天给出建议；查询不到的信息请明确说明。')
  fillQuery(parts.join('，'))
}

async function sendMessage() {
  const text = query.value.trim()
  if (!text || loading.value) return
  query.value = ''
  notice.value = ''
  view.value = 'chat'
  messages.value.push({ role: 'user', content: text })
  const reply = reactive({ role: 'assistant', content: '', pending: true, error: false })
  messages.value.push(reply)
  loading.value = true
  progressText.value = '正在连接旅行服务…'
  await nextTick()
  scrollToLatest()

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 600000)
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    if (!response.body) throw new Error('服务未返回数据流')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let done = false
    const consume = frame => {
      const data = frame.split('\n').filter(line => line.startsWith('data: '))
        .map(line => line.slice(6)).join('\n')
      if (!data) return
      if (data === '[DONE]') {
        done = true
        return
      }
      const event = JSON.parse(data)
      if (event.error) throw new Error(event.error)
      if (event.type === 'progress') progressText.value = event.message || '正在处理…'
      if (event.content) {
        const followLatest = isNearLatest()
        reply.content += event.content
        if (followLatest) nextTick(scrollToLatest)
      }
    }
    while (!done) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        consume(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
      }
      if (chunk.done) break
    }
    if (!reply.content.trim()) throw new Error('未收到旅行建议，请稍后重试')
    await loadMemory()
  } catch (error) {
    reply.error = true
    reply.content = error.name === 'AbortError'
      ? '查询超时。远程旅行服务响应较慢，请稍后重试。'
      : `查询失败：${error.message || '请检查后端服务后重试。'}`
  } finally {
    const followLatest = isNearLatest()
    window.clearTimeout(timeout)
    reply.pending = false
    progressText.value = ''
    loading.value = false
    if (followLatest) nextTick(scrollToLatest)
  }
}

async function saveProfile() {
  const key = (preferenceType.value === 'custom' ? profileKey.value : preferenceType.value).trim()
  const value = (preferenceChoice.value === 'custom' ? profileValue.value : preferenceChoice.value).trim()
  if (!key || !value) {
    notice.value = '请填写偏好名称和内容。'
    return
  }
  savingProfile.value = true
  try {
    await apiJson('/api/memory/profile', {
      method: 'POST',
      body: JSON.stringify({ profile: { [key]: value } }),
    })
    await loadMemory()
    profileKey.value = ''
    profileValue.value = ''
    notice.value = '偏好已保存。'
  } catch (error) {
    notice.value = `保存失败：${error.message}`
  } finally {
    savingProfile.value = false
  }
}

async function clearMemory() {
  if (!confirmClear.value || loading.value) return
  try {
    await apiJson('/api/memory/clear', { method: 'POST', body: '{}' })
    messages.value = []
    await loadMemory()
    confirmClear.value = false
    notice.value = '对话与偏好已清空。'
  } catch (error) {
    notice.value = `清空失败：${error.message}`
  }
}

onMounted(() => refreshData(true))
</script>

<template>
  <div class="app-shell">
    <div v-if="mobileNavOpen" class="mobile-scrim" @click="mobileNavOpen = false"></div>
    <aside class="sidebar" :class="{ 'sidebar-open': mobileNavOpen }">
      <div class="brand">
        <span class="brand-mark"><Compass :size="31" :stroke-width="1.8" /></span>
        <div><strong>SmartVoyage</strong><small>让旅行更简单</small></div>
        <button class="mobile-close icon-button" aria-label="关闭菜单" @click="mobileNavOpen = false"><X :size="20" /></button>
      </div>

      <nav class="main-nav" aria-label="主导航">
        <button v-for="item in navigation" :key="item.id" class="nav-item"
          :class="{ active: view === item.id }" :aria-current="view === item.id ? 'page' : undefined"
          @click="setView(item.id)">
          <component :is="item.icon" :size="21" :stroke-width="1.9" />
          <span>{{ item.label }}</span>
        </button>
      </nav>

      <div class="sidebar-foot">
        <div class="mountain-line" aria-hidden="true"></div>
        <p>去看更大的世界<br />让每一次出发都有意义</p>
        <span>AI 陪伴 · 探索不止</span>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <button class="mobile-menu icon-button" aria-label="打开菜单" @click="mobileNavOpen = true"><Menu :size="22" /></button>
        <div class="top-title"><strong>SmartVoyage <span>旅行助手</span></strong><span class="connection"><i :class="{ online: apiConnected }"></i>{{ statusLabel }}</span></div>
        <span class="top-motto">用 AI，遇见更好的旅程</span>
        <span class="avatar"><UserRound :size="20" /></span>
      </header>

      <div v-if="notice" class="notice" role="status"><AlertCircle :size="18" /><span>{{ notice }}</span><button class="icon-button" aria-label="关闭提示" @click="notice = ''"><X :size="16" /></button></div>

      <div class="content-grid">
        <div class="main-column">
          <template v-if="view === 'chat'">
            <section v-if="!messages.length" class="hero-card">
              <div class="hero-copy">
                <span class="eyebrow"><Sparkles :size="15" /> 你的旅行，从一句话开始</span>
                <h1>下一站，想去哪里？</h1>
                <p>告诉我目的地和时间，一起把旅程安排好</p>
              </div>
              <div class="quick-actions" aria-label="常用旅行问题">
                <button v-for="item in quickActions" :key="item.label" type="button" @click="fillQuery(item.prompt)">
                  <component :is="item.icon" :size="19" :stroke-width="1.9" /><span>{{ item.label }}</span>
                </button>
              </div>
            </section>

            <section class="conversation-card" :class="{ 'has-messages': messages.length }" aria-label="旅行对话">
              <div class="section-heading">
                <div><span class="section-kicker">YOUR JOURNEY</span><h2>{{ messages.length ? '旅行对话' : '从这里开启旅程' }}</h2></div>
                <span v-if="questions.length" class="question-count">第 {{ String(activeQuestion).padStart(2, '0') }} / {{ String(questions.length).padStart(2, '0') }} 问</span>
              </div>

              <div class="conversation-body">
                <div ref="messageViewport" class="message-viewport" tabindex="0" aria-label="对话消息，可上下滚动" @scroll.passive="updateQuestionPosition">
                  <div v-if="!messages.length" class="conversation-empty">
                    <div class="sample-label">界面示例 · 非实时数据</div>
                    <div class="sample-user">帮我规划成都三日游，顺便看看天气和火车票。</div>
                    <div class="sample-answer">
                      <div class="assistant-badge"><Compass :size="17" /></div>
                      <div class="sample-content">
                        <p class="sample-lead">我会把查到的信息整理成一份清晰的旅行建议。</p>
                        <div class="sample-cards">
                          <div><MapPin :size="20" /><strong>行程建议</strong><p>按天安排景点与路线，让行程轻松顺畅。</p></div>
                          <div><Sun :size="20" /><strong>天气提示</strong><p>结合目的地天气，提醒你做好出行准备。</p></div>
                          <div><TrainFront :size="20" /><strong>交通查询</strong><p>汇总可查询的车次与航班，方便比较。</p></div>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div v-else class="message-list" aria-live="polite">
                    <article v-for="(message, index) in messages" :key="index" class="message" :class="message.role"
                      :data-question-index="message.role === 'user' ? index : undefined">
                      <div v-if="message.role === 'assistant'" class="assistant-badge"><Compass :size="17" /></div>
                      <div class="message-body">
                        <div v-if="message.role === 'assistant' && message.pending && !message.content" class="thinking"><span class="thinking-dots"></span>{{ progressText || '正在整理旅行建议…' }}</div>
                        <div v-else-if="message.error" class="message-error"><AlertCircle :size="18" />{{ message.content }}</div>
                        <div v-else class="markdown-body" v-html="renderMarkdown(message.content)"></div>
                        <small v-if="message.time">{{ message.time }}</small>
                      </div>
                    </article>
                  </div>
                </div>
                <nav v-if="questions.length" class="question-rail" aria-label="跳转到问题">
                  <button v-for="(question, index) in questions" :key="question.index" type="button" class="question-tick"
                    :class="{ active: activeQuestion === index + 1 }" :title="`第 ${index + 1} 问：${question.preview}`"
                    :aria-label="`跳到第 ${index + 1} 问：${question.preview}`" :aria-current="activeQuestion === index + 1 ? 'step' : undefined"
                    @click="jumpToQuestion(question.index)"><span></span></button>
                </nav>
                <button v-if="showBackToLatest && messages.length" type="button" class="back-to-latest" @click="scrollToLatest">回到最新 <ArrowRight :size="15" /></button>
              </div>
              <form class="query-form" @submit.prevent="sendMessage">
                <MessageSquare :size="21" :stroke-width="1.8" aria-hidden="true" />
                <label class="sr-only" for="travel-query">输入旅行问题</label>
                <input id="travel-query" ref="messageInput" v-model="query" :disabled="loading" autocomplete="off"
                  placeholder="例如：帮我规划成都三日游，查天气和火车票" />
                <button type="submit" class="primary-button" :disabled="loading || !query.trim()"><Send :size="17" />{{ loading ? '查询中' : '发送问题' }}</button>
              </form>
            </section>
          </template>

          <template v-else-if="view === 'plan'">
            <section class="page-card planner-card">
              <span class="section-kicker">PLAN YOUR TRIP</span><h1>把旅行想法，变成清楚的问题</h1>
              <p class="page-intro">填写已确定的信息，我们会组合成问题交给旅行助手。其他细节可以在对话中补充。</p>
              <form class="planner-form" @submit.prevent="buildPlan">
                <label>目的地 <span>*</span><input v-model="plan.destination" placeholder="例如：成都" required /></label>
                <label>出发地 <small>可选</small><input v-model="plan.departure" placeholder="例如：北京" /></label>
                <label>出发日期 <small>可选</small><input v-model="plan.date" type="date" /></label>
                <label>游玩天数<select v-model="plan.days"><option value="2">2 天</option><option value="3">3 天</option><option value="4">4 天</option><option value="5">5 天</option><option value="7">7 天</option></select></label>
                <button type="submit" class="primary-button"><ArrowRight :size="18" />去对话中确认</button>
              </form>
            </section>
          </template>

          <template v-else-if="view === 'history'">
            <section class="page-card history-card">
              <span class="section-kicker">RECENT ACTIVITY</span><h1>最近对话</h1>
              <p class="page-intro">这里展示后端保存的短期对话和查询记录。</p>
              <div v-if="memory.short_term_messages?.length" class="history-list">
                <div v-for="(item, index) in memory.short_term_messages" :key="index" class="history-item">
                  <span class="history-icon"><component :is="item.role === 'user' ? UserRound : Compass" :size="17" /></span>
                  <div><strong>{{ item.role === 'user' ? '你' : '旅行助手' }}</strong><p>{{ item.content }}</p></div><small>{{ item.timestamp }}</small>
                </div>
              </div>
              <div v-else class="empty-panel"><Clock3 :size="28" /><strong>还没有保存的对话</strong><span>去智能对话页，开始一次旅行咨询。</span></div>
              <div v-if="entityHistory.length" class="entity-block"><h2>查询记录</h2><div v-for="(item, index) in entityHistory" :key="index" class="entity-row"><MapPin :size="16" /><span>{{ item.query }}</span><small>{{ item.timestamp }}</small></div></div>
            </section>
          </template>

          <template v-else>
            <section class="page-card preferences-card">
              <span class="section-kicker">YOUR PREFERENCES</span><h1>个人偏好</h1>
              <p class="page-intro">保存你希望旅行助手记住的信息，比如预算或行程节奏。</p>
              <div v-if="profileEntries.length" class="profile-list"><div v-for="[key, value] in profileEntries" :key="key" class="profile-row"><span>{{ key }}</span><strong>{{ value }}</strong></div></div>
              <div v-else class="empty-panel compact"><Settings2 :size="27" /><strong>还没有保存偏好</strong><span>可以在下面添加第一项。</span></div>
              <form class="profile-form" @submit.prevent="saveProfile">
                <h2>添加或更新偏好</h2>
                <div class="profile-fields">
                  <div class="profile-field">
                    <label for="preference-type">偏好类型</label>
                    <select id="preference-type" v-model="preferenceType" @change="onPreferenceTypeChange">
                      <option v-for="item in preferencePresets" :key="item.name" :value="item.name">{{ item.name }}</option>
                      <option value="custom">自定义类型</option>
                    </select>
                    <label v-if="preferenceType === 'custom'" class="sr-only" for="custom-preference-name">自定义偏好名称</label>
                    <input v-if="preferenceType === 'custom'" id="custom-preference-name" v-model="profileKey" placeholder="输入偏好名称" />
                  </div>
                  <div class="profile-field">
                    <label for="preference-choice">偏好内容</label>
                    <select id="preference-choice" v-model="preferenceChoice">
                      <option v-for="value in preferenceValues" :key="value" :value="value">{{ value }}</option>
                      <option value="custom">自定义内容</option>
                    </select>
                    <label v-if="preferenceChoice === 'custom'" class="sr-only" for="custom-preference-value">自定义偏好内容</label>
                    <input v-if="preferenceChoice === 'custom'" id="custom-preference-value" v-model="profileValue" placeholder="输入你的偏好" />
                  </div>
                </div>
                <button class="primary-button" :disabled="savingProfile" type="submit"><Check :size="18" />{{ savingProfile ? '保存中' : '保存偏好' }}</button>
              </form>
              <div class="danger-zone"><div><strong>清空记忆</strong><p>会同时清除短期对话、偏好、任务上下文和查询记录。</p></div><label class="confirm-label"><input v-model="confirmClear" type="checkbox" />我确认清空全部记忆</label><button type="button" class="danger-button" :disabled="!confirmClear || loading" @click="clearMemory"><Trash2 :size="16" />清空记忆</button></div>
            </section>
          </template>
        </div>

        <aside class="insights" :class="{ 'insights-open': insightsOpen }" aria-label="旅行助手状态">
          <button type="button" class="insights-toggle" :aria-expanded="insightsOpen" @click="insightsOpen = !insightsOpen">
            <span>服务状态 · {{ statusLabel }}</span><ChevronRight :size="18" />
          </button>
          <div class="insights-content">
          <section class="insight-card status-card">
            <div class="insight-head"><h2>智能体状态</h2><button class="icon-button refresh-button" aria-label="刷新状态" :disabled="refreshing" @click="refreshData()"><RefreshCw :size="18" :class="{ spinning: refreshing }" /></button></div>
            <p class="insight-subtitle">在线仅表示智能体可连接，远程接口以查询结果为准</p>
            <div class="agent-list">
              <div v-for="(name, key) in agentNames" :key="key" class="agent-row">
                <span class="agent-icon"><Sun v-if="key === 'WeatherQueryAssistant'" :size="20" /><TrainFront v-else-if="key === 'TicketAssistant'" :size="20" /><Map v-else :size="20" /></span>
                <span class="agent-info"><strong>{{ name }}</strong><small>{{ agents.find(agent => agent.name === key)?.description || '旅行服务' }}</small></span>
                <span class="agent-state" :class="agents.find(agent => agent.name === key)?.reachable ? 'ready' : 'offline'">{{ agents.find(agent => agent.name === key)?.reachable ? '在线' : '未连接' }}</span>
              </div>
            </div>
            <div v-if="loading" class="live-progress"><span class="pulse"></span>{{ progressText || '正在处理…' }}</div>
          </section>

          <section class="insight-card profile-card">
            <div class="insight-head"><h2>我的偏好</h2><button class="text-icon-button" @click="setView('preferences')"><Settings2 :size="17" />编辑</button></div>
            <div v-if="profileEntries.length" class="compact-profile"><button v-for="[key, value] in profileEntries.slice(0, 3)" :key="key" @click="setView('preferences')"><span><span class="small-diamond"></span>{{ key }}：{{ value }}</span><ChevronRight :size="17" /></button></div>
            <div v-else class="profile-placeholder"><span class="small-diamond"></span><span>还没有保存偏好</span><button @click="setView('preferences')">去设置 <ArrowRight :size="15" /></button></div>
          </section>

          <div class="source-note"><ShieldCheck :size="18" /><p>信息由远程旅行服务提供，具体以服务返回为准。</p></div>
          </div>
        </aside>
      </div>
    </main>
  </div>
</template>
