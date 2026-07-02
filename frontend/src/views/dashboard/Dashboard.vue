<script setup lang="ts">
import { ref, onActivated, onDeactivated, markRaw, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import type { ECharts } from 'echarts'
import { Document, Check, Loading, PieChart as PieChartIcon, ArrowUp, Refresh, Warning } from '@element-plus/icons-vue'
import { getDashboardStats, getScreenData, getDeliveryChangeAlerts } from '@/api/dashboard'
import type {
  DashboardStats,
  OrderTrendData,
  RecentOrderItem
} from '@/types/api'

interface KPICard {
  title: string
  value: string | number
  icon: ReturnType<typeof markRaw>
  color: string
  change: string
  gradient: string
}

const loading = ref(false)
const dashboardStats = ref<DashboardStats | null>(null)
const recentOrders = ref<RecentOrderItem[]>([])
const router = useRouter()

interface DeliveryChangeAlert {
  id: number
  order_no: string
  customer_name: string
  change_count: number
  latest_change_date: string
}

const deliveryChangeAlerts = ref<DeliveryChangeAlert[]>([])
const deliveryChangeCount = ref(0)

const trendChartRef = ref<HTMLDivElement>()
const statusChartRef = ref<HTMLDivElement>()
let trendChart: ECharts | null = null
let statusChart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const kpiCards: KPICard[] = [
  {
    title: '总订单数',
    value: 0,
    icon: markRaw(Document),
    color: '#6E9EF7',
    change: '',
    gradient: 'linear-gradient(135deg, #6E9EF7, #8DB0F9)'
  },
  {
    title: '已完成',
    value: 0,
    icon: markRaw(Check),
    color: '#5DAF5A',
    change: '',
    gradient: 'linear-gradient(135deg, #5DAF5A, #7CC47A)'
  },
  {
    title: '进行中',
    value: 0,
    icon: markRaw(Loading),
    color: '#E8B84E',
    change: '',
    gradient: 'linear-gradient(135deg, #E8B84E, #F0C96E)'
  },
  {
    title: '齐套率',
    value: '0%',
    icon: markRaw(PieChartIcon),
    color: '#E57373',
    change: '',
    gradient: 'linear-gradient(135deg, #E57373, #EF9A9A)'
  }
]

const initCharts = (): void => {
  if (trendChartRef.value && !trendChart) {
    trendChart = echarts.init(trendChartRef.value)
  }
  if (statusChartRef.value && !statusChart) {
    statusChart = echarts.init(statusChartRef.value)
  }
}

const setupResizeObserver = (): void => {
  if (resizeObserver) return
  resizeObserver = new ResizeObserver(() => {
    trendChart?.resize()
    statusChart?.resize()
  })
  if (trendChartRef.value) resizeObserver.observe(trendChartRef.value)
  if (statusChartRef.value) resizeObserver.observe(statusChartRef.value)
}

const loadDashboardData = async (): Promise<void> => {
  loading.value = true
  try {
    const [statsRes, screenRes, changeAlertRes] = await Promise.all([
      getDashboardStats().catch(() => null),
      getScreenData().catch(() => null),
      getDeliveryChangeAlerts().catch(() => null)
    ])

    if (statsRes) {
      const stats: DashboardStats = statsRes
      dashboardStats.value = stats
      kpiCards[0].value = stats.total_orders || 0
      kpiCards[1].value = stats.completed_orders || 0
      kpiCards[2].value = stats.in_progress_orders || 0
      kpiCards[3].value = `${Number(stats.kit_rate || 0).toFixed(1)}%`
      if (stats.recent_orders) {
        recentOrders.value = stats.recent_orders as RecentOrderItem[]
      }
    }

    if (changeAlertRes) {
      deliveryChangeCount.value = changeAlertRes.count || 0
      deliveryChangeAlerts.value = changeAlertRes.orders || []
    }

    await nextTick()

    const trendData: OrderTrendData | undefined = screenRes?.order_trend
    const statusData = screenRes?.order_status || null

    setTimeout(() => {
      initCharts()
      const trendOption = buildTrendOption(trendData)
      const statusOption = buildStatusOption(statusData)
      trendChart?.setOption(trendOption)
      statusChart?.setOption(statusOption)
      setTimeout(setupResizeObserver, 200)
    }, 0)
  } catch (error) {
    console.error('加载仪表盘数据失败:', error)
    // 重置所有数据避免渲染残留异常数据
    recentOrders.value = []
    deliveryChangeAlerts.value = []
  } finally {
    loading.value = false
  }
}

onDeactivated(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  trendChart?.dispose()
  trendChart = null
  statusChart?.dispose()
  statusChart = null
})

