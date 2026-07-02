<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart, PieChart, GaugeChart, RadarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  PolarComponent
} from 'echarts/components'
import VChart from 'vue-echarts'
import { getInventoryList, getScreenData } from '@/api'
import type { Inventory, ScreenData } from '@/types/api'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  PieChart,
  GaugeChart,
  RadarChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  PolarComponent
])

const route = useRoute()
const router = useRouter()
const activeTab = ref('overview')

// 切换Tab时同步更新URL参数，防止刷新/重载后丢失状态
const switchTab = (tab: string) => {
  activeTab.value = tab
  router.replace({ query: { tab } })
}

const AIAnalysisComponent = defineAsyncComponent(() => import('@/views/ai/AIAnalysis.vue'))
const DigitalTwinComponent = defineAsyncComponent(() => import('@/views/digital-twin/DigitalTwin.vue'))

watch(() => route.query.tab as string | undefined, (newTab) => {
  if (newTab && ['overview', 'ai-analysis', 'digital-twin'].includes(newTab as string)) {
    activeTab.value = newTab as string
  }
}, { immediate: true })

// ========== 粒子预计算位置（避免模板中 Math.random 导致重渲染跳变）==========
const particles = Array.from({ length: 30 }, () => ({
  left: Math.random() * 100 + '%',
  top: Math.random() * 100 + '%',
  delay: Math.random() * 6 + 's',
  duration: (3 + Math.random() * 4) + 's'
}))

// ========== 真实数据源 ==========
const screenData = ref<ScreenData>({})
const emptyOrderStatus: Record<string, number> = {}
const emptyPlanningStatus: NonNullable<ScreenData['planning_status']> = {
  total: 0,
  complete: 0,
  partial: 0,
  none: 0,
  avg_complete_rate: 0,
  has_data: false
}
const silentRequestOptions = { skipErrorHandler: true } as const

const currentTime = ref('')
let timeInterval: number
let dataInterval: number

const inventoryData = ref<Inventory[]>([])

// ========== 全屏模式 ==========
const isFullscreen = ref(false)
const toggleFullscreen = () => {
  isFullscreen.value = !isFullscreen.value
  if (isFullscreen.value) {
    document.documentElement.requestFullscreen?.().catch(() => {})
    document.body.classList.add('screen-fullscreen')
  } else {
    document.exitFullscreen?.().catch(() => {})
    document.body.classList.remove('screen-fullscreen')
  }
}
// 监听 ESC 退出全屏
const onFullscreenChange = () => {
  if (!document.fullscreenElement) {
    isFullscreen.value = false
    document.body.classList.remove('screen-fullscreen')
  }
}

/** 动态计算安全库存（与后端/InventoryList.vue getStockStatus 统一） */
const getDynamicSafety = (row: Inventory): number => {
  const material = typeof row.material === 'object' ? row.material : null
  const dbSafety = material?.safety_stock
  if (dbSafety && Number(dbSafety) !== 200) return Number(dbSafety)
  const qty = row.quantity || 0
  const dailyUsage = Math.max(qty / 30, 10)
  const cost = material?.standard_cost || 0
  const leadTime = (material as any)?.lead_time || 7
  const rf = cost > 500 ? 1.5 : cost > 100 ? 1.3 : 1.2
  return Math.max(Math.min(Math.round(dailyUsage * leadTime * rf), Math.round(qty * 0.3)), 20)
}

const inventoryStats = computed(() => {
  const total = inventoryData.value.reduce((sum, r) => sum + r.quantity, 0)
  const hold = inventoryData.value.filter(r => r.is_hold).reduce((sum, r) => sum + r.quantity, 0)
  const lowStock = inventoryData.value.filter(r => r.quantity < getDynamicSafety(r) * 0.5).length
  const totalValue = inventoryData.value.reduce((sum, r) => {
    const material = typeof r.material === 'object' ? r.material : null
    return sum + (material?.standard_cost || 0) * r.quantity
  }, 0)
  return { total, hold, lowStock, totalValue }
})

// ========== KPI 数据（来自API）==========
/** 安全格式化数值，防止 NaN / undefined / null 显示 */
const safeVal = (val: unknown): string => {
  if (val === null || val === undefined) return '0'
  // 已格式化的字符串（含单位）直接透传
  const s = String(val).trim()
  if (/[%天万¥元]/.test(s)) return s
  const n = Number(val)
  return (isNaN(n) || !isFinite(n)) ? '0' : String(n)
}

const kpiData = computed(() => {
  const raw = screenData.value.kpi_data || []
  return raw.map(k => ({
    ...k,
    value: safeVal(k.value),
    change: String(k.change || '')
  }))
})

// ========== 库存KPI（来自API或本地计算）==========
const inventoryKpi = computed(() => {
  if (screenData.value.inventory_kpi && screenData.value.inventory_kpi.length > 0) {
    return screenData.value.inventory_kpi.map(item => ({
      title: item.title,
      value: safeVal(item.value),
      color: item.color || '#00d4ff'
    }))
  }
  // fallback：从本地库存数据计算
  return [
    { title: '库存总量', value: Math.round(Number(inventoryStats.value.total || 0)).toLocaleString(), color: '#00d4ff' },
    { title: 'Hold量', value: Math.round(Number(inventoryStats.value.hold || 0)).toLocaleString(), color: '#ff4d6a' },
    { title: '低库存SKU', value: String(inventoryStats.value.lowStock || 0), color: '#ffa500' },
    { title: '库存价值', value: `¥${Number((inventoryStats.value.totalValue || 0) / 10000).toFixed(2)}万`, color: '#00ffaa' }
  ]
})

// ========== 圆环完成率（优先使用物料计划结果的avg_complete_rate）==========
const ringCompleteRate = computed(() => {
  const ps = screenData.value.planning_status || emptyPlanningStatus
  // 优先使用物料计划的平均完成率
  if (ps.has_data && ps.avg_complete_rate > 0) {
    return Math.min(ps.avg_complete_rate / 100, 1)  // 转为0~1比例
  }
  // 无物料计划数据时从原始order_status计算
  const os = screenData.value.order_status || emptyOrderStatus
  const complete = Number(os.complete || 0) + Number(os.shipped || 0) + Number(os.delivered || 0)
  const total = Object.values(os).reduce((s, v) => s + (Number(v) || 0), 0)
  if (total === 0) return 0
  return complete / total
})

// ========== 订单趋势图（来自API）==========
const orderTrendOption = computed(() => {
  const trend = screenData.value.order_trend
  const categories = trend?.categories || ['1月', '2月', '3月', '4月', '5月', '6月']
  const sales = trend?.sales || []
  const purchase = trend?.purchase || []

  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(2, 8, 20, 0.95)',
      borderColor: 'rgba(0, 212, 255, 0.4)',
      textStyle: { color: '#c8e0f8', fontSize: 12 },
      axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0, 212, 255, 0.08)' } }
    },
    legend: {
      data: ['销售订单', '采购订单'],
      textStyle: { color: '#7aa2c8', fontSize: 12 },
      top: 10, right: 20,
      itemWidth: 14, itemHeight: 10, borderRadius: 2
    },
    grid: { left: '3%', right: '4%', bottom: '3%', top: 55, containLabel: true },
    xAxis: {
      type: 'category',
      data: categories,
      axisLine: { lineStyle: { color: 'rgba(0, 180, 220, 0.18)' } },
      axisLabel: { color: '#6a8caa', fontSize: 12 },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { color: '#6a8caa', fontSize: 12 },
      splitLine: { lineStyle: { color: 'rgba(0, 180, 220, 0.06)', type: 'dashed' } }
    },
    series: [
      {
        name: '销售订单',
        type: 'bar',
        data: sales.length ? sales : [86, 92, 105, 128, 168, 121],
        itemStyle: {
          color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: '#00d4ff' }, { offset: 1, color: '#0066cc' }] },
          borderRadius: [3, 3, 0, 0]
        },
        barWidth: '36%'
      },
      {
        name: '采购订单',
        type: 'bar',
        data: purchase.length ? purchase : [42, 48, 56, 72, 95, 68],
        itemStyle: {
          color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: '#00ffaa' }, { offset: 1, color: '#00bb77' }] },
          borderRadius: [3, 3, 0, 0]
        },
        barWidth: '36%'
      }
    ]
  }
})