const buildTrendOption = (trendData?: OrderTrendData): Record<string, unknown> => {
  let trendCategories: string[] = ['暂无数据']
  let trendOrderData: number[] = [0]
  let trendCompleteData: number[] = [0]

  if (trendData && Array.isArray(trendData.categories) && trendData.categories.length > 0) {
    trendCategories = trendData.categories
    trendOrderData = Array.isArray(trendData.sales) ? trendData.sales :
      Array.isArray(trendData.order_data) ? trendData.order_data :
      Array.isArray(trendData.total) ? trendData.total : trendData.categories.map(() => 0)
    trendCompleteData = Array.isArray(trendData.purchase) ? trendData.purchase :
      Array.isArray(trendData.complete_data) ? trendData.complete_data :
      Array.isArray(trendData.completed) ? trendData.completed : trendData.categories.map(() => 0)
  }

  return {
    title: {
      text: '订单趋势',
      textStyle: { color: '#e2e8f0', fontSize: 16, fontWeight: 600 }
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(5, 13, 26, 0.95)',
      borderColor: 'rgba(64, 158, 255, 0.2)',
      textStyle: { color: '#e2e8f0' }
    },
    legend: {
      data: ['订单数量', '完成数量'],
      textStyle: { color: '#909399' },
      top: 25
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: 80,
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: trendCategories,
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } },
      axisLabel: { color: '#606266', fontSize: 12 },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { color: '#606266', fontSize: 12 },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)', type: 'dashed' } }
    },
    series: [
      {
        name: '订单数量',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 8,
        data: trendOrderData,
        itemStyle: {
          color: '#409EFF',
          borderColor: '#0a0e27',
          borderWidth: 2
        },
        lineStyle: { width: 3 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(64, 158, 255, 0.35)' },
              { offset: 1, color: 'rgba(64, 158, 255, 0.02)' }
            ]
          }
        }
      },
      {
        name: '完成数量',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 8,
        data: trendCompleteData,
        itemStyle: {
          color: '#67C23A',
          borderColor: '#0a0e27',
          borderWidth: 2
        },
        lineStyle: { width: 3 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(103, 194, 58, 0.3)' },
              { offset: 1, color: 'rgba(103, 194, 58, 0.02)' }
            ]
          }
        }
      }
    ]
  }
}

const buildStatusOption = (statusData: unknown): Record<string, unknown> => {
  let statusMap: Record<string, number> = {
    pending: 0,
    confirmed: 0,
    in_production: 0,
    allocated: 0,
    partial: 0,
    complete: 0,
    shipped: 0,
    delivered: 0,
    cancelled: 0
  }

  if (statusData && typeof statusData === 'object' && !Array.isArray(statusData)) {
    Object.keys(statusData as Record<string, number>).forEach((key: string) => {
      const lowerKey = key.toLowerCase()
      const value = (statusData as Record<string, number>)[key]
      if (lowerKey in statusMap) {
        statusMap[lowerKey] = value
      } else if (lowerKey === 'occupied') {
        statusMap.allocated = value
      } else if (lowerKey.includes('pending') || lowerKey.includes('待处理') || lowerKey.includes('待确认')) {
        statusMap.pending = value
      } else if (lowerKey.includes('confirmed') || lowerKey.includes('已确认')) {
        statusMap.confirmed = value
      } else if (lowerKey.includes('in_production') || lowerKey.includes('生产中')) {
        statusMap.in_production = value
      } else if (lowerKey.includes('allocated') || lowerKey.includes('已占料')) {
        statusMap.allocated = value
      } else if (lowerKey.includes('processing') || lowerKey.includes('进行中')) {
        statusMap.in_production += value
      } else if (lowerKey.includes('partial') || lowerKey.includes('部分')) {
        statusMap.partial = value
      } else if (lowerKey.includes('complete') || lowerKey.includes('完成') || lowerKey.includes('齐套')) {
        statusMap.complete = value
      } else if (lowerKey.includes('shipped') || lowerKey.includes('已发货')) {
        statusMap.shipped = value
      } else if (lowerKey.includes('delivered') || lowerKey.includes('已交付')) {
        statusMap.delivered = value
      } else if (lowerKey.includes('cancelled') || lowerKey.includes('已取消')) {
        statusMap.cancelled = value
      }
    })
  } else if (Array.isArray(statusData)) {
    statusData.forEach((item: Record<string, unknown>) => {
      const name = String(item.name || item.status || item.label || '').toLowerCase()
      const value = Number(item.value || item.count || 0)
      if (name.includes('confirmed') || name.includes('已确认')) {
        statusMap.confirmed = value
      } else if (name.includes('in_production') || name.includes('生产中')) {
        statusMap.in_production = value
      } else if (name.includes('allocated') || name.includes('已占料')) {
        statusMap.allocated = value
      } else if (name.includes('pending') || name.includes('待处理') || name.includes('待确认')) {
        statusMap.pending = value
      } else if (name.includes('partial') || name.includes('部分')) {
        statusMap.partial = value
      } else if (name.includes('complete') || name.includes('完成') || name.includes('齐套')) {
        statusMap.complete = value
      } else if (name.includes('shipped') || name.includes('已发货')) {
        statusMap.shipped = value
      } else if (name.includes('delivered') || name.includes('已交付')) {
        statusMap.delivered = value
      } else if (name.includes('cancelled') || name.includes('已取消')) {
        statusMap.cancelled = value
      }
    })
  }

  return {
    title: {
      text: '订单状态分布',
      textStyle: { color: '#e2e8f0', fontSize: 16, fontWeight: 600 }
    },
    tooltip: {
      trigger: 'item',
      formatter: '{a} <br/>{b}: {c} ({d}%)',
      backgroundColor: 'rgba(5, 13, 26, 0.95)',
      borderColor: 'rgba(64, 158, 255, 0.2)',
      textStyle: { color: '#e2e8f0' }
    },
    legend: {
      orient: 'vertical',
      right: '5%',
      top: 'center',
      textStyle: { color: '#909399', fontSize: 12 },
      itemWidth: 12,
      itemHeight: 12,
      itemGap: 12
    },
    series: [
      {
        name: '订单状态',
        type: 'pie',
        radius: ['45%', '75%'],
        center: ['35%', '55%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 12,
          borderColor: '#0a0e27',
          borderWidth: 3
        },
        label: { show: false },
        emphasis: {
          label: {
            show: true,
            fontSize: 14,
            fontWeight: 'bold',
            color: '#e2e8f0'
          },
          itemStyle: {
            shadowBlur: 20,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        },
        data: [
          { value: statusMap.pending, name: '待处理', itemStyle: { color: '#E6A23C' } },
          { value: statusMap.confirmed, name: '已确认', itemStyle: { color: '#b37feb' } },
          { value: statusMap.in_production, name: '生产中', itemStyle: { color: '#36cfc9' } },
          { value: statusMap.allocated, name: '已占料', itemStyle: { color: '#409EFF' } },
          { value: statusMap.partial, name: '部分齐套', itemStyle: { color: '#F56C6C' } },
          { value: statusMap.complete, name: '完全齐套', itemStyle: { color: '#67C23A' } },
          { value: statusMap.shipped, name: '已发货', itemStyle: { color: '#909399' } },
          { value: statusMap.delivered, name: '已交付', itemStyle: { color: '#00b894' } },
          { value: statusMap.cancelled, name: '已取消', itemStyle: { color: '#dfe6e9' } }
        ]
      }
    ]
  }
}

const shouldRefresh = ref(true)

const setRefreshFlag = (flag: boolean): void => {
  shouldRefresh.value = flag
}

defineExpose({ setRefreshFlag })

onActivated(() => {
  if (shouldRefresh.value) {
    loadDashboardData()
  }
  shouldRefresh.value = true
})
</script>

<template>
  <div class="dashboard">
    <div class="page-header">
      <div class="header-content">
        <h1 class="page-title">仪表盘</h1>
        <p class="page-desc">实时监控业务数据和关键指标</p>
      </div>
      <div class="header-stats">
        <div class="stat-item">
          <span class="stat-value">{{ dashboardStats?.total_orders || 0 }}</span>
          <span class="stat-label">总订单</span>
        </div>
        <div class="stat-divider"></div>
        <div class="stat-item">
          <span class="stat-value success">{{ dashboardStats?.kit_rate != null ? `${Number(dashboardStats.kit_rate).toFixed(1)}%` : '0%' }}</span>
          <span class="stat-label">齐套率</span>
        </div>
      </div>
      <el-button
        type="primary"
        :loading="loading"
        class="refresh-btn"
        @click="loadDashboardData"
      >
        <el-icon v-if="!loading"><Refresh /></el-icon>
        刷新
      </el-button>
    </div>

    <el-row :gutter="20" class="kpi-row">
      <el-col
        v-for="(card, index) in kpiCards"
        :key="index"
        :xs="24"
        :sm="12"
        :md="6"
        class="kpi-col"
      >
        <div class="kpi-card" :style="{ animationDelay: `${index * 0.1}s` }">
          <div class="kpi-glow" :style="{ background: card.gradient }"></div>
          <div class="kpi-icon" :style="{ background: card.gradient }">
            <el-icon size="26">
              <component :is="card.icon" />
            </el-icon>
          </div>
          <div class="kpi-content">
            <div class="kpi-value" :style="{ color: card.color }">{{ card.value }}</div>
            <div class="kpi-title">{{ card.title }}</div>
          </div>
          <div v-if="card.change" class="kpi-change" :class="{ positive: card.change.startsWith('+') }">
            <ArrowUp v-if="card.change.startsWith('+')" class="change-icon" />
            <ArrowUp v-else class="change-icon rotate" />
            {{ card.change }}
          </div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="chart-row">
      <el-col :xs="24" :lg="14" class="chart-col">
        <div class="chart-card large">
          <div class="card-header">
            <h3>订单趋势分析</h3>
            <span class="card-subtitle">近7天数据</span>
          </div>
          <div ref="trendChartRef" class="chart-content"></div>
        </div>
      </el-col>

      <el-col :xs="24" :lg="10" class="chart-col">
        <div class="chart-card">
          <div class="card-header">
            <h3>订单状态分布</h3>
            <span class="card-subtitle">实时统计</span>
          </div>
          <div ref="statusChartRef" class="chart-content"></div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="alert-row">
      <el-col :span="24" class="alert-col">
        <div class="alert-card" :class="{ 'has-alert': deliveryChangeCount > 0 }">
          <div class="alert-icon-wrap">
            <el-icon :size="24"><Warning /></el-icon>
          </div>
          <div class="alert-content">
            <h3 class="alert-title">交期变更预警</h3>
            <p class="alert-desc">
              <template v-if="deliveryChangeCount > 0">
                有 <span class="alert-count">{{ deliveryChangeCount }}</span> 个订单交期变更次数超标，请及时处理
              </template>
              <template v-else>
                当前无交期变更超标订单
              </template>
            </p>
            <div v-if="deliveryChangeAlerts.length > 0" class="alert-orders">
              <el-tag
                v-for="order in deliveryChangeAlerts.slice(0, 5)"
                :key="order.id"
                class="alert-order-tag"
                type="warning"
                size="small"
                @click="router.push(`/sales-order`)"
              >
                {{ order.order_no }} (变更{{ order.change_count }}次)
              </el-tag>
              <span v-if="deliveryChangeAlerts.length > 5" class="alert-more">
                +{{ deliveryChangeAlerts.length - 5 }}个
              </span>
            </div>
          </div>
          <el-button
            v-if="deliveryChangeCount > 0"
            type="warning"
            size="small"
            @click="router.push('/sales-order')"
            class="alert-action-btn"
          >
            查看详情
          </el-button>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="table-row">
      <el-col :span="24" class="table-col">
        <div class="table-card">
          <div class="table-header">
            <div class="header-left">
              <h3>最近订单</h3>
              <span class="table-subtitle">最近更新的订单列表</span>
            </div>
            <el-button type="primary" size="small" @click="$router.push('/sales-order')" class="view-all-btn">
              查看全部
            </el-button>
          </div>

          <el-table border
            :data="recentOrders"
            stripe
            class="order-table"
            :loading="loading"
            :loading-text="''"
          >
            <el-table-column prop="order_no" label="订单号" width="110">
              <template #default="{ row }: { row: RecentOrderItem }">
                <span class="order-no">{{ row.order_no }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="customer_name" label="客户" width="100">
              <template #default="{ row }: { row: RecentOrderItem }">
                <span class="customer-name">{{ row.customer_name || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="物料" width="90">
              <template #default="{ row }: { row: RecentOrderItem }">
                <span class="material-code">{{ row.material_code || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="quantity" label="数量" width="60">
              <template #default="{ row }: { row: RecentOrderItem }">
                <span class="quantity">{{ row.quantity || 0 }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="demand_date" label="需求日期" width="95">
              <template #default="{ row }: { row: RecentOrderItem }">
                <span class="date">{{ row.demand_date?.split('T')[0] || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="65">
              <template #default="{ row }: { row: RecentOrderItem }">
                <el-tag
                  :type="
                    (row.status === 'complete' || row.status === 'completed') ? 'success' :
                    row.status === 'partial' ? 'warning' :
                    row.status === 'allocated' ? 'primary' :
                    row.status === 'confirmed' ? 'info' :
                    row.status === 'in_production' ? 'primary' :
                    row.status === 'processing' ? 'warning' :
                    row.status === 'shipped' ? 'info' :
                    row.status === 'delivered' ? 'success' :
                    row.status === 'cancelled' ? 'danger' :
                    'info'
                  "
                  size="small"
                  class="status-tag"
                >
                  {{ (row.status === 'complete' || row.status === 'completed') ? '完全齐套' :
                     row.status === 'partial' ? '部分齐套' :
                     row.status === 'allocated' ? '已占料' :
                     row.status === 'confirmed' ? '已确认' :
                     row.status === 'in_production' ? '生产中' :
                     row.status === 'processing' ? '进行中' :
                     row.status === 'shipped' ? '已发货' :
                     row.status === 'delivered' ? '已交付' :
                     row.status === 'cancelled' ? '已取消' : '待处理' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped lang="scss">
.dashboard {
  max-width: 1500px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 32px;
  padding: 24px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(10px);
  flex-wrap: wrap;
  gap: 16px;

  .refresh-btn {
    background: linear-gradient(135deg, #6E9EF7, #8DB0F9);
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 500;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(110, 158, 247, 0.25);
    }
  }

  .header-content {
    .page-title {
      font-size: 32px;
      font-weight: 700;
      margin: 0 0 8px 0;
      background: linear-gradient(135deg, #6E9EF7, #5DAF5A);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .page-desc {
      font-size: 14px;
      color: #78849E;
      margin: 0;
    }
  }

  .header-stats {
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 16px 24px;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 12px;

    .stat-item {
      display: flex;
      flex-direction: column;
      align-items: center;

      .stat-value {
        font-size: 28px;
        font-weight: 700;
        color: #E8EAED;

        &.success {
          color: #5DAF5A;
        }
      }

      .stat-label {
        font-size: 12px;
        color: #78849E;
        margin-top: 4px;
      }
    }

    .stat-divider {
      width: 1px;
      height: 40px;
      background: rgba(255, 255, 255, 0.08);
    }
  }
}

.kpi-row {
  margin-bottom: 24px;
}

.kpi-col {
  margin-bottom: 20px;
}

.kpi-card {
  position: relative;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 16px;
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 16px;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  backdrop-filter: blur(10px);
  overflow: hidden;
  animation: kpiCardFadeIn 0.6s ease-out backwards;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.18), transparent);
    opacity: 0;
    transition: opacity 0.3s ease;
  }

  &:hover {
    transform: translateY(-5px);
    border-color: rgba(110, 158, 247, 0.22);
    box-shadow: 0 10px 35px rgba(0, 0, 0, 0.25);

    .kpi-glow {
      opacity: 0.5;
    }

    &::before {
      opacity: 1;
    }
  }

  .kpi-glow {
    position: absolute;
    top: -50%;
    right: -50%;
    width: 150%;
    height: 150%;
    opacity: 0;
    filter: blur(65px);
    transition: opacity 0.4s ease;
    pointer-events: none;
  }
}

@keyframes kpiCardFadeIn {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.kpi-icon {
  width: 64px;
  height: 64px;
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  flex-shrink: 0;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
}

.kpi-content {
  flex: 1;
  min-width: 0;

  .kpi-value {
    font-size: 36px;
    font-weight: 700;
    line-height: 1.1;
    letter-spacing: -1px;
  }

  .kpi-title {
    font-size: 14px;
    color: #78849E;
    margin-top: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
}

.kpi-change {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  font-weight: 600;
  color: #E57373;
  padding: 6px 12px;
  border-radius: 20px;
  background: rgba(229, 115, 115, 0.1);
  flex-shrink: 0;

  .change-icon {
    font-size: 12px;
    transition: transform 0.3s ease;

    &.rotate {
      transform: rotate(180deg);
    }
  }

  &.positive {
    color: #5DAF5A;
    background: rgba(93, 175, 90, 0.1);
  }
}

.chart-row {
  margin-bottom: 24px;
}

.chart-col {
  margin-bottom: 20px;
}

.chart-card {
  position: relative;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 16px;
  padding: 24px;
  height: 400px;
  backdrop-filter: blur(10px);
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(110, 158, 247, 0.25), transparent);
  }

  &.large {
    height: 420px;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;

    h3 {
      font-size: 16px;
      font-weight: 600;
      color: #E8EAED;
      margin: 0;
    }

    .card-subtitle {
      font-size: 12px;
      color: #78849E;
    }
  }

  .chart-content {
    width: 100%;
    height: calc(100% - 50px);
  }
}

.alert-row {
  margin-bottom: 24px;
}

.alert-col {
  margin-bottom: 20px;
}

.alert-card {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 20px 24px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 16px;
  backdrop-filter: blur(10px);
  transition: all 0.3s ease;

  &.has-alert {
    border-color: rgba(230, 162, 60, 0.3);
    background: rgba(230, 162, 60, 0.05);
  }

  .alert-icon-wrap {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(230, 162, 60, 0.15);
    color: #E6A23C;
    flex-shrink: 0;
  }

  .alert-content {
    flex: 1;
    min-width: 0;

    .alert-title {
      font-size: 16px;
      font-weight: 600;
      color: #E8EAED;
      margin: 0 0 6px 0;
    }

    .alert-desc {
      font-size: 13px;
      color: #78849E;
      margin: 0 0 8px 0;

      .alert-count {
        font-size: 18px;
        font-weight: 700;
        color: #E6A23C;
      }
    }

    .alert-orders {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;

      .alert-order-tag {
        cursor: pointer;
        transition: opacity 0.2s;

        &:hover {
          opacity: 0.8;
        }
      }

      .alert-more {
        font-size: 12px;
        color: #78849E;
      }
    }
  }

  .alert-action-btn {
    flex-shrink: 0;
  }
}

.table-row {
  margin-bottom: 24px;
}

.table-col {
  margin-bottom: 20px;
}

.table-card {
  position: relative;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 16px;
  padding: 24px;
  backdrop-filter: blur(10px);
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(110, 158, 247, 0.25), transparent);
  }
}

.table-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;

  .header-left {
    h3 {
      font-size: 18px;
      font-weight: 600;
      color: #E8EAED;
      margin: 0 0 6px 0;
    }

    .table-subtitle {
      font-size: 13px;
      color: #78849E;
    }
  }

  .view-all-btn {
    background: linear-gradient(135deg, #6E9EF7, #8DB0F9);
    border: none;
    border-radius: 8px;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: 500;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(110, 158, 247, 0.25);
    }
  }
}

.order-table {
  :deep(.el-table__header-wrapper) {
    th {
      background: rgba(110, 158, 247, 0.06) !important;
      color: #B0B8C4;
      font-weight: 500;
      font-size: 13px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      padding: 14px 12px;
    }
  }

  :deep(.el-table__body-wrapper) {
    td {
      color: #E8EAED;
      font-size: 13px;
      padding: 14px 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
    }

    tr {
      transition: background 0.2s ease;

      &:hover {
        background: rgba(110, 158, 247, 0.04) !important;
      }
    }
  }

  :deep(.el-loading-mask) {
    background: rgba(31, 35, 48, 0.8);
  }
}

.order-no {
  font-family: 'Courier New', monospace;
  font-size: 13px;
  color: #6E9EF7;
}

.customer-name {
  font-size: 13px;
  color: #E8EAED;
}

.material-code {
  font-family: 'Courier New', monospace;
  font-size: 12px;
  color: #B0B8C4;
}

.quantity {
  font-size: 13px;
  color: #E8EAED;
  font-weight: 500;
}

.date {
  font-size: 12px;
  color: #78849E;
}

.status-tag {
  font-size: 11px;
  font-weight: 500;
}

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
    gap: 20px;
    text-align: center;

    .page-title {
      font-size: 24px;
    }

    .header-stats {
      width: 100%;
      justify-content: center;
    }
  }

  .kpi-card {
    padding: 20px 16px;

    .kpi-icon {
      width: 52px;
      height: 52px;
    }

    .kpi-value {
      font-size: 28px;
    }
  }

  .chart-card {
    height: 320px;
    padding: 16px;

    &.large {
      height: 340px;
    }
  }

  .table-card {
    padding: 16px;
  }

  .alert-card {
    flex-direction: column;
    align-items: flex-start;
    padding: 16px;
    gap: 12px;

    .alert-action-btn {
      width: 100%;
    }
  }
}
</style>