// ========== 物料状态分布（来自API）==========
const materialStatusOption = computed(() => {
  const statusList = screenData.value.material_status || []
  const hasRealData = statusList.some(s => s.value > 0)

  return {
    tooltip: {
      trigger: 'item',
      confine: true,
      appendToBody: false,
      formatter: '{b}: {c} ({d}%)',
      backgroundColor: 'rgba(2, 8, 20, 0.95)',
      borderColor: 'rgba(0, 212, 255, 0.4)',
      textStyle: { color: '#c8e0f8', fontSize: 12 },
      extraCssText: 'z-index:9999;box-shadow:0 0 12px rgba(0,212,255,0.3);'
    },
    legend: {
      orient: 'vertical', right: '4%', top: 'center',
      textStyle: { color: '#7aa2c8', fontSize: 12 },
      itemWidth: 10, itemHeight: 10, itemGap: 12
    },
    series: [{
      name: '物料状态',
      type: 'pie',
      radius: ['38%', '66%'],
      center: ['40%', '52%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 8, borderColor: '#061020', borderWidth: 2 },
      label: {
        show: true,
        position: 'outside',
        formatter: '{b}\n{c} ({d}%)',
        color: '#c8e0f8',
        fontSize: 11,
        lineHeight: 15,
        overflow: 'none'
      },
      labelLine: {
        show: true,
        length: 10,
        length2: 6,
        lineStyle: { color: 'rgba(0, 212, 255, 0.3)' }
      },
      emphasis: {
        label: { show: true, fontSize: 14, fontWeight: 'bold', color: '#fff' },
        itemStyle: { shadowBlur: 25, shadowColor: 'rgba(0,0,0,0.6)' }
      },
      data: hasRealData ? statusList.map((item: any) => ({
        value: item.value,
        name: item.name,
        itemStyle: { color: item.color || (item.name.includes('充足') ? '#00ffaa' : item.name.includes('接近') ? '#ffa500' : '#ff4d6a') }
      })) : [
        { value: 186, name: '库存充足', itemStyle: { color: '#00ffaa' } },
        { value: 62, name: '接近安全库存', itemStyle: { color: '#ffa500' } },
        { value: 38, name: '库存不足', itemStyle: { color: '#ff4d6a' } }
      ]
    }]
  }
})

// ========== 产能利用率仪表盘（来自API）==========
const capacityOdometerOption = computed(() => {
  const utilization = screenData.value.capacity?.utilization ?? 78.6

  return {
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      center: ['50%', '56%'],
      radius: '66%',
      min: 0, max: 100, splitNumber: 5,
      axisLine: {
        lineStyle: { width: 10, color: [[0.3, '#00ffaa'], [0.7, '#ffa500'], [1, '#ff4d6a']] }
      },
      pointer: { itemStyle: { color: '#00d4ff' }, length: '46%', width: 5 },
      axisTick: { show: false },
      splitLine: { show: false },
      splitArea: { show: false },
      axisLabel: { show: false },
      detail: {
        valueAnimation: true,
        formatter: (val: number) => Number(val).toFixed(1) + '%',
        color: '#00d4ff',
        fontSize: 22,
        fontWeight: 800,
        fontFamily: "'DIN Alternate', 'Orbitron', monospace",
        offsetCenter: [0, '42%'],
        textShadowColor: 'rgba(0, 212, 255, 0.6)',
        textShadowBlur: 15
      },
      data: [{ value: Number(utilization.toFixed(1)) }]
    }]
  }
})

// ========== 质量雷达图（来自API）==========
const qualityRadarOption = computed(() => {
  const qr = screenData.value.quality_radar
  const indicators = qr?.indicators || [
    { name: '来料合格率', max: 100 }, { name: '成品合格率', max: 100 },
    { name: '准时交付率', max: 100 }, { name: '客户满意度', max: 100 }, { name: '设备稼动率', max: 100 }
  ]
  const cm = qr?.current_month || []
  const lm = qr?.last_month || []

  const hasCurrentMonthData = cm.some((v: number) => v > 0)

  return {
    tooltip: { backgroundColor: 'rgba(2, 8, 20, 0.95)', borderColor: 'rgba(0, 212, 255, 0.4)', textStyle: { color: '#c8e0f8' } },
    legend: { data: ['本月', '上月'], textStyle: { color: '#7aa2c8', fontSize: 11 }, top: 0, right: 20, itemWidth: 12, itemHeight: 10, itemGap: 16 },
    radar: {
      indicator: indicators,
      center: ['50%', '54%'], radius: '62%',
      axisName: { color: '#7aa2c8', fontSize: 11 },
      splitArea: { areaStyle: { color: ['rgba(0, 212, 255, 0.03)', 'rgba(0, 212, 255, 0.07)'] } },
      axisLine: { lineStyle: { color: 'rgba(0, 180, 220, 0.12)' } },
      splitLine: { lineStyle: { color: 'rgba(0, 180, 220, 0.08)' } }
    },
    series: [{
      type: 'radar',
      data: [
        {
          value: hasCurrentMonthData ? cm : [96.5, 98.2, 94.8, 92.3, 85.6],
          name: '本月',
          areaStyle: { color: 'rgba(0, 212, 255, 0.22)' },
          lineStyle: { color: '#00d4ff', width: 2 },
          itemStyle: { color: '#00d4ff' }
        },
        {
          value: (lm.length && lm.some((v: number) => v > 0)) ? lm : [95.1, 97.5, 93.2, 91.0, 83.2],
          name: '上月',
          areaStyle: { color: 'rgba(0, 255, 170, 0.12)' },
          lineStyle: { color: '#00ffaa', width: 2, type: 'dashed' },
          itemStyle: { color: '#00ffaa' }
        }
      ]
    }]
  }
})

// ========== 订单状态分布（优先使用物料计划结果planning_status）==========
const orderStatusItems = computed(() => {
  const os = screenData.value.order_status || emptyOrderStatus
  const ps = screenData.value.planning_status || emptyPlanningStatus

  // 优先使用物料计划后的真实齐套数据
  if (ps.has_data && ps.total > 0) {
    const total = ps.total || 1
    return [
      { tag: '完全齐套', val: ps.complete || 0, clr: 'green' },
      { tag: '部分齐套', val: ps.partial || 0, clr: 'yellow' },
      { tag: '未分配', val: ps.none || 0, clr: 'cyan' }
    ].map(it => ({ ...it, pct: it.val / total }))
  }

  // 无物料计划数据时使用原始订单状态
  const total = Object.values(os).reduce((s, v) => s + (Number(v) || 0), 0) || 1
  const items = [
    { tag: '待处理', val: os.pending || 0, clr: 'cyan' },
    { tag: '已确认', val: os.confirmed || 0, clr: 'purple' },
    { tag: '生产中', val: os.in_production || 0, clr: 'teal' },
    { tag: '已占料', val: os.allocated || 0, clr: 'blue' },
    { tag: '部分齐套', val: os.partial || 0, clr: 'yellow' },
    { tag: '处理中', val: os.processing || 0, clr: 'orange' },
    { tag: '已完成', val: (os.complete || 0) + (os.shipped || 0) + (os.delivered || 0), clr: 'green' },
    { tag: '已取消', val: os.cancelled || 0, clr: 'red' }
  ]
  return items.map(it => ({ ...it, pct: it.val / total }))
})

// ========== 供应商地域分布（来自API）==========
const supplierDistributionOption = computed(() => {
  const dist = screenData.value.supplier_distribution
  const cities = dist?.cities || ['深圳', '上海', '广州', '杭州', '北京']
  const values = dist?.values || [12, 18, 15, 8, 10]

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(2, 8, 20, 0.95)',
      borderColor: 'rgba(0, 212, 255, 0.4)',
      textStyle: { color: '#c8e0f8', fontSize: 12 },
      formatter: '{b}: {c} 家'
    },
    grid: { left: '3%', right: '10%', top: '6%', bottom: '4%', containLabel: true },
    xAxis: { type: 'value', show: false },
    yAxis: {
      type: 'category',
      data: cities.reverse(),
      axisLine: { lineStyle: { color: 'rgba(0,180,220,0.15)' } },
      axisTick: { show: false },
      axisLabel: { color: '#7aa2c8', fontSize: 11 }
    },
    series: [{
      type: 'bar',
      data: values.reverse().map((v: number) => ({
        value: v,
        itemStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
            colorStops: [{ offset: 0, color: '#00d4ff' }, { offset: 1, color: '#00ffaa' }]
          },
          borderRadius: [0, 4, 4, 0]
        }
      })),
      barWidth: '50%',
      label: { show: true, position: 'right', color: '#00d4ff', fontSize: 11, fontWeight: 600 }
    }]
  }
})

// ========== 最近订单列表（来自API）==========
const recentOrdersList = computed(() => {
  const orders = screenData.value.recent_orders
  if (orders && orders.length > 0) {
    return orders.map(o => ({
      orderNo: o.order_no,
      customer: o.customer,
      status: o.status,
      amount: o.amount,
      progress: o.progress
    }))
  }
  return []
})

// ========== 实时告警列表（来自API）==========
const recentAlertList = computed(() => {
  const alerts = screenData.value.alerts
  if (alerts && alerts.length > 0) {
    return alerts.map(a => ({
      id: a.id,
      type: a.type as 'primary' | 'success' | 'warning' | 'info' | 'danger',
      message: a.message,
      time: a.time
    }))
  }
  // 无数据时显示默认提示
  return [
    { id: 1, type: 'info' as const, message: '系统运行正常，所有服务可用', time: '实时' }
  ]
})

// ========== 数据加载 ==========
const loadInventoryData = async () => {
  try {
    const res = await getInventoryList({ page: 1, page_size: 9999 }, silentRequestOptions)
    inventoryData.value = res?.results || []
  } catch { inventoryData.value = [] }
}

const isRefreshing = ref(false)

const refreshData = async () => {
  if (isRefreshing.value) return
  isRefreshing.value = true
  try {
    const screenRes = await getScreenData(silentRequestOptions).catch(() => null)
    if (screenRes) screenData.value = screenRes
    await loadInventoryData()
  } catch (error) {
    console.error('刷新大屏数据失败:', error)
  } finally {
    isRefreshing.value = false
  }
}

const updateTime = () => {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  currentTime.value = `${now.getFullYear()}年${pad(now.getMonth()+1)}月${pad(now.getDate())}日 ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
}

onMounted(() => {
  updateTime()
  refreshData()
  timeInterval = window.setInterval(updateTime, 1000)
  dataInterval = window.setInterval(() => { if (!isRefreshing.value) refreshData() }, 60000)
  document.addEventListener('fullscreenchange', onFullscreenChange)
})

onUnmounted(() => {
  if (timeInterval) clearInterval(timeInterval)
  if (dataInterval) clearInterval(dataInterval)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  document.body.classList.remove('screen-fullscreen')
})
</script>

<template>
  <div class="screen-container">
    <!-- 多层背景 -->
    <div class="bg-layer">
      <!-- 主网格 -->
      <div class="grid-main"></div>
      <!-- 细网格叠加 -->
      <div class="grid-fine"></div>
      <!-- 光晕球体群 -->
      <div class="orb orb-a"></div>
      <div class="orb orb-b"></div>
      <div class="orb orb-c"></div>
      <div class="orb orb-d"></div>
      <!-- 顶部渐变遮罩 -->
      <div class="vignette-top"></div>
      <!-- 底部渐变遮罩 -->
      <div class="vignette-bottom"></div>
      <!-- 水平扫描线 -->
      <div class="scan-line-h"></div>
      <!-- 垂直扫描线 -->
      <div class="scan-line-v"></div>
      <!-- 粒子容器 -->
      <div class="particles">
        <span v-for="(p, i) in particles" :key="i" class="p" :style="{left: p.left, top: p.top, animationDelay: p.delay, animationDuration: p.duration}"></span>
      </div>
    </div>

    <!-- 外框架（双层边框） -->
    <div class="frame-outer">
      <!-- 内框架 -->
      <div class="frame-inner">
        <!-- 四角L型装饰 - 外层 -->
        <div class="corner-lt corner-deco"></div>
        <div class="corner-rt corner-deco"></div>
        <div class="corner-lb corner-deco"></div>
        <div class="corner-rb corner-deco"></div>
        <!-- 四角小方块装饰 -->
        <i class="dot-corn dot-tl"></i><i class="dot-corn dot-tr"></i>
        <i class="dot-corn dot-bl"></i><i class="dot-corn dot-br"></i>

        <!-- 顶栏 -->
        <header class="header-bar">
          <!-- 左侧导航 -->
          <nav class="nav-tabs">
            <button v-for="tab in [
              {key:'overview',label:'总览概览'},
              {key:'ai-analysis',label:'智能分析'},
              {key:'digital-twin',label:'数字孪生'}
            ]" :key="tab.key"
              class="nav-item" :class="{active: activeTab === tab.key}"
              @click="switchTab(tab.key)">
              {{ tab.label }}
            </button>
          </nav>

          <!-- 中央标题 -->
          <h1 class="main-title">
            <span class="title-icon">◆</span>
            联宝智能供应链管理系统
            <span class="title-icon">◆</span>
          </h1>

          <!-- 右侧时间信息 -->
          <div class="hdr-right">
            <button class="fullscreen-btn" :class="{active: isFullscreen}" @click="toggleFullscreen" :title="isFullscreen ? '退出全屏' : '全屏显示'">
              <svg v-if="!isFullscreen" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
              <svg v-else viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
            </button>
            <span class="time-str">{{ currentTime }}</span>
            <span class="year-tag">{{ new Date().getFullYear() }}年度</span>
          </div>
        </header>

        <!-- 主内容区 -->
        <main class="body-area" v-if="activeTab === 'overview'">
          <!-- 第一行：核心数据 -->
          <section class="row row-1">
            <!-- 左面板：业务指标 -->
            <div class="panel panel-kpi">
              <div class="panel-head">
                <span class="ph-diamond">◆</span>
                <span class="ph-title">业务概况</span>
                <span class="ph-line"></span>
              </div>
              <div class="panel-body kpi-body">
                <div v-for="(k,i) in kpiData.filter(k => !k.title.includes('齐套') && !k.title.includes('金额') && !k.title.includes('产能'))" :key="'k'+i" class="kpi-item">
                  <div class="kpi-top"><span class="kpi-name">{{ k.title }}</span></div>
                  <div class="kpi-val-wrap">
                    <span class="kpi-num" :style="{color:k.color, '--glow': k.color}">{{ k.value }}</span>
                    <span class="kpi-pulse" :style="{background:k.color}"></span>
                  </div>
                  <div class="kpi-bot"><span class="kpi-chg" :class="{up:k.change.startsWith('+')}">{{ k.change }}</span><span class="kpi-trend-arrow">↗</span></div>
                </div>
                <div v-for="(k,i) in inventoryKpi.filter(k => !k.title.includes('总量') && !k.title.includes('价值'))" :key="'inv'+i" class="kpi-item">
                  <div class="kpi-top"><span class="kpi-name">{{ k.title }}</span></div>
                  <div class="kpi-val-wrap">
                    <span class="kpi-num" :style="{color:k.color, '--glow': k.color}">{{ k.value }}</span>
                    <span class="kpi-pulse" :style="{background:k.color}"></span>
                  </div>
                  <div class="kpi-bot"><span class="kpi-spark"></span></div>
                </div>
              </div>
            </div>

            <!-- 中间：大数字展示（上3下3） -->
            <div class="panel panel-center">
              <div class="center-content">
                <!-- 上排3个 -->
                <div class="mega-row">
                  <div class="mega-card" v-for="(mc,idx) in [
                    {label:'销售订单总数', val: kpiData[0]?.value||'0', sub:`${kpiData[1]?.value||0} 已完成`, cls:'cyan'},
                    {label: inventoryKpi[3]?.title || '库存价值', val: inventoryKpi[3]?.value||'¥0万', sub:`${inventoryKpi[2]?.value||0} 低库存SKU`, cls:'green'},
                    {label:'物料齐套率', val: kpiData.find(k => k.title.includes('齐套') || k.title.includes('完成'))?.value||'0%', sub:`${inventoryKpi[2]?.value || 0} 种物料缺料`, cls:'blue'}
                  ]" :key="'t'+idx" :class="'mc-'+mc.cls">
                    <span class="mc-label">{{ mc.label }}</span>
                    <span class="mc-value" :class="'v-'+mc.cls">{{ mc.val }}</span>
                    <span class="mc-sub">{{ mc.sub }}</span>
                    <div class="mc-glow-bg" :class="'g-'+mc.cls"></div>
                  </div>
                </div>
                <!-- 下排3个 -->
                <div class="mega-row">
                  <div class="mega-card" v-for="(mc,idx) in [
                    {label:'销售订单金额', val: kpiData.find(k => k.title.includes('金额'))?.value||'¥0万', sub:kpiData.find(k => k.title.includes('金额'))?.change||'', cls:'green'},
                    {label:'产能利用率', val: kpiData.find(k => k.title.includes('产能'))?.value||'0%', sub:kpiData.find(k => k.title.includes('产能'))?.change||'', cls:'blue'},
                    {label:'库存总量', val: inventoryKpi[0]?.value||'0', sub:`Hold ${inventoryKpi[1]?.value||0}`, cls:'cyan'}
                  ]" :key="'b'+idx" :class="'mc-'+mc.cls">
                    <span class="mc-label">{{ mc.label }}</span>
                    <span class="mc-value" :class="'v-'+mc.cls">{{ mc.val }}</span>
                    <span class="mc-sub">{{ mc.sub }}</span>
                    <div class="mc-glow-bg" :class="'g-'+mc.cls"></div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 右面板：状态分布 -->
            <div class="panel panel-status">
              <div class="panel-head">
                <span class="ph-diamond">◆</span>
                <span class="ph-title">订单状态分布</span>
                <span class="ph-line"></span>
              </div>
              <div class="panel-body status-body">
                <div class="status-group">
                  <div class="st-item" v-for="(st,si) in orderStatusItems" :key="si">
                    <span class="st-label" :class="'sl-'+st.clr">{{ st.tag }}</span>
                    <div class="st-bar-track"><div class="st-bar-fill" :class="'sf-'+st.clr" :style="{width: Math.max(3,st.pct*100)+'%'}"></div></div>
                    <span class="st-count">{{ st.val }}</span>
                  </div>
                </div>
                <div class="ring-section">
                  <svg viewBox="0 0 120 120" class="ring-chart">
                    <circle cx="60" cy="60" r="50" fill="none" stroke="#0a1e36" stroke-width="10"/>
                    <circle cx="60" cy="60" r="50" fill="none" stroke="url(#ringGrad)" stroke-width="10"
                      stroke-linecap="round"
                      :stroke-dasharray="314" :stroke-dashoffset="314*(1-(ringCompleteRate||0))"
                      transform="rotate(-90 60 60)" style="transition:stroke-dashoffset 1.5s cubic-bezier(.22,.61,.36,1)"/>
                    <defs>
                      <linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                        <stop offset="0%" stop-color="#00d4ff"/>
                        <stop offset="100%" stop-color="#00ffaa"/>
                      </linearGradient>
                    </defs>
                  </svg>
                  <div class="ring-center">
                    <span class="rc-num">{{ (ringCompleteRate*100).toFixed(0) }}<small>%</small></span>
                    <span class="rc-lbl">完成率</span>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- 第二行：图表分析 -->
          <section class="row row-2">
            <div class="panel panel-chart-main">
              <div class="panel-head">
                <span class="ph-diamond">◆</span>
                <span class="ph-title">订单趋势分析</span>
                <span class="ph-badge">月度</span>
                <span class="ph-line"></span>
              </div>
              <div class="chart-wrap"><v-chart :option="orderTrendOption" autoresize /></div>
            </div>
            <div class="panel-col">
              <div class="panel panel-sm">
                <div class="panel-head-sm">▸ 物料状态分布</div>
                <div class="chart-wrap"><v-chart :option="materialStatusOption" autoresize /></div>
              </div>
              <div class="panel panel-sm">
                <div class="panel-head-sm">▸ 产能利用率</div>
                <div class="chart-wrap gauge-wrap"><v-chart :option="capacityOdometerOption" autoresize /></div>
              </div>
            </div>
          </section>

          <!-- 第三行：底部详情 -->
          <section class="row row-3">
            <div class="panel-col-bottom">
              <div class="panel panel-radar">
                <div class="panel-head">
                  <span class="ph-diamond">◆</span>
                  <span class="ph-title">质量指标对比</span>
                  <span class="ph-line"></span>
                </div>
                <div class="chart-wrap"><v-chart :option="qualityRadarOption" autoresize /></div>
              </div>
              <div class="panel panel-supplier">
                <div class="panel-head">
                  <span class="ph-diamond">◆</span>
                  <span class="ph-title">供应商地域分布</span>
                  <span class="ph-line"></span>
                </div>
                <div class="chart-wrap"><v-chart :option="supplierDistributionOption" autoresize /></div>
              </div>
            </div>
            <div class="panel-col-right">
              <div class="panel panel-alerts">
                <div class="panel-head">
                  <span class="ph-diamond">◆</span>
                  <span class="ph-title">实时告警中心</span>
                  <span class="ph-alert-count">{{ recentAlertList.length }} 条</span>
                  <span class="ph-line"></span>
                </div>
                <div class="alert-scroll">
                  <div v-for="a in recentAlertList.slice(0,6)" :key="a.id" class="alert-row">
                    <span class="ar-dot" :class="'ad-'+a.type"></span>
                    <span class="ar-msg">{{ a.message }}</span>
                    <span class="ar-time">{{ a.time }}</span>
                  </div>
                </div>
              </div>
              <div class="panel panel-orders">
                <div class="panel-head">
                  <span class="ph-diamond">◆</span>
                  <span class="ph-title">最近订单</span>
                  <span class="ph-line"></span>
                </div>
                <div class="order-table-wrap">
                  <table class="order-mini-table" v-if="recentOrdersList.length > 0">
                    <thead><tr><th>订单号</th><th>客户</th><th>金额</th><th>进度</th></tr></thead>
                    <tbody>
                      <tr v-for="(o,i) in recentOrdersList.slice(0,5)" :key="i">
                        <td class="ot-order-no">{{ o.orderNo }}</td>
                        <td>{{ o.customer }}</td>
                        <td class="ot-amount">¥{{ o.amount?.toLocaleString() }}</td>
                        <td>
                          <div class="ot-progress-track"><div class="ot-progress-fill" :style="{width:o.progress+'%'}"></div></div>
                          <span class="ot-pct">{{ o.progress }}%</span>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-else class="order-empty">暂无订单数据</div>
                </div>
              </div>
            </div>
          </section>
        </main>

        <!-- 子模块Tab内容 -->
        <main class="body-area body-tab" v-else-if="activeTab === 'ai-analysis'">
          <component :is="AIAnalysisComponent" />
        </main>
        <main class="body-area body-tab" v-else-if="activeTab === 'digital-twin'">
          <component :is="DigitalTwinComponent" />
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
// ==================== 色彩系统 ====================
$bg-void: #010816;
$bg-deep: #030c1a;
$panel-bg: rgba(5, 18, 42, 0.88);
$border-bright: rgba(0, 210, 255, 0.5);
$border-dim: rgba(0, 160, 210, 0.15);
$border-glass: rgba(0, 200, 240, 0.08);
$cyan: #00e0ff;
$cyan-light: #90f7ff;
$green: #00ffa0;
$yellow: #ffc020;
$red: #ff3860;
$text-main: #d0e4f8;
$text-sub: #4a78a0;
$header-from: #052248;
$header-to: #091a32;
$accent-blue: #0088ff;
$accent-teal: #00ddbb;

// ==================== 容器 ====================
.screen-container {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
  background: $bg-void;
  overflow: hidden;
  font-family: 'Microsoft YaHei', 'PingFang SC', -apple-system, sans-serif;
  color: $text-main;
}

// ==================== 背景层 ====================
.bg-layer {
  position: fixed; inset: 0; pointer-events: none; z-index: 0;

  .grid-main {
    position: absolute; inset: 0;
    background-image:
      linear-gradient(rgba(0, 190, 230, 0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 190, 230, 0.04) 1px, transparent 1px);
    background-size: 56px 56px;
  }

  .grid-fine {
    position: absolute; inset: 0;
    background-image:
      linear-gradient(rgba(0, 190, 230, 0.015) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 190, 230, 0.015) 1px, transparent 1px);
    background-size: 14px 14px;
  }

  .orb {
    position: absolute; border-radius: 50%; filter: blur(120px);
    &.orb-a { width: 800px; height: 800px; background: radial-gradient(circle, rgba(0,160,230,0.13), transparent 70%); top: -350px; left: -250px; opacity: 0.7; }
    &.orb-b { width: 600px; height: 600px; background: radial-gradient(circle, rgba(0,230,170,0.09), transparent 70%); bottom: -220px; right: -180px; opacity: 0.55; }
    &.orb-c { width: 400px; height: 400px; background: radial-gradient(circle, rgba(0,140,255,0.07), transparent 70%); top: 40%; left: 30%; opacity: 0.4; }
    &.orb-d { width: 300px; height: 300px; background: radial-gradient(circle, rgba(0,220,200,0.06), transparent 70%); top: 20%; right: 15%; opacity: 0.35; }
  }

  .vignette-top {
    position: absolute; top: 0; left: 0; right: 0; height: 180px;
    background: linear-gradient(180deg, rgba(1,8,22,0.85) 0%, transparent 100%);
  }

  .vignette-bottom {
    position: absolute; bottom: 0; left: 0; right: 0; height: 200px;
    background: linear-gradient(0deg, rgba(1,8,22,0.9) 0%, transparent 100%);
  }

  .scan-line-h {
    position: absolute; left: 0; right: 0; top: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, $cyan 40%, $cyan 60%, transparent 100%);
    opacity: 0.08; animation: scanH 6s ease-in-out infinite;
  }
  @keyframes scanH {
    0% { top: 0; opacity: 0.03; }
    50% { top: 100vh; opacity: 0.12; }
    100% { top: 100vh; opacity: 0.03; }
  }

  .scan-line-v {
    position: absolute; top: 0; bottom: 0; left: 0; width: 1px;
    background: linear-gradient(180deg, transparent 0%, rgba(0,220,255,0.25) 50%, transparent 100%);
    opacity: 0.06; animation: scanV 8s ease-in-out infinite;
  }
  @keyframes scanV {
    0% { left: 0; opacity: 0.02; }
    50% { left: 100vw; opacity: 0.1; }
    100% { left: 100vw; opacity: 0.02; }
  }

  .particles {
    position: absolute; inset: 0;
    .p {
      position: absolute; width: 2px; height: 2px; border-radius: 50%;
      background: rgba(0, 210, 255, 0.5); box-shadow: 0 0 4px rgba(0,210,255,0.3);
      animation: pFloat 5s ease-in-out infinite;
    }
  }
  @keyframes pFloat {
    0%,100% { opacity: 0.1; transform: translateY(0) scale(1); }
    50% { opacity: 0.7; transform: translateY(-20px) scale(1.5); }
  }
}

// ==================== 外框架（双层边框）====================
.frame-outer {
  position: relative; z-index: 1; margin: 8px;
  border: 2px solid $border-bright;
  border-radius: 3px;
  background: rgba(2, 10, 26, 0.5);
  box-shadow:
    0 0 40px rgba(0, 180, 230, 0.1),
    0 0 80px rgba(0, 120, 200, 0.05),
    inset 0 0 80px rgba(0, 20, 50, 0.5);
  display: flex;
  flex-direction: column;
  height: calc(100% - 16px);
  min-height: 0;

  // L型四角装饰
  .corner-deco {
    position: absolute; width: 48px; height: 48px;
    &::before, &::after {
      content: ''; position: absolute; background: $cyan;
      box-shadow: 0 0 12px rgba(0, 224, 255, 0.6), 0 0 24px rgba(0, 224, 255, 0.25);
    }
    // 左上
    &.corner-lt { top: -2px; left: -2px;
      &::before { width: 48px; height: 3px; top: 0; left: 0; }
      &::after { width: 3px; height: 48px; top: 0; left: 0; }
    }
    // 右上
    &.corner-rt { top: -2px; right: -2px;
      &::before { width: 48px; height: 3px; top: 0; right: 0; }
      &::after { width: 3px; height: 48px; top: 0; right: 0; }
    }
    // 左下
    &.corner-lb { bottom: -2px; left: -2px;
      &::before { width: 48px; height: 3px; bottom: 0; left: 0; }
      &::after { width: 3px; height: 48px; bottom: 0; left: 0; }
    }
    // 右下
    &.corner-rb { bottom: -2px; right: -2px;
      &::before { width: 48px; height: 3px; bottom: 0; right: 0; }
      &::after { width: 3px; height: 48px; bottom: 0; right: 0; }
    }
  }

  // 内框
  .frame-inner {
    border: 1px solid rgba(0, 180, 230, 0.12);
    border-radius: 2px;
    margin: 3px;
    position: relative;
    display: flex;
    flex-direction: column;
    height: calc(100% - 6px);
    min-height: 0;

    // 角落小方块
    .dot-corn {
      position: absolute; width: 8px; height: 8px;
      background: $cyan; box-shadow: 0 0 10px rgba(0,224,255,0.5);
      &.dot-tl { top: 8px; left: 8px; }
      &.dot-tr { top: 8px; right: 8px; }
      &.dot-bl { bottom: 8px; left: 8px; }
      &.dot-br { bottom: 8px; right: 8px; }
    }
  }
}

// ==================== 头部栏 ====================
.header-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 24px;
  background: linear-gradient(180deg, $header-from 0%, $header-to 65%, rgba(3,12,26,0.98) 100%);
  border-bottom: 1px solid $border-dim;
  position: relative;
  min-height: 50px;
  flex-shrink: 0;

  // 底部发光线
  &::after {
    content: ''; position: absolute; bottom: 0; left: 8%; right: 8%; height: 1px;
    background: linear-gradient(90deg, transparent 0%, $cyan 20%, $cyan 80%, transparent 100%);
    opacity: 0.4;
    filter: blur(0.5px);
  }

  .nav-tabs {
    display: flex; gap: 2px; flex-shrink: 0;
    .nav-item {
      padding: 5px 14px; font-size: 12.5px; color: $text-sub;
      background: rgba(0, 30, 60, 0.3);
      border: 1px solid rgba(0, 180, 230, 0.12);
      border-radius: 2px; cursor: pointer;
      transition: all 0.35s cubic-bezier(.25,.46,.45,.94);
      letter-spacing: 1.5px;
      position: relative; overflow: hidden;

      &:hover {
        color: $cyan-light; border-color: rgba(0, 224, 255, 0.3);
        background: rgba(0, 160, 220, 0.08);
        box-shadow: 0 0 15px rgba(0, 200, 240, 0.08);
      }

      &.active {
        color: #fff;
        border-color: rgba(0, 224, 255, 0.55);
        background: linear-gradient(135deg, rgba(0, 120, 200, 0.25), rgba(0, 80, 160, 0.15));
        text-shadow: 0 0 15px rgba(0, 224, 255, 0.6), 0 0 30px rgba(0, 224, 255, 0.2);
        box-shadow:
          inset 0 0 18px rgba(0, 180, 230, 0.1),
          0 0 12px rgba(0, 224, 255, 0.18);

        &::before {
          content: ''; position: absolute; top: 0; left: -100%; width: 100%; height: 100%;
          background: linear-gradient(90deg, transparent, rgba(0,224,255,0.08), transparent);
          animation: navShimmer 3s ease-in-out infinite;
        }
      }
    }
  }
  @keyframes navShimmer { to { left: 100%; } }

  .main-title {
    position: absolute; left: 50%; top: 50%;
    transform: translate(-50%, -50%);
    margin: 0; font-size: 20px; font-weight: 900;
    letter-spacing: 4px;
    color: $cyan-light;
    text-shadow: 0 0 20px rgba(0, 224, 255, 0.4), 0 0 40px rgba(0, 224, 255, 0.15);
    white-space: nowrap;
    .title-icon {
      font-size: 9px; color: $green;
      text-shadow: 0 0 8px rgba(0,255,160,0.8);
    }
  }

  .hdr-right {
    display: flex; align-items: center; gap: 10px; flex-shrink: 0;
    margin-left: auto;

    .fullscreen-btn {
      width: 28px; height: 28px; border-radius: 6px;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
      color: $text-sub; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: all 0.25s ease;
      &:hover { background: rgba(0,224,255,0.1); color: $cyan; border-color: rgba(0,224,255,0.3); }
      &.active { background: rgba(0,224,255,0.12); color: $cyan; border-color: rgba(0,224,255,0.35); }
    }

    .time-str {
      font-family: 'DIN Alternate', 'Courier New', Consolas, monospace;
      font-size: 13px; font-weight: 700; color: $cyan;
      letter-spacing: 1.5px;
      text-shadow: 0 0 12px rgba(0, 224, 255, 0.5);
    }

    .year-tag {
      font-size: 11px; font-weight: 700; color: $yellow;
      padding: 2px 10px; letter-spacing: 1px;
      background: rgba(255, 192, 32, 0.06);
      border: 1px solid rgba(255, 192, 32, 0.25);
      border-radius: 10px;
      text-shadow: 0 0 8px rgba(255,192,32,0.3);
    }
  }
}

// ==================== 内容区域 ====================
.body-area {
  padding: 6px 8px; flex: 1; min-height: 0;
  display: flex; flex-direction: column;
  &.body-tab {
    padding: 10px 14px;
    overflow-y: auto;
    max-height: calc(100vh - 66px);

    // ===== 数字孪生嵌入适配 =====
    :deep(.digital-twin-page) {
      max-width: 100%;

      // 头部
      .page-header {
        margin-bottom: 8px !important;
        padding-bottom: 8px !important;
      }

      // 控件栏：保持数字孪生自己的青绿/琥珀状态体系
      .control-bar {
        margin-bottom: 7px !important;
        border-radius: 8px !important;
        background:
          linear-gradient(135deg, rgba(45, 212, 191, 0.08), rgba(56, 189, 248, 0.045)),
          rgba(5, 12, 24, 0.74) !important;
        border-color: rgba(125, 211, 252, 0.18) !important;

        :deep(.el-card__body) { padding: 7px 11px !important; }
        :deep(.el-card__header) { padding: 4px 10px !important; }

        .controls { gap: 5px !important; flex-wrap: nowrap; }
        .left-controls { gap: 4px !important; flex-wrap: nowrap;
          .el-switch { transform: scale(0.88); }
          .el-select { width: 75px !important; }
        }
      }

      // 主布局：单列（画布占满宽度）
      .main-content {
        display: block !important;
        gap: 0 !important;
      }

      // 画布面板
      .network-panel { gap: 5px !important;

        .canvas-card {
          background:
            linear-gradient(145deg, rgba(7, 14, 28, 0.96), rgba(2, 7, 18, 0.98)),
            rgba(4, 10, 22, 0.94) !important;
          border-color: rgba(125, 211, 252, 0.18) !important;
          :deep(.el-card__header) { padding: 5px 9px !important; }
          :deep(.el-card__body) { padding: 5px !important; }
        }

        // Canvas 高度约束（加大）
        .canvas-container {
          height: clamp(520px, calc(100vh - 185px), 780px) !important;
          min-height: 500px !important;
          border-radius: 6px !important;

          .legend { top: 12px !important; bottom: auto !important; left: 14px !important; padding: 6px 8px !important; gap: 7px !important; border-radius: 8px !important;
            .legend-item { font-size: 9px !important;
              .dot { width: 6px !important; height: 6px !important; }
              .pulse-dot { width: 6px !important; height: 6px !important; }
            }
          }

          // 浮动面板样式
          .overlay-panel {
            font-size: 11px;
          }
          .overlay-status {
            top: 12px !important; right: 12px !important;
            width: 210px !important;
          }
          .overlay-logistics {
            bottom: 62px !important; left: 16px !important; right: auto !important;
            width: min(520px, calc(100% - 360px)) !important;
            max-height: 95px !important;
          }
          .node-popup {
            min-width: 220px !important;
          }
          .overlay-bottleneck {
            bottom: 138px !important; right: 12px !important;
          }
        }

        // KPI（画布内底部浮动）
        .overlay-kpi {
          bottom: 0 !important; left: 0 !important; right: 0 !important;
          padding: 12px 16px 8px !important;
          gap: 6px !important;
          .kpi-mini { min-width: 82px !important; padding: 5px 12px 4px !important;
            .kpi-val { font-size: 18px !important; }
            .kpi-lbl { font-size: 9.5px !important; }
          }
        }
      }
    }

    // ===== AI分析嵌入适配 =====
    :deep(.ai-analysis-page) {
      max-width: 100%;
      .page-header { margin-bottom: 8px;
        .page-title { font-size: 18px !important; }
        .page-desc { font-size: 11px !important; }
      }
      .analysis-layout {
        grid-template-columns: 320px 1fr !important;
        gap: 10px !important;
      }
      .prediction-section,
      .results-section,
      .scenarios-section {
        .el-card__header { padding: 8px 12px !important; font-size: 12px !important; }
      }
    }

    // ===== 全局子组件卡片适配 =====
    :deep(.el-card) {
      background: rgba(5, 18, 42, 0.85) !important;
      border-color: rgba(0, 160, 210, 0.15) !important;
    }
    :deep(.el-card__header) {
      background: rgba(3, 12, 30, 0.7);
      border-bottom-color: rgba(0, 160, 210, 0.1);
    }

    // 修复radio按钮文字截断（扁平选择器，确保穿透）
    :deep(.el-radio-button__inner) {
      font-size: 11px !important;
      padding: 4px 8px !important;
      white-space: nowrap !important;
      display: inline-flex !important;
      align-items: center !important;
      gap: 3px !important;
    }
    // 缩小radio按钮内的图标
    :deep(.el-radio-button .el-icon),
    :deep(.el-radio-button i),
    :deep(.el-radio-button svg) {
      width: 14px !important;
      height: 14px !important;
      font-size: 14px !important;
      --el-icon-size: 14px !important;
    }
    :deep(.el-radio-group) {
      display: flex !important;
      flex-wrap: nowrap !important;
    }
    :deep(.el-tag) { font-size: 11px; }
    :deep(.el-button--default) { font-size: 12px; }
    :deep(.el-select), :deep(.el-input) {
      --el-border-color: rgba(0, 160, 210, 0.25);
      --el-text-color-regular: #a0c4e0;
    }
  }
}

.row { display: flex; gap: 3px; margin-bottom: 6px; min-height: 0; > * { flex-shrink: 0; min-width: 0; } }
.row-1 { flex: 0 0 auto; }
.row-2 { flex: 1; min-height: 0; }
.row-3 { flex: 1; min-height: 0; }
.row:last-child { margin-bottom: 0; }

// ==================== 面板通用样式 ====================
.panel {
  background: $panel-bg;
  border: 1px solid $border-dim;
  border-radius: 2px;
  position: relative; overflow: hidden;
  display: flex; flex-direction: column;

  // 面板顶部发光条
  &::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, $cyan 15%, $cyan 85%, transparent 100%);
    opacity: 0.5;
  }

  // 面板左侧发光条
  &::after {
    content: ''; position: absolute; top: 0; bottom: 0; left: 0; width: 1px;
    background: linear-gradient(180deg, transparent 0%, rgba(0,224,255,0.3) 20%, rgba(0,224,255,0.3) 80%, transparent 100%);
    opacity: 0.25;
  }

  // 悬浮效果
  &:hover::before { opacity: 0.75; transition: opacity 0.4s; }
}

.panel-head {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 14px; border-bottom: 1px solid $border-dim;
  position: relative;

  .ph-diamond {
    color: $cyan; font-size: 9px;
    text-shadow: 0 0 6px rgba(0,224,255,0.7);
    animation: diamondSpin 8s linear infinite;
  }
  @keyframes diamondSpin { to { transform: rotate(360deg); } }

  .ph-title {
    font-size: 14px; font-weight: 800; color: $cyan-light;
    letter-spacing: 2px;
    text-shadow: 0 0 10px rgba(0,224,255,0.25);
  }

  .ph-badge {
    font-size: 10.5px; color: $text-sub; padding: 2px 10px;
    border: 1px solid $border-dim; border-radius: 10px;
    margin-left: auto;
  }

  .ph-alert-count {
    font-size: 10.5px; color: $yellow; padding: 2px 10px;
    border: 1px solid rgba(255,192,32,0.25); border-radius: 10px;
    margin-left: auto;
  }

  .ph-line {
    flex: 1; height: 1px; margin-left: 12px;
    background: linear-gradient(90deg, $border-dim, transparent);
  }
}

.panel-head-sm {
  padding: 7px 14px; font-size: 13px; font-weight: 800; color: $cyan-light;
  border-bottom: 1px solid $border-dim; letter-spacing: 1.5px;
}

.chart-wrap {
  width: 100%; flex: 1; min-height: 0;
}
.gauge-wrap {
  flex: 1;
  min-height: 0;
  padding: 0 8px 6px;
  box-sizing: border-box;
}

// ==================== 第一行布局 ====================

.panel-kpi { flex: 0 0 22%; min-width: 280px; overflow: hidden;
  .kpi-body {
    display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 6px; flex: 1;

    .kpi-item {
      padding: 4px 7px; background: rgba(0, 28, 58, 0.55);
      border: 1px solid $border-glass; border-radius: 2px;
      display: flex; flex-direction: column; justify-content: space-between; gap: 0px;
      transition: all 0.35s; position: relative; overflow: hidden;

      &:hover {
        border-color: rgba(0, 200, 240, 0.25);
        background: rgba(0, 35, 72, 0.6);
        transform: translateY(-1px);
        box-shadow: 0 4px 16px rgba(0, 100, 180, 0.1);
      }

      .kpi-top { display: flex; align-items: center; justify-content: space-between;
        .kpi-name { font-size: 9px; color: $text-sub; letter-spacing: 0.3px; }
      }

      .kpi-val-wrap {
        display: flex; align-items: baseline; gap: 3px;
        .kpi-num {
          font-size: 17px; font-weight: 900;
          font-family: 'DIN Alternate', 'Consolas', monospace;
          line-height: 1;
          text-shadow: 0 0 12px var(--glow), 0 0 24px var(--glow);
          animation: numGlow 3s ease-in-out infinite alternate;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          max-width: 140px;
        }
        @keyframes numGlow {
          from { text-shadow: 0 0 12px var(--glow), 0 0 24px var(--glow); }
          to { text-shadow: 0 0 20px var(--glow), 0 0 40px var(--glow), 0 0 65px var(--glow); }
        }

        .kpi-pulse {
          width: 5px; height: 5px; border-radius: 50%;
          animation: pulse 2s ease-in-out infinite;
          box-shadow: 0 0 5px currentColor;
          flex-shrink: 0;
        }
        @keyframes pulse {
          0%,100% { opacity: 0.3; transform: scale(0.8); }
          50% { opacity: 1; transform: scale(1.3); }
        }
      }

      .kpi-bot { display: flex; align-items: center; gap: 2px;
        .kpi-chg { font-size: 9px; color: $text-sub; font-weight: 600; &.up { color: $green; } }
        .kpi-trend-arrow { font-size: 9px; color: $green; }
        .kpi-spark { width: 18px; height: 2px; background: linear-gradient(90deg, $cyan, transparent); border-radius: 1px; opacity: 0.3; }
      }
    }
  }
}

.panel-center { flex: 1; margin: 0 3px; min-width: 0; max-width: 52%;
  .center-content {
    display: flex; flex-direction: column; justify-content: space-evenly; gap: 6px; height: 100%; padding: 6px 12px;

    .mega-row {
      display: flex; align-items: center; justify-content: space-around; gap: 10px;
    }

    .mega-card {
      position: relative; text-align: center; padding: 8px 10px;
      background: linear-gradient(145deg, rgba(0, 50, 95, 0.2), rgba(0, 18, 42, 0.08));
      border: 1px solid $border-glass; border-radius: 6px;
      flex: 1; max-width: 200px; min-width: 0;
      overflow: visible;
      transition: all 0.4s cubic-bezier(.25,.46,.45,.94);
      display: flex; flex-direction: column; gap: 2px;

      &:hover {
        transform: translateY(-2px);
        border-color: rgba(0, 200, 240, 0.2);
        box-shadow: 0 8px 30px rgba(0, 100, 180, 0.12);
      }

      .mc-label { font-size: 10px; color: $text-sub; letter-spacing: 0.3px; white-space: nowrap; }
      .mc-value {
        font-size: 22px; font-weight: 900;
        font-family: 'DIN Alternate', 'Orbitron', monospace;
        line-height: 1.1; word-break: keep-all; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis;
        &.v-cyan { color: $cyan; text-shadow: 0 0 20px rgba(0,224,255,0.35), 0 0 40px rgba(0,224,255,0.12); }
        &.v-green { color: $green; text-shadow: 0 0 20px rgba(0,255,160,0.35), 0 0 40px rgba(0,255,160,0.12); }
        &.v-blue { color: $accent-blue; text-shadow: 0 0 20px rgba(0,136,255,0.35), 0 0 40px rgba(0,136,255,0.12); }
      }
      .mc-sub { font-size: 9px; color: $text-sub; letter-spacing: 0.2px; white-space: nowrap; }

      .mc-glow-bg {
        position: absolute; top: 50%; left: 50%; width: 100px; height: 100px;
        border-radius: 50%; transform: translate(-50%, -50%);
        filter: blur(60px); opacity: 0.08; z-index: 0; pointer-events: none;
        &.g-cyan { background: $cyan; }
        &.g-green { background: $green; }
        &.g-blue { background: $accent-blue; }
      }

      > * { position: relative; z-index: 1; }
    }
  }
}

.panel-status { flex: 1; min-width: 200px; overflow: hidden;
  .status-body {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) 82px;
    align-items: center;
    column-gap: 10px;
    flex: 1 1 auto !important;
    padding: 7px 9px 6px;
    overflow: hidden;

    .status-group {
      display: flex !important;
      flex-direction: column !important;
      justify-content: center !important;
      gap: 8px;
      min-width: 0;
      .st-item {
        display: flex; align-items: center; gap: 5px;
        .st-label {
          font-size: 9px; min-width: 44px; flex-shrink: 0; font-weight: 600; letter-spacing: 0.2px;
          &.sl-cyan { color: $cyan; }
          &.sl-purple { color: #b37feb; }
          &.sl-teal { color: #36cfc9; }
          &.sl-blue { color: #597ef7; }
          &.sl-yellow { color: $yellow; }
          &.sl-green { color: $green; }
          &.sl-red { color: $red; }
        }
        .st-bar-track { flex: 1; height: 10px; background: rgba(0, 20, 48, 0.6); border-radius: 2px; overflow: hidden;
          .st-bar-fill { height: 100%; border-radius: 2px; transition: width 1.5s cubic-bezier(.22,.61,.36,1);
            &.sf-cyan { background: linear-gradient(90deg, #004499, $cyan, #00bbff); box-shadow: 0 0 6px rgba(0,224,255,0.3); }
            &.sf-purple { background: linear-gradient(90deg, #51258f, #b37feb, #d3adf7); box-shadow: 0 0 6px rgba(179,127,235,0.3); }
            &.sf-teal { background: linear-gradient(90deg, #006d75, #36cfc9, #87e8de); box-shadow: 0 0 6px rgba(54,207,201,0.3); }
            &.sf-blue { background: linear-gradient(90deg, #1d39c4, #597ef7, #85a5ff); box-shadow: 0 0 6px rgba(89,126,247,0.3); }
            &.sf-yellow { background: linear-gradient(90deg, #886600, $yellow, #ffd040); box-shadow: 0 0 6px rgba(255,192,32,0.3); }
            &.sf-green { background: linear-gradient(90deg, #006633, $green, #40ffa0); box-shadow: 0 0 6px rgba(0,255,160,0.3); }
            &.sf-red { background: linear-gradient(90deg, #a8071a, $red, #ff7875); box-shadow: 0 0 6px rgba(255,56,96,0.3); }
          }
        }
        .st-count { font-size: 11px; font-weight: 800; color: $text-main; width: 28px; text-align: right; font-family: 'DIN Alternate', monospace; }
      }
    }

    .ring-section {
      position: relative;
      width: 76px;
      height: 76px;
      display: flex;
      align-items: center;
      justify-content: center;
      justify-self: end;
      flex: 0 0 auto;
      .ring-chart { width: 68px; height: 68px; circle { transition: stroke-dashoffset 1.5s cubic-bezier(.22,.61,.36,1); } }
      .ring-center {
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -45%);
        margin: 0;
        text-align: center;
        .rc-num { font-size: 18px; font-weight: 900; color: $cyan; font-family: 'DIN Alternate', monospace; text-shadow: 0 0 10px rgba(0,224,255,0.4), 0 0 20px rgba(0,224,255,0.16);
          small { font-size: 10px; font-weight: 700; margin-left: 1px; }
        }
        .rc-lbl { font-size: 8px; color: $text-sub; display: block; margin-top: 1px; letter-spacing: 1px; }
      }
    }
  }
}

// ==================== 第二行布局 ====================
.panel-chart-main { flex: 1.8; min-width: 0; overflow: hidden; }
.panel-col { flex: 1; min-width: 300px; display: flex; flex-direction: column; gap: 8px;
  .panel-sm { flex: 1; min-width: 0; overflow: hidden; }
}

// ==================== 第三行布局 ====================
.panel-col-bottom { flex: 1; min-width: 0; display: flex; gap: 8px;
  .panel-radar { flex: 0 0 auto; min-width: 340px; overflow: hidden; }
  .panel-supplier { flex: 1; min-width: 0; overflow: hidden; }
}
.panel-col-right { flex: 1; min-width: 380px; display: flex; flex-direction: column; gap: 6px; overflow: hidden; }

// ==================== 响应式适配 ====================
@media (max-width: 1600px) {
  .panel-kpi { flex: 0 0 24%; min-width: 260px; }
  .panel-status { flex: 1; min-width: 180px; }
  .panel-chart-main { flex: 1.6; }
  .panel-col { min-width: 280px; }
  .panel-col-bottom { .panel-radar { min-width: 300px; } }
  .panel-col-right { min-width: 360px; }
}

@media (max-width: 1366px) {
  .panel-kpi { flex: 0 0 26%; min-width: 240px; }
  .panel-status { flex: 1; min-width: 160px; }
  .panel-chart-main { flex: 1.5; }
  .panel-col { min-width: 260px; }
  .panel-col-bottom { .panel-radar { min-width: 280px; } }
  .panel-col-right { min-width: 320px; }

  .kpi-body { grid-template-columns: 1fr !important; }
  .kpi-num { font-size: 14px !important; }
  .mc-value { font-size: 22px !important; }
}
.panel-alerts { flex: 1; min-height: 0; overflow: hidden;
  .alert-scroll {
    height: calc(100% - 42px); overflow-y: auto; padding: 8px;
    &::-webkit-scrollbar { width: 3px; }
    &::-webkit-scrollbar-thumb { background: $border-dim; border-radius: 2px; }
  }
  .alert-row {
    display: flex; align-items: center; gap: 10px; padding: 5px 0;
    border-bottom: 1px solid rgba(255,255,255,0.025);
    transition: background 0.2s;
    &:hover { background: rgba(0, 180, 230, 0.03); }

    .ar-dot {
      width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
      &.ad-success { background: $green; box-shadow: 0 0 8px rgba(0,255,160,0.6); }
      &.ad-warning { background: $yellow; box-shadow: 0 0 8px rgba(255,192,32,0.6); }
      &.ad-danger { background: $red; box-shadow: 0 0 8px rgba(255,56,96,0.6); }
      &.ad-info { background: $cyan; box-shadow: 0 0 8px rgba(0,224,255,0.6); }
      &.ad-primary { background: #667; }
    }
    .ar-msg { flex: 1; font-size: 11.5px; color: $text-main; overflow: hidden; text-overflow: ellipsis; }
    .ar-time { font-size: 10px; color: $text-sub; font-family: 'DIN Alternate', monospace; flex-shrink: 0; }
  }
}
.panel-orders { flex: 1; min-height: 0; overflow: hidden;
  .order-table-wrap { height: calc(100% - 42px); overflow-y: auto; padding: 4px 8px;
    &::-webkit-scrollbar { width: 3px; }
    &::-webkit-scrollbar-thumb { background: $border-dim; border-radius: 2px; }
  }
  .order-mini-table {
    width: 100%; border-collapse: collapse; font-size: 11px;
    th { color: $text-sub; font-weight: 500; padding: 3px 4px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.06); white-space: nowrap; }
    td { padding: 3px 4px; color: $text-main; border-bottom: 1px solid rgba(255,255,255,0.02); white-space: nowrap; }
    .ot-order-no { color: $cyan; font-family: 'DIN Alternate', monospace; font-size: 10.5px; max-width: 120px; overflow: hidden; text-overflow: ellipsis; }
    .ot-amount { color: $green; font-family: 'DIN Alternate', monospace; font-weight: 600; }
    .ot-progress-track { width: 40px; height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 4px; }
    .ot-progress-fill { height: 100%; background: linear-gradient(90deg, #00d4ff, #00ffaa); border-radius: 3px; transition: width 0.6s ease; }
    .ot-pct { color: $cyan; font-size: 10px; font-family: 'DIN Alternate', monospace; }
  }
  .order-empty { height: 100%; display: flex; align-items: center; justify-content: center; color: $text-sub; font-size: 12px; }
}
</style>

<!-- 全屏模式全局样式（影响侧边栏和主内容区） -->
<style lang="scss">
body.screen-fullscreen {
  .sidebar {
    width: 0 !important;
    min-width: 0 !important;
    border-right: none;
    overflow: hidden;
    padding: 0;
    &::before { display: none; }
    .sidebar-header, .menu-container, .sidebar-footer { display: none; }
  }
  .main-content {
    padding: 8px !important;
    margin-left: 0 !important;
    &::before { left: 0 !important; }
  }
}

// ==================== 非全屏模式适配（仅影响大屏页面自身） ====================
// 注意：不再使用 body:not(.screen-fullscreen) .main-content {} 选择器
// 因为那会影响到所有页面的滚动行为！大屏自身的适配通过 .screen-container 内部样式实现

.screen-container {
  // 非全屏时大屏容器内部缩放
  .frame-outer { margin: 4px; }
  .frame-inner { margin: 2px; }
  .header-bar { padding: 6px 16px; min-height: 42px;
    .nav-tabs .nav-item { padding: 4px 10px; font-size: 11px; }
    .main-title { font-size: 17px; letter-spacing: 3px; }
    .time-str { font-size: 11.5px; }
    .year-tag { font-size: 10px; padding: 1px 8px; }
  }
  .body-area { padding: 4px 6px; }

  // 第一行压缩
  .row-1 { margin-bottom: 4px; }
  .panel-kpi { flex: 0 0 24%; min-width: 240px;
    .kpi-body { max-height: 160px; gap: 3px; padding: 4px;
      .kpi-item { padding: 3px 5px;
        .kpi-top .kpi-name { font-size: 8px; }
        .kpi-val-wrap .kpi-num { font-size: 14px; max-width: 120px; }
        .kpi-bot { font-size: 8px; }
      }
    }
  }
  .panel-center { max-height: none;
    .center-content { gap: 4px; padding: 4px 8px;
      .mega-row { gap: 6px; }
      .mega-card { padding: 5px 8px;
        .mc-label { font-size: 9px; }
        .mc-value { font-size: 18px; }
        .mc-sub { font-size: 8px; }
        .mc-glow-bg { width: 80px; height: 80px; }
      }
    }
  }
  .panel-status { flex: 1; min-width: 160px;
    .status-body {
      display: grid !important;
      grid-template-columns: minmax(0, 1fr) 74px !important;
      align-items: center !important;
      column-gap: 8px !important;
      flex: 1 1 auto !important;
      padding: 5px 7px !important;

      .status-group {
        display: flex !important;
        flex-direction: column !important;
        justify-content: center !important;
        gap: 7px !important;
        min-width: 0 !important;
      }
      .st-item { gap: 4px;
        .st-label { font-size: 8px; min-width: 40px; }
        .st-count { font-size: 10px; width: 24px; }
      }
      .ring-section {
        position: relative !important;
        width: 66px !important;
        height: 66px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        justify-self: end !important;
        padding: 0 !important;

        .ring-chart { width: 60px; height: 60px; }
        .ring-center {
          position: absolute !important;
          left: 50% !important;
          top: 50% !important;
          transform: translate(-50%, -45%) !important;
          margin: 0 !important;
          .rc-num { font-size: 16px; small { font-size: 9px; } }
          .rc-lbl { font-size: 8px; }
        }
      }
    }
  }

  // 第二行
  .row-2 { margin-bottom: 4px; gap: 6px; }
  .panel-head { padding: 5px 10px;
    .ph-title { font-size: 12px; letter-spacing: 1.5px; }
    .ph-badge, .ph-alert-count { font-size: 9.5px; padding: 1px 8px; }
  }
  .panel-head-sm { padding: 5px 10px; font-size: 11.5px; }

  // 第三行
  .row-3 { gap: 6px; }
  .panel-col-bottom { gap: 6px;
    .panel-radar { min-width: 280px; }
    .panel-supplier { min-width: 0; }
  }
  .panel-col-right { min-width: 320px; gap: 4px; }

  // 面板内部
  .alert-scroll { padding: 4px 6px;
    .alert-row { padding: 3px 0; gap: 6px;
      .ar-msg { font-size: 10px; }
      .ar-time { font-size: 9px; }
    }
  }
  .order-table-wrap { padding: 2px 6px; }
  .order-mini-table { font-size: 10px;
    th { padding: 2px 3px; }
    td { padding: 2px 3px; }
    .ot-order-no { font-size: 9.5px; max-width: 100px; }
    .ot-pct { font-size: 9px; }
  }
}

@media (max-width: 767px) {
  // 移动端：只调整大屏容器自身，不再修改全局 .main-content 样式
  .screen-container {
    height: auto;
    min-height: 100vh;
    overflow-x: hidden;
  }

  .frame-outer {
    height: auto !important;
    min-height: calc(100vh - 8px);
    margin: 4px !important;
  }

  .frame-inner {
    height: auto !important;
    min-height: calc(100vh - 16px);
  }

  .header-bar {
    min-height: auto !important;
    padding: 8px 10px 10px !important;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 6px;

    &::after {
      left: 12px;
      right: 12px;
    }

    .main-title {
      position: static !important;
      transform: none !important;
      order: 0;
      width: 100%;
      text-align: center;
      white-space: normal !important;
      font-size: 16px !important;
      line-height: 1.25;
      letter-spacing: 1px !important;
    }

    .nav-tabs {
      order: 1;
      width: 100%;
      justify-content: center;
      overflow-x: auto;
      gap: 4px;

      .nav-item {
        flex: 0 0 auto;
        padding: 4px 8px !important;
        font-size: 10px !important;
        letter-spacing: 0 !important;
      }
    }

    .hdr-right {
      order: 2;
      width: 100%;
      justify-content: center;
      gap: 6px;
      margin-left: 0 !important;

      .fullscreen-btn {
        width: 26px;
        height: 26px;
      }

      .time-str {
        font-size: 10px !important;
        letter-spacing: 0 !important;
      }

      .year-tag {
        font-size: 9px !important;
        padding: 1px 6px !important;
        letter-spacing: 0 !important;
      }
    }
  }

  .body-area.body-tab {
    padding: 6px !important;
    max-height: none !important;
    overflow: visible !important;
  }

  .body-area.body-tab .digital-twin-page {
    .control-bar {
      .controls,
      .left-controls {
        flex-wrap: wrap !important;
      }
    }

    .network-panel .canvas-container {
      height: clamp(500px, calc(100vh - 165px), 780px) !important;
      min-height: 500px !important;
    }
  }
}
</style>
